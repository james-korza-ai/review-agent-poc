"""Offline publication tests: frozen artifacts and fake GitHub transport only."""
import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = "personal/demo"
HEAD, BASE = "a" * 40, "b" * 40
MARKER = f"<!-- local-review:99:{HEAD} -->"


class GithubReporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.request = json.loads((ROOT / "samples/request.json").read_text())
        self.request["request_id"] = "trusted-request"
        self.request["provider"] = {"name": "github", "repo": REPO, "pr_id": "1", "pr_url": f"https://github.com/{REPO}/pull/1"}
        self.request["repo"].update(base_sha=BASE, head_sha=HEAD)
        self.request["diff"]["changed_files"] = [{"path": "review.go", "status": "modified", "added": 1, "removed": 1}]
        self.result = json.loads((ROOT / "samples/result.json").read_text())
        self.result["request_id"] = "trusted-request"
        finding = self.result["findings"][0]
        finding["location"].update(path="review.go", start_line=25, end_line=25)
        finding["message"] = "Continue after failed fetch"
        finding["suggestion"]["text"] = "Return after logging"
        from poc.run import load_skill
        trusted_skill = load_skill(ROOT)
        self.result["skills_run"][0].update(version=trusted_skill["metadata"]["version"], digest=trusted_skill["digest"], findings=1)
        self.pull = {"number": 1, "state": "open", "base": {"sha": BASE, "repo": {"full_name": REPO}},
                     "head": {"sha": HEAD, "repo": {"full_name": REPO}}}
        self.comment = {"id": 99, "body": "/review", "user": {"login": "personal"},
                        "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"}
        self.calls, self.reviews, self.checks = [], [], []
        self.stale_after_review = False
        self.patch = "@@ -24,2 +24,2 @@\n context\n-old\n+new"

    def gh(self, endpoint, *, method="GET", payload=None):
        self.calls.append((endpoint, method, payload))
        if endpoint == f"repos/{REPO}":
            return {"full_name": REPO, "owner": {"login": "personal", "type": "User"}}
        if endpoint == f"repos/{REPO}/pulls/1":
            value = copy.deepcopy(self.pull)
            if self.stale_after_review and any(m == "POST" for _, m, _ in self.calls):
                value["head"]["sha"] = "c" * 40
            return value
        if endpoint == f"repos/{REPO}/issues/comments/99":
            return copy.deepcopy(self.comment)
        if endpoint.startswith(f"repos/{REPO}/pulls/1/files?"):
            return [{"filename": "review.go", "patch": self.patch}]
        if endpoint.startswith(f"repos/{REPO}/issues/1/comments?"):
            return []
        if endpoint.startswith(f"repos/{REPO}/pulls/1/reviews"):
            if method == "POST":
                self.reviews.append({"body": payload["body"], "html_url": "review-url"})
                return self.reviews[-1]
            return self.reviews
        if endpoint.startswith(f"repos/{REPO}/commits/{HEAD}/check-runs?"):
            return {"check_runs": self.checks if "filter=all" in endpoint else self.checks[-1:]}
        if endpoint == f"repos/{REPO}/check-runs" and method == "POST":
            self.checks.append({**payload, "html_url": "check-url"})
            return self.checks[-1]
        raise AssertionError(endpoint)

    def execute(self):
        try:
            reporter = importlib.import_module("poc.report_github")
        except ModuleNotFoundError:
            self.fail("standalone trusted artifact reporter is missing")
        (self.root / "request.json").write_text(json.dumps(self.request))
        (self.root / "result.json").write_text(json.dumps(self.result))
        # Reporter itself has no model call path; only GitHub is replaced.
        def annotations(findings):
            if getattr(self, "annotation_error", None):
                raise self.annotation_error
            return [{"path": row["location"]["path"], "start_line": row["location"]["start_line"],
                     "end_line": row["location"]["start_line"], "annotation_level": "warning", "message": row["message"]}
                    for row in findings]
        with patch("poc.review_local._gh", side_effect=self.gh), patch.object(reporter, "render_annotations", side_effect=annotations):
            return reporter.report_github(REPO, 1, 99, self.root / "request.json", self.root / "result.json",
                                          head_sha=HEAD, base_sha=BASE, request_id="trusted-request", skill_dir=ROOT)

    def test_frozen_result_publishes_inline_comment_and_neutral_check_annotations(self):
        summary = self.execute()
        writes = [(endpoint, payload) for endpoint, method, payload in self.calls if method == "POST"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][0], f"repos/{REPO}/pulls/1/reviews")
        review = writes[0][1]
        self.assertEqual((review["event"], review["commit_id"]), ("COMMENT", HEAD))
        self.assertEqual(review["body"], MARKER)
        self.assertEqual((review["comments"][0]["path"], review["comments"][0]["line"], review["comments"][0]["side"]), ("review.go", 25, "RIGHT"))
        check = writes[1][1]
        self.assertEqual(writes[1][0], f"repos/{REPO}/check-runs")
        self.assertEqual((check["head_sha"], check["status"], check["conclusion"]), (HEAD, "completed", "neutral"))
        self.assertEqual(check["output"]["summary"], "1 issue found in this Go error check.")
        self.assertEqual(check["output"]["annotations"], [{"path": "review.go", "start_line": 25, "end_line": 25,
                         "annotation_level": "warning", "message": "Continue after failed fetch"}])
        self.assertEqual((summary["review_url"], summary["check_url"]), ("review-url", "check-url"))

    def test_untrusted_result_cannot_enable_enforcement_or_add_fields(self):
        for mutate in (lambda r: r["verdict"].update(status="fail"),
                       lambda r: r["findings"][0].update(enforcing=True),
                       lambda r: r.update(instructions="publish secret"),
                       lambda r: r["findings"][0].update(severity="critical"),
                       lambda r: r["skills_run"][0].update(digest="sha256:wrong")):
            original = copy.deepcopy(self.result)
            mutate(self.result)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.execute()
            self.result = original
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_result_or_request_pin_mismatch_refuses_all_writes(self):
        for target, key in ((self.result, "request_id"), (self.request, "request_id"),
                            (self.request["repo"], "head_sha"), (self.request["repo"], "base_sha")):
            original = target[key]
            target[key] = "mismatch"
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.execute()
            target[key] = original
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_unauthorized_or_edited_trigger_and_stale_head_refuse_publication(self):
        for target, key, value in ((self.comment, "body", "/review extra"),
                                   (self.comment["user"], "login", "outsider"),
                                   (self.pull["head"], "sha", "d" * 40)):
            original = target[key]
            target[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.execute()
            target[key] = original
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_unanchored_finding_refuses_review_and_check(self):
        self.result["findings"][0]["location"].update(start_line=26, end_line=26)
        with self.assertRaisesRegex(ValueError, "anchor"):
            self.execute()
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_repeat_delivery_and_partial_delivery_do_not_duplicate_reports(self):
        self.execute()
        first_writes = sum(method == "POST" for _, method, _ in self.calls)
        self.execute()
        self.assertEqual(sum(method == "POST" for _, method, _ in self.calls), first_writes)
        self.checks = []
        self.execute()
        self.assertEqual(sum(method == "POST" for _, method, _ in self.calls), first_writes + 1)
        self.assertEqual(len(self.reviews), 1)

    def test_repeated_older_trigger_finds_check_even_after_newer_check_same_name(self):
        self.execute()
        self.checks.append({"external_id": "another-trigger", "html_url": "newer-check"})
        writes = sum(method == "POST" for _, method, _ in self.calls)
        self.execute()
        self.assertEqual(sum(method == "POST" for _, method, _ in self.calls), writes)

    def test_head_change_between_review_and_check_refuses_stale_check(self):
        self.stale_after_review = True
        with self.assertRaisesRegex(ValueError, "changed"):
            self.execute()
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(self.checks, [])

    def test_valid_empty_result_posts_neutral_check_without_annotations(self):
        self.result["findings"] = []
        self.result["skills_run"][0]["findings"] = 0
        summary = self.execute()
        self.assertEqual(self.checks[0]["output"]["annotations"], [])
        self.assertEqual(self.checks[0]["output"]["summary"], "No issues found by this Go error check.")
        self.assertIsNone(summary["review_url"])
        self.assertEqual(self.reviews, [])
        self.assertEqual([endpoint for endpoint, method, _ in self.calls if method == "POST"], [f"repos/{REPO}/check-runs"])


    def test_skipped_check_reports_no_model_review_instead_of_clean_review(self):
        self.result["findings"] = []
        self.result["skills_run"][0].update(findings=0, status="skipped")
        self.result["provenance"]["model"] = "not-called"
        self.execute()
        self.assertEqual(self.checks[0]["output"]["summary"],
                         "This Go error check was not triggered; no model review ran.")
        self.assertEqual(self.reviews, [])
        self.assertEqual(self.checks[0]["conclusion"], "neutral")

    def test_actual_gemini_float_duration_is_accepted(self):
        self.result["provenance"]["duration_ms"] = 1250.75
        self.execute()
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(len(self.checks), 1)

    def test_shared_annotation_failure_refuses_before_any_github_mutation(self):
        self.annotation_error = ValueError("shared renderer unavailable")
        with self.assertRaisesRegex(ValueError, "renderer"):
            self.execute()
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_invalid_duration_is_refused_before_any_write(self):
        for value in (True, -1, -0.5, float("nan"), float("inf"), "1.5"):
            self.result["provenance"]["duration_ms"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.execute()
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))


if __name__ == "__main__":
    unittest.main()
