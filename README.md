# Local review-agent POC

Comment `/review` on a demo PR. The owner then starts the local runner, which reads
that exact comment and the actual PR commits, calls the locally signed-in Claude
CLI, validates findings and posts an advisory summary with file/line links.

This demo contains made-up Go code only. It checks one mistake: logging/checking an
error and then using the failed result. The two PRs show a broken case and a valid
fallback. The model gets the whole changed file and diff, without fixture labels
or the answer key. The controller chooses the advisory verdict, not the model.

## Run locally

Use Python 3.9+, Git, an authenticated `gh`, and the existing Claude CLI signed in
locally. No API key is stored in this repository or passed to GitHub Actions.
Keep this checkout on its reviewed `main`; do not execute code from a PR branch.

1. Fetch the two PR commit objects without checking out their code.
2. Copy `PROMPT.md` and `skill.yaml` into a trusted folder outside this clone.
3. Set `MODEL_PROVIDER=claude-cli`, `REVIEW_DEMO_CLI_WORKDIR` to a fresh empty
   local directory, and `REVIEW_DEMO_LEDGER` to a local attempts file.
4. The owner posts exactly `/review` on a demo PR. Take the comment ID from `gh api`.
5. Run from this trusted checkout:

```sh
python3 -m poc.review_local --repo james-korza-ai/review-agent-poc \
  --pr PR_NUMBER --comment-id COMMENT_ID --source-repo "$PWD" \
  --skill-dir TRUSTED_FOLDER --out FRESH_OUTPUT_FOLDER --post
```

Without `--post`, the report stays local. Unknown commenters, forks, changed
commits, reused outputs and invalid findings are rejected. PR data never executes.

## What this proves and what it does not

- A real local Claude CLI review can consume an owner PR command and return a
  validated advisory finding. The runner is manually invoked, not a webhook or
  always-on GitHub service. `/review` alone will not start a hosted job.
- Model: Claude Opus 5.5, medium effort. Tools, hooks, plugins, MCP and browser
  integration are disabled. Review calls have no persisted conversation history.
- At most six CLI attempts, with a $1 CLI budget per invocation. This is a local
  control, not a provider-enforced hard cap; failures also consume an attempt.
- Usage comes from the CLI. Reported cost is its list-price estimate, not a bill.
  One CLI invocation is not a guarantee of one underlying provider HTTP request.
- Results are ordinary PR comments, not inline review threads or required checks.
- This skips the proposed direct-API/provider-cap route and does not count as the
  planned 18-review scorecard. It proves neither 90% precision nor vendor parity.
- All examples are synthetic. No company/client code or credentials are included.
  The repository is private, so viewers need collaborator access.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

The separate `demo_local.py` is an explicitly labelled simulation; its constructed
responses are excluded from real quality scoring. No hosted workflow is configured.
