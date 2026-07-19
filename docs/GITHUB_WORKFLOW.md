# GitHub Planning and PR Review Workflow

This document defines Durex's reusable GitHub workflow. It covers planning,
traceability, pull requests, supervised work, autonomous review loops, and the
finding lifecycle. It intentionally excludes Alfred-specific architecture,
runtime contracts, and test commands.

## Sources of truth

Each collaboration surface has one role:

| Surface | Purpose | Must not become |
| --- | --- | --- |
| GitHub milestone | Measurable outcome, due date, and visible progress | A detailed technical specification |
| GitHub Project | Current operational state across issues and PRs | The permanent history of decisions |
| Parent issue | Milestone execution plan, checklist, dependencies, and progress | The stable long-term contract |
| Child issue | One bug, micro-step, test gap, audit result, or documentation task | An unbounded project plan |
| Discussion | Open questions, alternatives, and trade-offs | A final decision or implementation contract |
| Pull request | A reviewable implementation of one coherent issue | A container for unrelated cleanup |
| Repository docs | Consolidated behavior, decisions, and rationale | A minute-by-minute status board |

The practical rule is:

```text
Discussion = live reasoning
Documentation = consolidated decision
Issue = work to perform
Pull request = verifiable change
```

## Traceability chain

Use this chain for non-trivial roadmap work:

```text
Milestone -> parent issue -> child issue -> pull request -> commits
```

Every link must be navigable in both directions:

- the parent issue links the milestone, primary roadmap, child issues, and PRs;
- every child issue links the parent and milestone;
- use GitHub's native sub-issue relationship when available;
- the PR uses `Closes #CHILD`, then links the parent and milestone;
- commit bodies use `Refs #CHILD` or link a review finding;
- the parent checklist and implementation traceability table stay in sync;
- stable documentation links back to the relevant issue or PR when historical
  context is useful.

Trivial typo fixes and emergency maintenance do not require an artificial
milestone or parent issue. They still require a scoped branch, clear commit,
validation, and a readable PR when they are reviewed through GitHub.

## Milestone lifecycle

Create a milestone only for an outcome that needs multiple coherent steps or
cross-cutting coordination. Before opening it, define:

- a stable name and measurable definition of done;
- an estimated start and target date;
- why the work is prioritized now;
- dependencies and explicit non-goals;
- the primary roadmap or design document;
- the parent issue that owns execution;
- what the milestone unlocks.

Update the target date and reason when scope, blockers, bugs, or review work
change the estimate. At closure, record the actual date, delivered outcome,
main issues and PRs, validation evidence, and deferred work. Update
`docs/ROADMAP.md` when the milestone changes Durex's public roadmap or stable
project direction.

## Parent issues

A parent issue is the operational plan for one milestone. It must contain:

- goal and user value;
- `Primary roadmap` immediately after the goal, with a clickable repository
  link and one sentence explaining why it is authoritative;
- scope and non-goals;
- dependencies and decisions already made;
- definition of done;
- ordered child-issue checklist;
- implementation traceability table;
- validation and documentation expectations;
- risks, blockers, and deferred work.

Recommended traceability columns are `Step`, `Child issue`, `PR`, `Commit`,
`Status`, and `Evidence`. Update the row when a child issue opens, a PR opens, a
fix lands, review state changes, or the step closes. Never mark the summary
checklist complete while its traceability row remains pending.

## Child issues

Create one child issue for each coherent non-trivial micro-step. A child issue
must be small enough for one PR and contain:

- parent issue and milestone links;
- problem and intended outcome;
- scope and non-goals;
- acceptance criteria;
- implementation notes only when already decided;
- tests and documentation to update;
- dependencies or blocking decisions;
- labels.

If implementation exposes a separate bug, contract decision, or unrelated
cleanup, create another child issue instead of expanding the current PR.

## Labels

Apply at least one area and one kind label to non-trivial issues. Create a
missing label only when it will be reused.

Suggested Durex taxonomy:

| Family | Purpose | Suggested labels |
| --- | --- | --- |
| `area:*` | Affected subsystem | `area:queue`, `area:runner`, `area:telegram`, `area:voice`, `area:config`, `area:docs`, `area:tests`, `area:security`, `area:ci` |
| `kind:*` | Type of work | `kind:feature`, `kind:bug`, `kind:design`, `kind:debt`, `kind:roadmap`, `kind:test`, `kind:audit`, `kind:docs` |
| `priority:*` | Urgency or blocking impact | `priority:p0`, `priority:p1`, `priority:p2` |
| `status:*` | Extra state not represented elsewhere | `status:needs-discussion`, `status:ready`, `status:blocked`, `status:needs-docs` |

Do not use priority or status labels as decoration. Issue state, draft state,
review status, and CI already communicate much of the workflow.

## Pull request contract

Normally create one PR per child issue and open non-trivial PRs as drafts. The
PR description must explain:

- summary and motivation;
- `Closes #CHILD`, parent issue, and milestone;
- scope and explicit non-goals;
- concrete changes and affected files;
- behavior, configuration, data, security, or compatibility contracts;
- meaningful function call paths for runtime changes;
- validation commands and results;
- documentation changes;
- residual risks and follow-up issues;
- review-round history.

Keep the PR body current. It is both the entry point for the reviewer and the
historical record of review decisions.

## Operating modes

The mode controls confirmations, not engineering quality. Both modes require
the same traceability, atomic commits, validation, review depth, and stop-before-
merge rule. If no mode is specified, use supervised mode.

