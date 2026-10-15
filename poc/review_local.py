"""Manually consume one owner /review comment using a trusted local engine.

This is an explicit local trigger, not a webhook or always-on service. Source
clone Git objects must already contain the actual GitHub base/head commits.
Default is private output; --post publishes one advisory PR issue comment.
No approval, required status, hosted check, or quality scorecard is produced.
"""
import argparse
import html
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

from . import run
from .build_request import _git, build_request, checked_path, read_regular, write_json


def _gh(endpoint, *, method="GET", payload=None):
    """Only GitHub transport; gh owns authentication, never read credential files."""
    command = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
    if payload is not None:
        command += ["--input", "-"]
    process = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                             capture_output=True, text=True, timeout=60)
    if process.returncode:
        raise ValueError("GitHub operation failed; private evidence remains available")
    return json.loads(process.stdout)


def _identity(repo, pr, comment_id, metadata, pull, comment):
    if metadata.get("full_name", "").lower() != repo.lower():
        raise ValueError("repository metadata does not match selected repository")
    owner = metadata.get("owner", {})
    if owner.get("type") != "User" or owner.get("login", "").lower() != repo.split("/")[0].lower():
        raise ValueError("only the selected personal repository owner is supported")
    if pull.get("number") != pr or pull.get("base", {}).get("repo", {}).get("full_name", "").lower() != repo.lower():
        raise ValueError("PR repository does not match selected repository")
    if pull.get("head", {}).get("repo", {}).get("full_name", "").lower() != repo.lower():
        raise ValueError("fork PRs are outside this local demonstration")
    if pull.get("state") != "open":
        raise ValueError("PR must be open")
    for side in ("base", "head"):
        if not re.fullmatch(r"[0-9a-f]{40}", pull[side].get("sha", "")):
            raise ValueError("actual GitHub commit SHA is invalid")
    expected_issue = f"https://api.github.com/repos/{repo}/issues/{pr}"
    if comment.get("id") != comment_id or comment.get("issue_url", "").lower() != expected_issue.lower():
        raise ValueError("trigger comment does not belong to selected PR")
    if comment.get("user", {}).get("login", "").lower() != owner["login"].lower():
        raise ValueError("only the personal repository owner may trigger review")
    if comment.get("body") != "/review":
        raise ValueError("trigger must be exact /review")


def _text(value):
    # PR/model text cannot inject HTML or notify arbitrary mentioned accounts.
    escaped = html.escape(str(value), quote=False).replace("@", "&#64;")
    # Keep findings as literal text within the controller-owned Markdown links.
    return re.sub(r"([\\`*_{}\[\]()!|])", r"\\\1", escaped)


def _report(repo, pr, comment_id, head, result):
    marker = f"<!-- local-review:{comment_id}:{head} -->"
    lines = [marker, "### Local advisory review", "",
             f"PR #{pr}, commit `{head}`; owner trigger comment `{comment_id}`.", "",
             "Manually run with the trusted local engine and local Claude CLI. This is an advisory demonstration; "
             "it creates no approval, required status or hosted check and does not count toward the 18-review scorecard.", "",
             f"Model: {_text(result['provenance']['model'])}. Findings: {len(result['findings'])}.", ""]
    for finding in result["findings"]:
        location = finding["location"]
        path = location["path"]
        line = location["start_line"]
        url = f"https://github.com/{repo}/blob/{head}/{quote(path, safe='/')}#L{line}"
        lines += [f"- **{_text(finding['severity'])}** [{_text(path)}:{line}]({url}): {_text(finding['message'])}"]
        suggestion = finding.get("suggestion", {}).get("text")
        if suggestion:
            lines += [f"  Suggested action: {_text(suggestion)}"]
    if not result["findings"]:
        lines += ["The single advisory Go error-handling check reported no findings."]
    return "\n".join(lines) + "\n"


