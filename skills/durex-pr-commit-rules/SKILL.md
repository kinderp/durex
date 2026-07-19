---
name: durex-pr-commit-rules
description: Durex repository commit, milestone, parent and child issue, pull request, review finding, inline comment, and traceability workflow. Use when Codex is asked to plan GitHub work, create or update Durex issues or milestones, commit changes, open or update a PR, perform supervised or autonomous PR review loops, fix review findings, reply to inline comments, or determine whether a Durex PR is ready for merge.
---

# Durex PR and Commit Rules

Apply the repository workflow in `docs/GITHUB_WORKFLOW.md` and the operating
baseline in `docs/OPERATING_RULES.md`. Read
`references/templates.md` before creating public GitHub content or a finding-fix
commit.

## Establish context

Before changing files or GitHub state:

1. Read `AGENTS.md`, `docs/OPERATING_RULES.md`, and
   `docs/GITHUB_WORKFLOW.md` from the repository root.
2. Inspect the current branch, worktree, recent commits, linked issue, PR, and
   milestone.
3. Leave unrelated tracked and untracked files untouched.
4. Identify the current contract, relevant tests, documentation, scope, and
   non-goals.
5. Determine the operating mode. Default to supervised mode when no mode was
   explicitly selected.

## Select operating mode

Treat `supervised mode`, `modalita supervisionata`, and `/dpcr super` as
supervised-mode triggers.

In supervised mode:

- explain significant actions and trade-offs first;
- ask before push, PR creation, finding fixes, and material scope, contract,
  architecture, or documentation decisions;
- prepare local changes and validation without altering shared GitHub state
  when that does not make a product decision.

Treat `autonomous PR loop`, `modalita autonoma PR loop`, and `/dpcr auto` as
autonomous-mode triggers.

In autonomous mode:

- perform regulated mechanical branch, issue, label, commit, push, PR, review,
  finding, reply, resolution, and PR-body updates without intermediate
  confirmation;
- loop over full review, one-finding-at-a-time fixes, validation, and
  traceability updates until two consecutive full reviews have no new findings;
- stop before merge;
- stop earlier for product choices, unclear contract changes, non-trivial CI
  failures, permission or secret limits, destructive operations, or work
  outside the agreed milestone.

## Maintain GitHub traceability

Use this chain for non-trivial roadmap work:

```text
Milestone -> parent issue -> child issue -> pull request -> commits
```

- Define a measurable milestone outcome, target date, dependencies, non-goals,
  parent issue, and primary roadmap.
- Keep the parent checklist and implementation traceability table synchronized.
- Create one child issue and normally one PR per coherent non-trivial step.
- Link children to the parent in both bodies and with native sub-issues when
  available.
- Put `Closes #CHILD` in the PR and `Refs #CHILD` in normal commit bodies.
- Apply at least one `area:*` and one `kind:*` label. Use priority and status
  labels only when meaningful.
- Record stable decisions in repository documentation, not only in Discussions,
  issue comments, or chat.

Do not invent milestones or parent issues for trivial editorial fixes.

## Commit atomically

Write commit subjects and bodies in English. Keep each commit single-scope and
include:

- the linked issue or review finding;
- problem and solution;
- meaningful entry-point/helper notes when call paths change;
- contract and documentation impact;
- validation commands and results;
- a final `Modified files:` list with adjacent list items.

Never include local databases, Telegram credentials, voice alias state, logs,
caches, generated artifacts, or unrelated user changes.

## Open and maintain pull requests

Open non-trivial PRs as drafts. Populate the repository PR template with:

- summary and motivation;
- child issue, parent issue, and milestone;
- scope and non-goals;
- concrete changes and affected contracts;
- function notes for meaningful runtime call paths;
- validation and documentation;
- residual risks and review history.

Update the body after every significant review round. Keep it in draft until two
consecutive complete reviews of the current full diff produce no new findings.
Any material code, test, or documentation fix resets the clean-review count.

## Review rigorously

Lead with findings ordered by severity. Review all applicable dimensions from
`docs/GITHUB_WORKFLOW.md`, with special attention to:

- queue lifecycle, replay, idempotency, and duplicate processing;
- session identifiers and approval deduplication;
- PTY process ownership, cleanup, cancellation, and bounded output;
- SQLite transactions, concurrency, migrations, and integrity;
- Telegram authorization, polling ownership, callbacks, and API failures;
- voice privacy, temporary files, language handling, and resource bounds;
- configuration precedence, secrets, safe defaults, tests, and docs.

Place actionable findings inline on the narrowest useful diff line. Include
current behavior, concrete risk, required fix, and regression protection. If no
findings remain, state residual risk and validation gaps.

## Resolve findings audibly

Prefer one fix commit per finding:

1. Explain the finding and planned correction first in supervised mode.
2. Implement the smallest complete fix and regression test or contract update.
3. Run targeted validation and inspect the resulting full diff.
4. Commit with PR and finding URLs in the body.
5. Push the commit.
6. Reply inline in English with a clickable commit link, risk, solution, and
   regression protection.
7. Resolve the thread only after the pushed fix and reply are visible.
8. Update the PR review round and parent traceability.

Never reply only with `fixed`. If one commit must resolve multiple findings,
explain why they are inseparable and link all of them.

## Finish at the merge boundary

Before reporting readiness, verify:

- all required local validation and CI checks are understood;
- all actionable threads are resolved;
- the PR body and linked issue state are current;
- two consecutive complete reviews are clean;
- the PR remains unmerged.

Report readiness and ask the maintainer whether to mark the PR ready or merge.
Do not merge without explicit maintainer instruction.
