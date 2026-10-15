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

The policy decides that all findings are advisory. The model cannot approve a
PR, enable blocking, choose tools or execute repository code. Changed commits
require a fresh `/review`; stale results are refused. Duplicate delivery of the
same command does not post twice.

## Setup

Python, Git and `gh` are available on the GitHub-hosted runner. Actions are pinned
to commit SHAs; no third-party Python package is installed. Add `GEMINI_API_KEY`
as an Actions secret. It is exposed only to the model step, not the reporter.

The workflow uses `gemini-3.8-flash`, at most 8192 generated tokens and one HTTP
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