### Supervised mode

Triggers:

```text
supervised mode
modalita supervisionata
/dpcr super
```

In this mode:

- explain significant actions and trade-offs before acting;
- ask before pushing, creating a PR, fixing a finding, or changing a material
  contract, scope, architecture decision, or documentation promise;
- explain each finding and proposed solution before applying it when the
  maintainer is actively directing the review;
- stop after major checkpoints when requested.

Local inspection, targeted validation, branch creation, and draft preparation
may proceed when they do not alter shared GitHub state or make a product
decision.

### Autonomous PR-loop mode

Triggers:

```text
autonomous PR loop
modalita autonoma PR loop
/dpcr auto
```

In this mode, perform regulated mechanical actions without intermediate
confirmation:

- create branch, milestone, parent or child issue when the agreed plan requires
  them;
- apply labels and native sub-issue links;
- implement, validate, commit, push, and open or update a draft PR;
- run detailed reviews and create actionable inline findings;
- fix one finding at a time with separate commits when practical;
- push fixes, reply inline, resolve threads, and update the PR body;
- repeat until two consecutive complete reviews produce no new findings.

Stop immediately and ask the maintainer when work requires:

- a product choice or scope expansion;
- a contract or architecture choice not derivable from current docs and code;
- a non-trivial CI diagnosis with multiple plausible fixes;
- elevated permissions, new secrets, destructive operations, or unavailable
  external services;
- work outside the agreed issue or milestone;
- merge. Autonomous mode never authorizes merge by itself.

## Review loop

Use this loop for every non-trivial PR:

1. Inspect the complete diff, linked issue, contracts, and changed call paths.
2. Check local validation and current CI status.
3. Review all applicable quality dimensions, not only the happy path.
4. Add actionable findings inline at the narrowest useful diff line.
5. Update the PR body with `Review round N` and reset the clean count if new
   findings exist.
6. Fix one finding per commit when practical, validate it, push it, reply
   inline with the commit link, and resolve the thread.
7. Review the complete resulting diff again. Do not review only the fix commit.
8. Repeat until two consecutive complete rounds produce no new findings.
9. Keep the PR in draft before that point. After two clean rounds, report that
   it is ready and ask the maintainer whether to mark it ready or merge it.

Any code, test, or material documentation change after a clean round resets the
consecutive clean count to zero. A review round is complete only when it covers
the current full diff and the relevant quality dimensions.

## Review quality dimensions

Check the dimensions that apply, and state which risks received special
attention:

- functional correctness and behavior-contract alignment;
- API, ownership, lifecycle, state-machine, and error-model clarity;
- accepted, rejected, boundary, regression, recovery, and failure tests;
- simple design, maintainability, and absence of speculative abstractions;
- performance and bounded memory, output, queues, retries, and file handles;
- reliability, idempotency, partial state, cleanup, cancellation, and shutdown;
- input validation, authorization, secret handling, privacy, and safe defaults;
- concurrency, SQLite transaction boundaries, races, and process ownership;
- deterministic identifiers, replay behavior, duplicate suppression, and
  stable output where required;
- compatibility, configuration precedence, versioning, and migration behavior;
- dependency necessity, maintenance, license, and supply-chain exposure;
- portability across supported Python and Linux environments;
- observability, actionable errors, degraded mode, and recovery;
- usability of CLI, Telegram controls, output, and documentation;
- data integrity and consistency across code, tests, docs, and examples;
- traceability among issue, PR, commit, finding, review round, and docs.

## Finding lifecycle

An actionable finding should identify one technical problem and include:

- severity and precise file/line context;
- current behavior;
- concrete risk or violated contract;
- required change;
- regression test or documentation expectation.

Prefer this chain:

```text
inline finding
-> single-scope fix commit
-> push
-> inline reply with clickable commit link and rationale
-> thread resolution
-> PR-body review update
```

The fix commit must link the PR and inline finding in its body. The reply must
explain the risk, solution, and regression protection in English. Never use a
bare `fixed` response. If one commit intentionally resolves multiple findings,
explain why they are inseparable and link every finding.

## CI and merge boundary

Review and local validation do not replace CI. Before declaring a PR ready:

- inspect all required checks and relevant logs;
- distinguish failures caused by the PR from infrastructure failures;
- record any validation that could not be run and its residual risk;
- verify all actionable threads are resolved;
- verify two consecutive full reviews are clean;
- verify the PR body, linked issues, checklist, and traceability table agree.

Only the maintainer decides whether and how to merge. Never force-push or merge
unless the maintainer explicitly requests it.

## Reusable templates

Use [GITHUB_TEMPLATES.md](GITHUB_TEMPLATES.md) for milestone, parent issue,
child issue, commit, PR, review round, inline finding, finding-fix commit,
finding reply, and parent-progress templates.

## Codex skill

The versioned workflow skill lives at
`skills/durex-pr-commit-rules/SKILL.md`. `AGENTS.md` directs repository-aware
agents to load it for commits, GitHub planning, PR work, and reviews.

To expose the skill by name to new Codex sessions on a Linux development
machine, link the versioned directory into the local skill directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$(pwd)/skills/durex-pr-commit-rules" \
  "${CODEX_HOME:-$HOME/.codex}/skills/durex-pr-commit-rules"
```

Run the command from the Durex repository root. Do not overwrite an existing
path without first checking whether it contains local changes. Start a new
Codex session after adding the link, then invoke `$durex-pr-commit-rules` or use
the textual mode triggers documented above.
