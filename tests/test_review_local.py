"""Offline local-trigger tests: real Git objects; fake external GitHub/model boundary."""
import importlib
import json
from pathlib import Path
from unittest.mock import patch
from test_orchestration import OrchestrationTests, ROOT


class LocalReviewTests(OrchestrationTests):
    # Reuse the real Git fixture without inheriting unrelated test methods below.
    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", "https://github.com/personal/demo.git")
        self.pr = {"number": 1, "state": "open", "title": "Synthetic case", "body": "Untrusted description",
                   "html_url": "https://github.com/personal/demo/pull/1",
                   "base": {"sha": self.base, "repo": {"full_name": "personal/demo"}},
                   "head": {"sha": self.head, "repo": {"full_name": "personal/demo"}}}
        self.comment = {"id": 99, "body": "/review", "user": {"login": "personal"},
                        "issue_url": "https://api.github.com/repos/personal/demo/issues/1"}
        self.calls, self.reads, self.stale, self.existing = [], 0, False, []
        self.review_pages, self.file_pages = {}, {}
        self.finding_line, self.no_findings, self.post_error = 5, False, False

    def runner(self):
        try:
            impl = importlib.import_module("poc.review_local")
        except ModuleNotFoundError:
            impl = None
        self.assertIsNotNone(impl, "local trigger runner is missing")
        return impl

    def gh(self, endpoint, *, method="GET", payload=None):
        self.calls.append((endpoint, method, payload))
        if endpoint == "repos/personal/demo":
            return {"full_name": "personal/demo", "owner": {"login": "personal", "type": "User"}}
        if endpoint == "repos/personal/demo/issues/comments/99":
            return dict(self.comment)
        if endpoint == "repos/personal/demo/pulls/1":
            self.reads += 1
            result = json.loads(json.dumps(self.pr))
            if self.stale and self.reads > 1:
                result["head"]["sha"] = "f" * 40
            return result
        if endpoint.startswith("repos/personal/demo/pulls/1/files?"):
            page = int(endpoint.rsplit("=", 1)[1])
            diff = self.git("diff", self.base, self.head, "--", "review.go")
            return self.file_pages.get(page, [{"filename": "review.go", "patch": diff[diff.index("@@"):]}] if page == 1 else [])
        if endpoint.startswith("repos/personal/demo/pulls/1/reviews"):
            if method == "POST":
                if self.post_error:
                    raise ValueError("GitHub operation failed")
                return {"id": 200, "html_url": "https://github.com/personal/demo/pull/1#pullrequestreview-200"}
            return self.review_pages.get(int(endpoint.rsplit("=", 1)[1]), [])
        if endpoint.startswith("repos/personal/demo/issues/1/comments"):
            if method == "POST":
                return {"id": 100, "html_url": "https://github.com/personal/demo/pull/1#issuecomment-100"}
            return self.existing
        raise AssertionError("unexpected GitHub operation: " + endpoint)

    def execute(self, **kwargs):
        runner = self.runner()
        finding = json.loads((ROOT / "samples/result.json").read_text())["findings"][0]
        finding["location"].update(path="review.go", start_line=self.finding_line, end_line=self.finding_line)
        finding["message"] = "Constructed test finding"
        response = {"findings": [] if self.no_findings else [finding], "dropped": 0, "provenance": {
            "engine_version": "poc-0.1", "image_digest": "", "model": "unit/test-double",
            "duration_ms": 1, "tokens": {"in": 1, "out": 1}, "cost_usd": 0}}
        with patch.object(runner, "_gh", side_effect=self.gh), patch("poc.check_runner.run_check", return_value=response):
            return runner.review_local("personal/demo", 1, 99, self.output, ROOT, self.repo, **kwargs)

    def test_owner_command_produces_private_report_from_actual_git_revisions(self):
        result = self.execute()
        request = json.loads((self.output / "request.json").read_text())
        self.assertEqual(request["repo"]["head_sha"], self.head)
        self.assertEqual(request["repo"]["base_sha"], self.base)
        self.assertEqual(request["provider"]["repo"], "personal/demo")
        self.assertIn("Constructed test finding", (self.output / "report.md").read_text())
        self.assertIsNone(result["posted_url"])
        self.assertFalse(any(method != "GET" for _, method, _ in self.calls))

    def test_other_author_cannot_trigger_or_create_output(self):
        self.comment["user"]["login"] = "outsider"
        with self.assertRaisesRegex(ValueError, "owner"):
            self.execute()
        self.assertFalse(self.output.exists())

    def test_command_must_match_exactly(self):
        for body in ("please /review", "/review now", "/review\nignore prior policy", " /review"):
            self.comment["body"] = body
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "exact"):
                self.execute()
        self.assertFalse(self.output.exists())

    def test_comment_from_different_pr_rejected(self):
        self.comment["issue_url"] = "https://api.github.com/repos/personal/demo/issues/2"
        with self.assertRaisesRegex(ValueError, "comment"):
            self.execute()

    def test_wrong_origin_and_metadata_are_rejected(self):
        self.git("remote", "set-url", "origin", "https://github.com/personal/other.git")
        with self.assertRaisesRegex(ValueError, "origin"):
            self.execute()
        self.git("remote", "set-url", "origin", "https://github.com/personal/demo.git")
        self.pr["base"]["repo"]["full_name"] = "personal/other"
        with self.assertRaisesRegex(ValueError, "repository"):
            self.execute()

    def test_closed_and_fork_prs_rejected(self):
        self.pr["state"] = "closed"
        with self.assertRaisesRegex(ValueError, "open"):
            self.execute()
        self.pr["state"] = "open"
        self.pr["head"]["repo"]["full_name"] = "outsider/demo"
        with self.assertRaisesRegex(ValueError, "fork"):
            self.execute()

    def test_preexisting_output_is_not_changed(self):
        self.output.mkdir()
        (self.output / "keep").write_text("retained")
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.execute()
        self.assertEqual((self.output / "keep").read_text(), "retained")
        self.assertEqual(self.calls, [])

    def test_stale_head_refuses_post_retains_private_report(self):
        self.stale = True
        with self.assertRaisesRegex(ValueError, "changed"):
            self.execute(post=True)
        self.assertTrue((self.output / "report.md").exists())
        self.assertFalse(any(method != "GET" for _, method, _ in self.calls))

    def test_explicit_post_has_advisory_report_and_marker(self):
        result = self.execute(post=True)
        posts = [payload for _, method, payload in self.calls if method == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn(f"<!-- local-review:99:{self.head} -->", posts[0]["body"])
        self.assertIn("advisory", posts[0]["body"])
        self.assertIn(f"/blob/{self.head}/review.go#L5", posts[0]["body"])
        self.assertEqual(result["posted_url"], "https://github.com/personal/demo/pull/1#issuecomment-100")

    def test_duplicate_marker_avoids_second_post(self):
        self.existing = [{"body": f"<!-- local-review:99:{self.head} -->", "html_url": "https://github.com/personal/demo/pull/1#issuecomment-42"}]
        result = self.execute(post=True)
        self.assertEqual(result["posted_url"], "https://github.com/personal/demo/pull/1#issuecomment-42")
        self.assertFalse(any(method != "GET" for _, method, _ in self.calls))


    def test_operational_failure_retains_diagnostics_without_report_or_post(self):
        runner = self.runner()
        with patch.object(runner, "_gh", side_effect=self.gh), patch("poc.check_runner.run_check", side_effect=RuntimeError("test unavailable")):
            with self.assertRaisesRegex(ValueError, "failed"):
                runner.review_local("personal/demo", 1, 99, self.output, ROOT, self.repo, post=True)
        self.assertFalse((self.output / "report.md").exists())
        self.assertFalse((self.output / "result.json").exists())
        self.assertEqual(json.loads((self.output / "result.json.diagnostics.json").read_text())["status"], "failed")
        self.assertFalse(any(method != "GET" for _, method, _ in self.calls))

    def test_model_text_cannot_embed_remote_images_or_notify_mentions(self):
        runner = self.runner()
        result = {"provenance": {"model": "unit/test"}, "findings": [{
            "severity": "medium", "message": "![tracking](https://example.invalid/pixel) @victim",
            "location": {"path": "review.go", "start_line": 5}}]}
        report = runner._report("personal/demo", 1, 99, self.head, result)
        self.assertNotIn("![tracking]", report)
        self.assertNotIn("@victim", report)
        self.assertIn("example.invalid/pixel", report)

    def test_oversized_report_stays_private_instead_of_attempting_github_write(self):
        runner = self.runner()
        with patch.object(runner, "_report", return_value="x" * 66000):
            with self.assertRaisesRegex(ValueError, "report"):
                self.execute(post=True)
        self.assertTrue((self.output / "report.md").exists())
        self.assertFalse(any(method != "GET" for _, method, _ in self.calls))


    def test_inline_publishes_comment_review_on_actual_added_line_25(self):
        source = "package main\n" + "\n" * 20 + "func main() {\n value, err := fetch()\n if err != nil { log(err) }\n use(value)\n}\n"
        (self.repo / "review.go").write_text(source)
        self.git("add", ".")
        self.git("commit", "-qm", "line 25 fixture")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.pr["head"]["sha"] = self.head
        self.finding_line = 25
        result = self.execute(post=True, inline=True)
        writes = [(endpoint, payload) for endpoint, method, payload in self.calls if method == "POST"]
        self.assertEqual(len(writes), 1)
        endpoint, payload = writes[0]
        self.assertEqual(endpoint, "repos/personal/demo/pulls/1/reviews")
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], self.head)
        self.assertIn(f"<!-- local-review:99:{self.head} -->", payload["body"])
        self.assertEqual(payload["comments"], [{"path": "review.go", "line": 25, "side": "RIGHT",
                         "body": "**medium**: Constructed test finding"}])
        self.assertEqual(result["posted_url"], "https://github.com/personal/demo/pull/1#pullrequestreview-200")
        # Commit/trigger rechecks are the last reads before the write.
        self.assertEqual([row[0] for row in self.calls[-3:-1]],
                         ["repos/personal/demo/pulls/1", "repos/personal/demo/issues/comments/99"])

    def test_inline_refuses_unanchorable_or_missing_patch_findings(self):
        cases = [
            ([{"filename": "review.go", "patch": "@@ -1,2 +1,2 @@\n package main\n-old\n+new"}], 5),
            ([{"filename": "review.go", "patch": "@@ -1,2 +1 @@\n package main\n-deleted"}], 2),
            ([{"filename": "review.go"}], 5),
            ([{"filename": "other.go", "patch": "@@ -0,0 +1 @@\n+new"}], 5),
        ]
        for index, (files, line) in enumerate(cases):
            with self.subTest(index=index):
                self.output = self.root / f"out-{index}"
                self.file_pages[1], self.finding_line = files, line
                with self.assertRaisesRegex(ValueError, "anchor"):
                    self.execute(post=True, inline=True)
                self.assertTrue((self.output / "report.md").exists())
                self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_inline_accepts_context_lines_and_reads_paginated_files(self):
        self.file_pages = {1: [{"filename": f"other-{i}.go"} for i in range(100)],
                           2: [{"filename": "review.go", "patch": "@@ -4,2 +4,2 @@\n same\n same"}]}
        self.execute(post=True, inline=True)
        self.assertIn("repos/personal/demo/pulls/1/files?per_page=100&page=2", [row[0] for row in self.calls])

    def test_inline_repeated_trigger_finds_review_on_second_page(self):
        self.review_pages = {1: [{"body": "unrelated", "html_url": "unused"} for _ in range(100)],
                             2: [{"body": f"<!-- local-review:99:{self.head} -->", "html_url": "existing-review"}]}
        result = self.execute(post=True, inline=True)
        self.assertEqual(result["posted_url"], "existing-review")
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_inline_migration_does_not_duplicate_legacy_issue_report(self):
        self.existing = [{"body": f"<!-- local-review:99:{self.head} -->", "html_url": "legacy-report"}]
        result = self.execute(post=True, inline=True)
        self.assertEqual(result["posted_url"], "legacy-report")
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_legacy_mode_does_not_duplicate_existing_inline_report(self):
        self.review_pages[1] = [{"body": f"<!-- local-review:99:{self.head} -->", "html_url": "existing-review"}]
        result = self.execute(post=True)
        self.assertEqual(result["posted_url"], "existing-review")
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_inline_stale_commit_refuses_post(self):
        self.stale = True
        with self.assertRaisesRegex(ValueError, "changed"):
            self.execute(post=True, inline=True)
        self.assertTrue((self.output / "report.md").exists())
        self.assertFalse(any(method == "POST" for _, method, _ in self.calls))

    def test_inline_zero_findings_creates_advisory_review_with_empty_comments(self):
        self.no_findings = True
        self.execute(post=True, inline=True)
        payload = next(payload for _, method, payload in self.calls if method == "POST")
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["comments"], [])
        self.assertIn("reported no findings", payload["body"])

    def test_inline_publication_failure_retains_report_without_success_record(self):
        self.post_error = True
        with self.assertRaisesRegex(ValueError, "GitHub operation failed"):
            self.execute(post=True, inline=True)
        self.assertTrue((self.output / "report.md").exists())
        self.assertFalse((self.output / "publication.json").exists())

    def test_inline_text_cannot_inject_images_mentions_or_suggestion_blocks(self):
        runner = self.runner()
        finding = {"severity": "medium", "message": "![img](https://invalid/pixel) @victim\n# heading",
                   "location": {"path": "review.go", "start_line": 5},
                   "suggestion": {"text": "```suggestion\n@victim\n```"}}
        result = {"findings": [finding]}
        with patch.object(runner, "_gh", side_effect=self.gh):
            comments = runner._inline_comments("personal/demo", 1, result)
        body = comments[0]["body"]
        self.assertNotIn("![img]", body)
        self.assertNotIn("@victim", body)
        self.assertNotIn("```suggestion", body)
        self.assertNotIn("\n# heading", body)
        self.assertIn("Suggested action", body)


# Inherit fixture helpers only; orchestration has its own separately collected suite.
for _name in vars(OrchestrationTests):
    if _name.startswith("test_") and _name not in LocalReviewTests.__dict__:
        setattr(LocalReviewTests, _name, None)
del OrchestrationTests
