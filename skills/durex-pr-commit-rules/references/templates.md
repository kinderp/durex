# Durex GitHub Templates Reference

## Canonical templates

From the Durex worktree, resolve the repository root with
`git rev-parse --show-toplevel`, then read `docs/GITHUB_TEMPLATES.md` before
creating or updating any of these artifacts:

- milestone;
- parent issue;
- child issue;
- commit;
- pull request body;
- review-round entry;
- inline finding;
- finding-fix commit;
- inline finding reply;
- parent progress update;
- clean-review comment.

The canonical file is intentionally outside the skill so maintainers and
contributors can use the same templates without loading or installing Codex,
and so a symlinked skill does not create a second source of truth. Do not
maintain a second copy in the skill.

## Invocation examples

Use supervised mode by default:

```text
Use $durex-pr-commit-rules in supervised mode to prepare this change and stop
before push or PR creation.
```

Run the regulated autonomous loop only when explicitly requested:

```text
Use $durex-pr-commit-rules in autonomous PR-loop mode. Keep the PR draft and
repeat full review and finding fixes until two consecutive reviews are clean.
Stop before merge.
```

Short aliases, when the client passes slash commands through to the agent:

```text
/dpcr super
/dpcr auto
```

If a client consumes slash commands itself, use the full textual trigger.
