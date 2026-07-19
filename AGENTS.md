# Durex Agent Instructions

Read these files before modifying Durex:

1. `docs/OPERATING_RULES.md`
2. `docs/GITHUB_WORKFLOW.md` for GitHub, commit, PR, or review work
3. the task-specific user, architecture, configuration, or subsystem docs

Use `skills/durex-pr-commit-rules/SKILL.md` when work involves commits,
milestones, parent or child issues, pull requests, reviews, findings, inline
comments, or GitHub traceability. Supervised mode is the default. Autonomous
PR-loop mode must be explicitly requested and always stops before merge.

Keep changes scoped, documented, and testable. Preserve unrelated user changes.
Never commit local databases, Telegram credentials, chat identifiers, voice
alias state, logs, caches, or generated artifacts.

For runtime changes, identify the observable contract and relevant tests before
editing. Keep queue lifecycle, session identifiers, approval deduplication, PTY
ownership, SQLite transactions, Telegram authorization, polling ownership, and
voice privacy explicit.
