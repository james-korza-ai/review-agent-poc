# On-demand AI review demo

Comment `/review` on either demo PR. GitHub Actions reads the actual commits,
asks Gemini to check one Go error-handling rule, then puts findings beside the
relevant code. A clean run leaves only a neutral check. Nothing approves or
blocks the PR.

- [Broken error path](https://github.com/james-korza-ai/review-agent-poc/pull/1)
- [Valid fallback](https://github.com/james-korza-ai/review-agent-poc/pull/2)
- [Actions runs](https://github.com/james-korza-ai/review-agent-poc/actions)

## How it works

1. The owner comments exactly `/review`. Only these two same-repo demo PRs run.
2. A read-only job uses the engine and rule from the trusted default branch.
   It reads the pinned PR source as data, never running its code or scripts.
3. Gemini checks whether code uses a failed call's result after observing its
   error. It sees the changed file and diff, without the answer key.
4. A separate job validates the result and current diff, then posts inline
   comments and a neutral check with line annotations. It has no model key.

A finding can include a native GitHub suggested change. The edit has its own
line range and exact original text, separate from the line where the bug was
found. The reporter checks the current commit, complete diff hunk, bounds and
overlapping edits, then compiles the proposed one-file Go change without running
it. Unsupported or invalid edits fall back to advice. Applying a suggestion is
a human decision; compilation does not prove the fix is correct.

Annotation rendering reuses the existing renderer in
[`korzainc/shared-workflows`' public `ci-common` image](https://github.com/korzainc/shared-workflows/tree/ca100272df8971e477064cd211e0beb56c8d8208/images/ci-common),
pinned by digest. It receives only validated finding data, with no network,
mounts or credentials. The thin adapter retains neutral status and current-commit
checks because shared security publishing has a different gating contract.
No shared security defaults are changed and no private source is copied here.

The policy decides that all findings are advisory. The model cannot approve a
PR, enable blocking, choose tools or execute repository code. Changed commits
require a fresh `/review`; stale results are refused. Duplicate delivery of the
same command does not post twice.

## Setup

Python, Git and `gh` are available on the GitHub-hosted runner. Actions are pinned
to commit SHAs; no third-party Python package is installed. Add `GEMINI_API_KEY`
as an Actions secret. It is exposed only to the model step, not the reporter.
The reporter also uses the runner's Go compiler and Docker. Only the released,
digest-pinned public image is pulled, using a fresh credential-free client config.

The workflow uses `gemini-3.1-flash-lite`, at most 8192 generated tokens and one HTTP
attempt per command. Actual usage and a conservative list-price cost estimate
are retained in the run's receipts for one day. This is not an invoice or a
provider-enforced spend cap. The key's project controls billing and quotas.
Only made-up examples are included; free-tier model use is not approved for
company or client code.

GitHub Actions supplies its own scoped token for comments and checks. No custom
GitHub App or local Claude credentials are needed for this personal POC. An org
rollout still needs agreed authorization, data handling, budgets and evaluation.

## Limits

This proves one on-demand rule can run in CI and report beside the code. It does
not prove review quality, 90% precision, vendor parity or production readiness.
The planned labelled evaluation and vendor comparison are separate steps.
Only one changed Go file is supported here. No code is executed from a PR.

The earlier local Claude CLI and constructed-response demos remain available
for comparison. Their results are not GitHub Actions results or quality trials.

## Local checks

```sh
python3 -m unittest discover -s tests -v
```

## Eighteen-run evaluation

Run the **Six-fixture evaluation** workflow manually on `main`. It reviews each
of the six synthetic cases three times with the same model, then scores exact
rule/file/line matches. The answer key is read only after model calls. Requests,
raw responses, results and the scorecard are retained as a one-day artifact.
After model calls, a separate stage requires all six bug trials to provide an
exact-source edit that compiles, and all twelve valid-code trials to stay quiet.
That updated output contract needs a fresh evaluation; the earlier scorecard
does not establish its quality. Proposed code and generated tests never run.
Failed or rejected-output trials never count as quiet successes. Evaluation uses
`bounded-transient-retry-v1`: at most two physical attempts per logical trial,
only after HTTP 429/503 or an actual timeout. Valid answers, schema errors, other
HTTP errors and general transport errors are never retried. Every attempt keeps
its request, outcome and available raw receipts; prior batches remain separate.
The `/review` workflow still makes a single attempt.

Bounds are 15 minutes per batch, 245 seconds per logical trial, 120 seconds and
16,000 reserved tokens per physical attempt, at most 36 calls, and 32,000 reserved
tokens per logical trial. Calls start at least 5.1 seconds apart; an exhausted
service stops the batch. The HTTP wall deadline runs on the Unix main thread.
Failed calls with unavailable usage make aggregate cost and token totals unknown,
even if every logical quality trial completes. The original complete-cost bar
then remains unmet; known estimates are not an invoice or provider spend cap.
The proposed acceptance bar is labelled provisional until
the team agrees it. Passing these fixtures does not graduate the rule to blocking.
