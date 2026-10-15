"""Trusted Actions preparation; PR source is read as data, never executed."""
import argparse
import json
import os
from pathlib import Path
import sys

from . import review_local as local
from .build_request import build_request, checked_path, write_json
from .run import run_request

DEMO_REPO = 'james-korza-ai/review-agent-poc'


def identify(event, repo):
    if repo != DEMO_REPO or event.get('repository', {}).get('full_name') != repo:
        raise ValueError('Only the authorized synthetic personal repo is supported')
    issue, comment = event.get('issue', {}), event.get('comment', {})
    if event.get('action') != 'created' or 'pull_request' not in issue or comment.get('body') != '/review':
        raise ValueError('Expected an exact owner PR review command')
    pr, comment_id = issue.get('number'), comment.get('id')
    if type(pr) is not int or pr not in (1, 2) or type(comment_id) is not int or comment_id <= 0:
        raise ValueError('Only the two approved synthetic demo PRs are supported')
    metadata = local._gh(f'repos/{repo}')
    pull = local._gh(f'repos/{repo}/pulls/{pr}')
    current = local._gh(f'repos/{repo}/issues/comments/{comment_id}')
    if comment.get('user', {}).get('login') != metadata.get('owner', {}).get('login'):
        raise ValueError('Only the owner may request this demo')
    local._identity(repo, pr, comment_id, metadata, pull, current)
    if pull['base'].get('ref') != metadata.get('default_branch'):
        raise ValueError('Demo policy must come from the default branch')
    return {'repo': repo, 'pr': pr, 'comment_id': comment_id,
            'base_sha': pull['base']['sha'], 'head_sha': pull['head']['sha']}


def prepare(pins, source, out):
    request = build_request(source, pins['base_sha'], pins['head_sha'], out / 'request.json',
        provider={'name': 'github', 'repo': pins['repo'], 'pr_id': str(pins['pr']),
                  'pr_url': f"https://github.com/{pins['repo']}/pull/{pins['pr']}"})
    changed = request['diff']['changed_files']
    if len(changed) != 1 or changed[0]['path'] != 'review.go' or changed[0]['status'] != 'modified':
        raise ValueError('Only the made-up review.go demo change is permitted')
    if (request['repo']['head_sha'], request['repo']['base_sha']) != (pins['head_sha'], pins['base_sha']):
        raise ValueError('Source does not match actual PR commits')
    return {**pins, 'request_id': request['request_id']}


def outputs(values):
    # All values are controller-generated IDs, fixed repo or validated hex SHAs.
    destination = checked_path(os.environ['GITHUB_OUTPUT'])
    with destination.open('a', encoding='utf-8') as handle:
        for key, value in values.items():
            if '\n' in str(value) or '\r' in str(value):
                raise ValueError('Invalid step output')
            handle.write(f'{key}={value}\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('identify', 'prepare', 'run'))
    parser.add_argument('--out', required=True)
    parser.add_argument('--source')
    parser.add_argument('--skill-dir')
    args = parser.parse_args(argv)
    try:
        out = checked_path(args.out)
        if args.stage == 'identify':
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
            pins = identify(event, os.environ['GITHUB_REPOSITORY'])
            write_json(out / 'pins.json', pins)
            outputs(pins)
        elif args.stage == 'prepare':
            pins = json.loads((out / 'pins.json').read_text())
            outputs(prepare(pins, args.source, out))
        else:
            return run_request(out / 'request.json', out / 'result.json', args.skill_dir, allow_live=True)
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print('Review preparation refused; verify owner command, current commits and synthetic scope.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
