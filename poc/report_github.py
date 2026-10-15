"""Publish frozen advisory artifacts from a trusted GitHub Actions reporter job.

This module never runs a model, PR code, or source checkout. Its caller supplies
base/head/request pins from a trusted prepare job, independently of the downloaded
model artifact. Only the reporter job receives a token with write permissions.
"""
import argparse
import json
import math
import re
import subprocess
import sys

from . import review_local as local
from .shared_annotations import render_annotations
from .build_request import RULE, checked_path, read_regular, relative_path, validate_policy
from .run import load_skill


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ValueError("unsupported frozen artifact shape")


def _nonnegative(value):
    return type(value) is int and value >= 0


def _validate(repo, pr, request, result, head, base, request_id, skill_dir):
    """Treat downloaded JSON as untrusted; accept only the frozen advisory rule."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or type(pr) is not int or pr <= 0:
        raise ValueError("invalid selected repository or PR")
    if any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head, base)):
        raise ValueError("invalid trusted commit pin")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("missing trusted request pin")
    _shape(request, "schema_version request_id provider repo diff untrusted skills gate output budget")
    _shape(request["provider"], "name repo pr_id pr_url")
    expected_provider = {"name": "github", "repo": repo, "pr_id": str(pr), "pr_url": f"https://github.com/{repo}/pull/{pr}"}
    if request["schema_version"] != "1.0" or request["request_id"] != request_id or request["provider"] != expected_provider:
        raise ValueError("request does not match trusted invocation pins")
    _shape(request["repo"], "root base_ref base_sha head_ref head_sha")
    if (request["repo"]["head_sha"], request["repo"]["base_sha"]) != (head, base):
        raise ValueError("request commit pins do not match trusted invocation")
    validate_policy({key: request[key] for key in ("skills", "gate", "budget")})
    _shape(request["diff"], "location changed_files")
    changed = request["diff"]["changed_files"]
    if not isinstance(changed, list) or len(changed) > request["budget"]["max_files"]:
        raise ValueError("invalid changed file list")
    paths = set()
    for row in changed:
        _shape(row, "path status added removed")
        relative_path(row["path"])
        if row["path"] in paths or row["status"] not in {"added", "modified", "deleted"} or any(not _nonnegative(row[key]) for key in ("added", "removed")):
            raise ValueError("invalid changed file")
        if row["status"] != "deleted":
            paths.add(row["path"])
    _shape(result, "schema_version request_id verdict findings skills_run provenance")
    if result["schema_version"] != "1.0" or result["request_id"] != request_id:
        raise ValueError("result does not match trusted request pin")
    if result["verdict"] != {"status": "pass", "reason": "no enforcing findings", "blocking_findings": []}:
        raise ValueError("only the frozen advisory verdict is supported")
    findings = result["findings"]
    if not isinstance(findings, list) or len(findings) > 50:
        raise ValueError("invalid findings or annotation limit exceeded")
    for index, finding in enumerate(findings, 1):
        _shape(finding, "id rule_id skill severity confidence enforcing location message suggestion")
        if (finding["id"], finding["rule_id"], finding["skill"], finding["severity"]) != (f"finding-{index}", RULE, "error-handling-go", "medium") or finding["confidence"] is not None or finding["enforcing"] is not False:
            raise ValueError("unsupported finding policy")
        _shape(finding["location"], "path start_line end_line start_col end_col")
        loc = finding["location"]
        if loc["path"] not in paths or not loc["path"].endswith(".go") or loc["path"].endswith("_test.go"):
            raise ValueError("finding is outside eligible changed files")
        if any(not _nonnegative(loc[key]) for key in ("start_line", "end_line", "start_col", "end_col")) or not 1 <= loc["start_line"] <= loc["end_line"]:
            raise ValueError("invalid finding location")
        if not isinstance(finding["message"], str) or not finding["message"].strip():
            raise ValueError("invalid finding message")
        suggestion = finding["suggestion"]
        if not isinstance(suggestion, dict) or set(suggestion) not in ({"text", "replacement"}, {"text", "replacement", "edit"}):
            raise ValueError("invalid suggestion shape")
        if any(not isinstance(suggestion[key], str) for key in ("text", "replacement")):
            raise ValueError("invalid suggestion")
    skills = result["skills_run"]
    if not isinstance(skills, list) or len(skills) != 1:
        raise ValueError("unsupported skill execution")
    _shape(skills[0], "skill version digest enforcing findings status")
    trusted = load_skill(skill_dir)
    if skills[0] != {"skill": RULE, "version": trusted["metadata"]["version"], "digest": trusted["digest"],
                     "enforcing": False, "findings": len(findings), "status": skills[0]["status"]} or skills[0]["enforcing"] is not False or skills[0]["status"] not in {"ok", "skipped"} or (findings and skills[0]["status"] != "ok"):
        raise ValueError("result skill does not match trusted skill")
    provenance = result["provenance"]
    _shape(provenance, "engine_version image_digest model duration_ms tokens cost_usd")
    _shape(provenance["tokens"], "in out")
    if any(not isinstance(provenance[key], str) for key in ("engine_version", "image_digest", "model")) or not provenance["model"]:
        raise ValueError("invalid model provenance")
    duration = provenance["duration_ms"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0 or any(not _nonnegative(value) for value in provenance["tokens"].values()):
        raise ValueError("invalid model usage")
    cost = provenance["cost_usd"]
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        raise ValueError("invalid model cost")


def _current(repo, pr, comment_id, metadata, head, base):
    pull = local._gh(f"repos/{repo}/pulls/{pr}")
    comment = local._gh(f"repos/{repo}/issues/comments/{comment_id}")
    local._identity(repo, pr, comment_id, metadata, pull, comment)
    if (pull["head"]["sha"], pull["base"]["sha"]) != (head, base):
        raise ValueError("PR commits changed; refusing stale publication")


def report_github(repo, pr, comment_id, request_path, result_path, *, head_sha, base_sha, request_id, skill_dir):
    """Publish inline findings when present and one neutral check, idempotently."""
    if type(comment_id) is not int or comment_id <= 0:
        raise ValueError("invalid owner trigger pin")
    request_path, result_path = checked_path(request_path), checked_path(result_path)
    if request_path.stat().st_size > 4 * 1024 * 1024 or result_path.stat().st_size > 1024 * 1024:
        raise ValueError("artifact exceeds reporter limit")
    request = json.loads(read_regular(request_path.parent, request_path.name))
    result = json.loads(read_regular(result_path.parent, result_path.name))
    _validate(repo, pr, request, result, head_sha, base_sha, request_id, skill_dir)
    metadata = local._gh(f"repos/{repo}")
    _current(repo, pr, comment_id, metadata, head_sha, base_sha)
    comments = local._inline_comments(repo, pr, result, head_sha=head_sha)
    annotations = render_annotations(result["findings"])
    marker = f"<!-- local-review:{comment_id}:{head_sha} -->"
    review_url = None
    if comments:
        for endpoint in (f"repos/{repo}/issues/{pr}/comments", f"repos/{repo}/pulls/{pr}/reviews"):
            duplicate = next((row for row in local._pages(endpoint) if marker in (row.get("body") or "")), None)
            if duplicate:
                review_url = duplicate["html_url"]
                break
        if review_url is None:
            _current(repo, pr, comment_id, metadata, head_sha, base_sha)
            review_url = local._gh(f"repos/{repo}/pulls/{pr}/reviews", method="POST", payload={
                "commit_id": head_sha, "event": "COMMENT", "body": marker, "comments": comments})["html_url"]
    check_url, page = None, 1
    while True:
        response = local._gh(f"repos/{repo}/commits/{head_sha}/check-runs?filter=all&per_page=100&page={page}")
        checks = response["check_runs"]
        duplicate = next((row for row in checks if row.get("external_id") == marker), None)
        if duplicate:
            check_url = duplicate["html_url"]
            break
        if len(checks) < 100:
            break
        page += 1
    if check_url is None:
        count = len(comments)
        summary = (f"{count} issue{'s' if count != 1 else ''} found in this Go error check."
                   if count else "No issues found by this Go error check.")
        if result["skills_run"][0]["status"] == "skipped":
            summary = "This Go error check was not triggered; no model review ran."
        _current(repo, pr, comment_id, metadata, head_sha, base_sha)
        check_url = local._gh(f"repos/{repo}/check-runs", method="POST", payload={
            "name": "Advisory Go error handling", "head_sha": head_sha, "external_id": marker,
            "status": "completed", "conclusion": "neutral",
            "output": {"title": "Advisory Go error-handling review", "summary": summary,
                       "annotations": annotations}})["html_url"]
    return {"repo": repo, "pr": pr, "head_sha": head_sha, "comment_id": comment_id,
            "review_url": review_url, "check_url": check_url}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "request", "result", "head-sha", "base-sha", "request-id", "skill-dir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--comment-id", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        summary = report_github(args.repo, args.pr, args.comment_id, args.request, args.result,
                                head_sha=args.head_sha, base_sha=args.base_sha, request_id=args.request_id,
                                skill_dir=args.skill_dir)
        print(json.dumps(summary))
        return 0
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        print("Advisory publication refused; check trusted pins, owner trigger, current PR and retained artifacts.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
