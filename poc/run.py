"""Run one trusted local advisory check; repository contents remain data.

Examples (run from the trusted candidate directory):
  python3 -m poc.run /private/tmp/review-out/request.json /private/tmp/review-out/result.json --skill-dir .

CLI live calls are disabled. Constructed unit-test responses must never be used as
quality trials. Operational failures return 2 and write only a diagnostic sidecar.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from . import check_runner
from .build_request import RULE, checked_path, read_regular, relative_path, validate_policy, write_json


def load_skill(skill_dir):
    """The sole skill loader reads only explicitly selected trusted data files.

    skill.yaml is JSON-compatible YAML for this POC; no dynamic YAML constructors,
    plugin imports, repository scripts, search or skill discovery are supported.
    """
    root = checked_path(skill_dir)
    prompt = read_regular(root, "PROMPT.md")
    metadata_text = read_regular(root, "skill.yaml")
    metadata = json.loads(metadata_text)
    expected = {"name": RULE, "version": "0.4.1", "language": "go", "severity": "medium", "enforce": False,
                "triggers": {"include": ["**/*.go"], "exclude": ["**/*_test.go"],
                             "diff_contains": "call returning error"}}
    if metadata != expected or metadata["enforce"] is not False or not prompt.strip():
        raise ValueError("unsupported advisory skill metadata or empty prompt")
    digest = hashlib.sha256(prompt.encode("utf-8") + metadata_text.encode("utf-8")).hexdigest()
    return {"metadata": metadata, "prompt": prompt, "digest": "sha256:" + digest}


def trigger(request, diff):
    """Cheap POC text heuristic for err assignments; this is not Go type checking."""
    eligible = [row["path"] for row in request["diff"]["changed_files"]
                if row.get("status") != "deleted" and row["path"].endswith(".go")
                and not row["path"].endswith("_test.go")]
    if not eligible or not diff.strip():
        return False
    # Look only in added/context source lines; removed calls must not fire a review.
    source = "\n".join(line[1:] for line in diff.splitlines()
                       if line.startswith(("+", " ")) and not line.startswith("+++"))
    return bool(re.search(r"\b(?:err|error)\s*(?::=|=)\s*[\w.]+\s*\(", source))


def advisory_gate(findings):
    """The only verdict policy: the sole reference check is always advisory."""
    if not isinstance(findings, list) or any(not isinstance(row, dict) for row in findings):
        raise ValueError("invalid findings returned by check")
    for index, row in enumerate(findings, 1):
        row.update(id=f"finding-{index}", confidence=None, enforcing=False)
    return {"status": "pass", "reason": "no enforcing findings", "blocking_findings": []}


def _validate_request(request):
    required = {"schema_version", "request_id", "provider", "repo", "diff", "untrusted",
                "skills", "gate", "output", "budget"}
    if not isinstance(request, dict) or set(request) != required or request["schema_version"] != "1.0":
        raise ValueError("unsupported request schema")
    if not isinstance(request["request_id"], str) or not request["request_id"]:
        raise ValueError("missing request id")
    validate_policy({key: request[key] for key in ("skills", "gate", "budget")})
    if not Path(request["repo"]["root"]).is_absolute():
        raise ValueError("data root must be an absolute directory")
    root = checked_path(request["repo"]["root"])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("data root must be an absolute directory")
    rows = request["diff"]["changed_files"]
    if not isinstance(rows, list) or len(rows) > request["budget"]["max_files"]:
        raise ValueError("invalid changed files budget")
    paths = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "status", "added", "removed"}:
            raise ValueError("unsupported changed file shape")
        relative_path(row["path"])
        paths.append(row["path"])
        if row["status"] not in {"added", "modified", "deleted"}:
            raise ValueError("unsupported changed file status")
        if any(type(row[key]) is not int or row[key] < 0 for key in ("added", "removed")):
            raise ValueError("invalid changed line counts")
        if row["status"] != "deleted":
            read_regular(root, row["path"])
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate changed file")
    location = request["diff"]["location"]
    if not isinstance(location, str):
        raise ValueError("invalid diff location")
    if Path(location).is_absolute():
        location = str(Path(location).relative_to(root))
    diff = read_regular(root, location)
    return root, diff


def run_request(request_path, result_path, skill_dir, *, allow_live=False):
    """Return 0 on a complete advisory run, 2 on failure. No retry or fallback."""
    request_id = None
    own_diagnostic = False
    diagnostic_path = Path(str(result_path) + ".diagnostics.json")
    try:
        request_path, result_path = checked_path(request_path), checked_path(result_path)
        if result_path.exists() or diagnostic_path.exists():
            raise ValueError("output exists; use a fresh path to preserve evidence")
        request = json.loads(read_regular(request_path.parent, request_path.name))
        request_id = request.get("request_id") if isinstance(request, dict) else None
        data_root, diff = _validate_request(request)
        skill_root = checked_path(skill_dir)
        if skill_root == data_root or data_root in skill_root.parents:
            raise ValueError("skill directory must be trusted and outside PR data")
        if result_path == data_root or data_root in result_path.parents:
            raise ValueError("result must be outside PR data")
        skill = load_skill(skill_root)
        fired = trigger(request, diff)
        if fired:
            checked = check_runner.run_check(request, skill, allow_live=allow_live)
            status = "ok"
        else:
            checked = {"findings": [], "dropped": 0,
                       "provenance": {"engine_version": "poc-0.1", "image_digest": "", "model": "not-called",
                                      "duration_ms": 0, "tokens": {"in": 0, "out": 0}, "cost_usd": 0}}
            status = "skipped"
        findings = checked["findings"]
        verdict = advisory_gate(findings)
        result = {"schema_version": "1.0", "request_id": request_id, "verdict": verdict, "findings": findings,
                  "skills_run": [{"skill": RULE, "version": skill["metadata"]["version"],
                                  "digest": skill["digest"], "enforcing": False,
                                  "findings": len(findings), "status": status}],
                  "provenance": checked["provenance"]}
        diagnostic = {"status": "completed", "request_id": request_id, "trigger_fired": fired,
                      "dropped": checked["dropped"], "receipts": checked.get("receipts", {}),
                      "scope": "local POC; no graduation or vendor-parity evidence"}
        # Serialization failures must be reported before publishing success diagnostics.
        json.dumps(result, allow_nan=False)
        json.dumps(diagnostic, allow_nan=False)
        # Write the sidecar first: inability to retain diagnostics cannot leave a
        # newly emitted pass result. Existing output evidence is never overwritten.
        write_json(diagnostic_path, diagnostic)
        own_diagnostic = True
        write_json(result_path, result)
        return 0
    except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
        safe_codes = {"model_transport_failed", "model_usage_invalid", "model_usage_missing",
                      "model_response_unfinished", "model_response_nontext", "model_response_no_text",
                      "model_receipt_invalid", "model_decode_invalid", "model_redirect_refused",
                      "model_configuration_missing", "model_configuration_invalid",
                      "model_credential_missing", "model_budget_exceeded"}
        code = getattr(exc, "code", "review_failed")
        if not isinstance(code, str) or not (code in safe_codes or re.fullmatch(r"model_http_[1-5][0-9]{2}", code)):
            code = "review_failed"
        diagnostic = {"status": "failed", "request_id": request_id, "error_type": type(exc).__name__,
                      "error_code": code,
                      "message": "Review did not complete; no result was published. See the safe error code."}
        try:
            if own_diagnostic:
                diagnostic_path.unlink()
            write_json(diagnostic_path, diagnostic)
        except (ValueError, OSError):
            # Refuse overwrites and do not expose exception text or credentials.
            pass
        print(f"review failed ({code}); no result published", file=sys.stderr)
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="request JSON produced by trusted build_request")
    parser.add_argument("result", help="new result JSON path outside PR data")
    parser.add_argument("--skill-dir", required=True, help="explicit trusted directory with PROMPT.md and skill.yaml")
    args = parser.parse_args(argv)
    return run_request(args.request, args.result, args.skill_dir)


if __name__ == "__main__":
    raise SystemExit(main())