def review_local(repo, pr, comment_id, out, skill_dir, source_repo, *, post=False):
    """Consume explicit selected GitHub identity; output must be fresh and owned."""
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo):
        raise ValueError("expected selected owner/repository")
    if type(pr) is not int or pr <= 0 or type(comment_id) is not int or comment_id <= 0:
        raise ValueError("PR and comment ID must be positive integers")
    out, source_repo, skill_dir = checked_path(out), checked_path(source_repo), checked_path(skill_dir)
    if out.exists():
        raise ValueError("output must be a fresh owned directory")
    if out == source_repo or source_repo in out.parents:
        raise ValueError("output must be outside source repository")
    if skill_dir == source_repo or source_repo in skill_dir.parents:
        raise ValueError("skill must be trusted and outside source repository")
    origin = _git(source_repo, "config", "--get", "remote.origin.url").decode().strip()
    origins = {f"https://github.com/{repo}", f"https://github.com/{repo}.git", f"git@github.com:{repo}", f"git@github.com:{repo}.git"}
    if origin.lower() not in {value.lower() for value in origins}:
        raise ValueError("source origin does not match selected repository")
    metadata = _gh(f"repos/{repo}")
    pull = _gh(f"repos/{repo}/pulls/{pr}")
    comment = _gh(f"repos/{repo}/issues/comments/{comment_id}")
    _identity(repo, pr, comment_id, metadata, pull, comment)
    # Reserve output after trigger authorization; mkdir fails instead of overwriting.
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    base, head = pull["base"]["sha"], pull["head"]["sha"]
    write_json(out / "trigger.json", {"repo": repo, "pr": pr, "comment_id": comment_id,
               "author": comment["user"]["login"], "command": comment["body"], "base_sha": base, "head_sha": head,
               "trigger": "manual-local", "posting_requested": post})
    build_request(source_repo, base, head, out / "request.json",
                  provider={"name": "github", "repo": repo, "pr_id": str(pr),
                            "pr_url": f"https://github.com/{repo}/pull/{pr}"},
                  untrusted={"pr_title": pull.get("title", ""), "pr_description": pull.get("body") or "",
                             "comments": [comment["body"]]})
    if run.run_request(out / "request.json", out / "result.json", skill_dir, allow_live=True):
        raise ValueError("local review failed; diagnostic sidecar retained")
    result = json.loads(read_regular(out, "result.json"))
    report = _report(repo, pr, comment_id, head, result)
    with (out / "report.md").open("x", encoding="utf-8") as handle:
        handle.write(report)
    posted_url = None
    if post:
        if len(report) > 65536:
            raise ValueError("report exceeds GitHub comment limit; private report retained")
        # Read all existing comments for repeat-invocation protection.
        page = 1
        while True:
            comments = _gh(f"repos/{repo}/issues/{pr}/comments?per_page=100&page={page}")
            duplicate = next((row for row in comments if f"<!-- local-review:{comment_id}:{head} -->" in row.get("body", "")), None)
            if duplicate:
                posted_url = duplicate["html_url"]
                break
            if len(comments) < 100:
                break
            page += 1
        if posted_url is None:
            latest = _gh(f"repos/{repo}/pulls/{pr}")
            latest_comment = _gh(f"repos/{repo}/issues/comments/{comment_id}")
            _identity(repo, pr, comment_id, metadata, latest, latest_comment)
            if latest["head"]["sha"] != head or latest["base"]["sha"] != base:
                raise ValueError("PR commits changed; private report retained, rerun against current commits")
            posted_url = _gh(f"repos/{repo}/issues/{pr}/comments", method="POST", payload={"body": report})["html_url"]
    summary = {"repo": repo, "pr": pr, "comment_id": comment_id, "head_sha": head,
               "report": str(out / "report.md"), "posted_url": posted_url, "scope": "local advisory demonstration"}
    write_json(out / "publication.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--comment-id", required=True, type=int)
    parser.add_argument("--out", required=True)
    parser.add_argument("--skill-dir", required=True)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--post", action="store_true", help="explicitly publish advisory PR issue comment")
    args = parser.parse_args(argv)
    try:
        summary = review_local(args.repo, args.pr, args.comment_id, args.out, args.skill_dir, args.source_repo, post=args.post)
        print(json.dumps(summary))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        print("Local review did not complete. Check selected repository, owner trigger, actual Git objects and retained private diagnostics.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
