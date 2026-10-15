"""Build a frozen-shape request from Git objects, without checking out PR code.

The caller selects the trusted base revision and trusted engine. Repository code
is data. The local POC config path defaults to .review-agent.json; its contents
come exclusively from the base tree. No repository programs, filters or hooks run.
"""
import argparse
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import uuid

RULE = "error-handling-go/continue-after-error"
DEFAULT_BUDGET = {"max_files": 200, "max_tokens": 400000, "timeout_seconds": 600}


def relative_path(value):
    """Accept a portable, confined repository-relative regular-file path."""
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("invalid data path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")) or ".git" in path.parts:
        raise ValueError("data path must stay within its root")
    return path


def checked_path(value):
    """Reject symlink components, including ancestors, before local I/O."""
    path = Path(os.path.abspath(value))
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("symlink paths are not permitted")
    return path


def read_regular(root, relative):
    """Read UTF-8 data through no-follow directory descriptors, never symlinks."""
    root = checked_path(root)
    path = relative_path(relative)
    descriptors = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in path.parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=descriptor)
        descriptors.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("data must be a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
            descriptors.pop()
            return handle.read()
    except (OSError, UnicodeError) as exc:
        raise ValueError("unable to read confined regular UTF-8 data") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def write_json(path, data):
    """The JSON writer refuses symlinks and overwrites, preserving old evidence."""
    path = checked_path(path)
    encoded = (json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_path(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def validate_policy(config):
    """Only the frozen, single advisory policy is implemented this week."""
    if not isinstance(config, dict) or set(config) != {"skills", "gate", "budget"}:
        raise ValueError("config must contain exactly skills, gate and budget")
    expected_skills = {"run": [RULE], "config": {RULE: {"enforce": False}}}
    if config["skills"] != expected_skills or config["skills"]["config"][RULE]["enforce"] is not False:
        raise ValueError("only the single advisory reference check is supported")
    if config["gate"] != {"default_enforce": False, "fail_on_severity": "high"} or config["gate"]["default_enforce"] is not False:
        raise ValueError("only the frozen advisory gate is supported")
    budget = config["budget"]
    if not isinstance(budget, dict) or set(budget) != set(DEFAULT_BUDGET):
        raise ValueError("unsupported budget shape")
    for key, ceiling in DEFAULT_BUDGET.items():
        if type(budget[key]) is not int or not 0 < budget[key] <= ceiling:
            raise ValueError("budget must contain positive integers within POC ceilings")
    return copy.deepcopy(config)


def _git(repo, *args):
    # Sanitize Git environment controls. show/diff read blobs only and explicitly
    # disable external diff/textconv; there is no checkout, import, shell or hook.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    process = subprocess.run(["/usr/bin/git", "--no-pager", "--no-replace-objects", "-C", str(repo),
                              *args], env=environment, capture_output=True, timeout=30)
    if process.returncode:
        # Git stderr may contain attacker-controlled data; keep diagnostics generic.
        raise ValueError("Git object read failed")
    return process.stdout


def _revision(repo, ref):
    if not isinstance(ref, str) or not ref or ref.startswith("-") or "\x00" in ref:
        raise ValueError("invalid revision")
    value = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("invalid resolved commit")
    return value


def _tree(repo, revision):
    entries = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry:
            continue
        header, name = entry.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split()
        path = name.decode("utf-8")
        relative_path(path)
        entries[path] = (mode, kind, oid)
    return entries


def _blob(repo, tree, path):
    entry = tree.get(path)
    if entry is None or entry[0] not in {"100644", "100755"} or entry[1] != "blob":
        raise ValueError("Git data must be a regular file")
    return _git(repo, "cat-file", "blob", entry[2]).decode("utf-8")


def build_request(repo, base, head, out, *, config_path=".review-agent.json", provider=None, untrusted=None):
    """Write request + changed-file snapshot outside repo; refs are caller-trusted."""
    repo, out = checked_path(repo), checked_path(out)
    if out == repo or repo in out.parents:
        raise ValueError("output must be outside the source repository")
    if out.exists() or (out.parent / "data").exists():
        raise ValueError("output already exists; use a fresh owned output directory")
    relative_path(config_path)
    base_sha, head_sha = _revision(repo, base), _revision(repo, head)
    base_tree, head_tree = _tree(repo, base_sha), _tree(repo, head_sha)
    policy = validate_policy(json.loads(_blob(repo, base_tree, config_path)))
    diff_args = ("--no-ext-diff", "--no-textconv", "--no-renames", base_sha, head_sha, "--")
    diff = _git(repo, "diff", *diff_args).decode("utf-8")
    changed = []
    contents = {}
    for record in _git(repo, "diff", "--numstat", "-z", *diff_args).split(b"\0"):
        if not record:
            continue
        added, removed, filename = record.split(b"\t", 2)
        path = filename.decode("utf-8")
        relative_path(path)
        if added == b"-" or removed == b"-":
            raise ValueError("binary changes are outside the synthetic POC")
        status = "added" if path not in base_tree else "deleted" if path not in head_tree else "modified"
        # Even deleted symlinks are rejected: their target text is not source.
        if path in base_tree:
            _blob(repo, base_tree, path)
        if path in head_tree:
            contents[path] = _blob(repo, head_tree, path)
        changed.append({"path": path, "status": status, "added": int(added), "removed": int(removed)})
    if len(changed) > policy["budget"]["max_files"]:
        raise ValueError("changed files exceed max_files budget")
    if "change.diff" in contents:
        raise ValueError("changed file collides with reserved snapshot diff")
    data = out.parent / "data"
    data.mkdir(parents=True, exist_ok=False)
    for path, text in {**contents, "change.diff": diff}.items():
        destination = data / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(text)
        destination.chmod(0o600)
    request = {"schema_version": "1.0", "request_id": str(uuid.uuid4()),
               "provider": provider or {"name": "github", "repo": "local/synthetic", "pr_id": "0", "pr_url": ""},
               "repo": {"root": str(data), "base_ref": base, "base_sha": base_sha,
                        "head_ref": head, "head_sha": head_sha},
               "diff": {"location": "change.diff", "changed_files": changed},
               "untrusted": untrusted or {"pr_title": "", "pr_description": "", "comments": []},
               "skills": policy["skills"], "gate": policy["gate"],
               "output": {"format": "sarif+verdict", "path": str(out.parent)}, "budget": policy["budget"]}
    write_json(out, request)
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="local synthetic Git repository; read only")
    parser.add_argument("--base", required=True, help="trusted actual base ref or SHA")
    parser.add_argument("--head", required=True, help="actual PR head ref or SHA")
    parser.add_argument("--out", required=True, help="request JSON in fresh output directory outside repository")
    parser.add_argument("--config-path", default=".review-agent.json", help="JSON policy path in base tree")
    args = parser.parse_args(argv)
    try:
        build_request(args.repo, args.base, args.head, args.out, config_path=args.config_path)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError):
        print("request construction failed; check revisions, base policy and confined output paths", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
