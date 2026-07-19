# GitHub Workflow Templates

These templates implement the workflow in
[GITHUB_WORKFLOW.md](GITHUB_WORKFLOW.md). Public GitHub content and commit
messages must be written in English. Replace placeholders and remove sections
that genuinely do not apply; do not leave template instructions in published
content.

## Milestone

```markdown
# MILESTONE_NAME

## Outcome

Describe the measurable user or engineering outcome.

## Why now

Explain the risk reduced or work unlocked by doing this now.

## Schedule

- Estimated start: YYYY-MM-DD
- Target date: YYYY-MM-DD
- Actual completion: pending

## Dependencies

- DEPENDENCY_OR_NONE

## Non-goals

- EXPLICITLY_EXCLUDED_WORK

## Definition of done

- [ ] MEASURABLE_RESULT
- [ ] Validation evidence is recorded.
- [ ] Stable documentation is updated.
- [ ] Deferred work is linked.

## Coordination

- Parent issue: #PARENT
- Primary roadmap: REPOSITORY_LINK
- Project: PROJECT_LINK_OR_NOT_USED
```

## Parent issue

```markdown
## Goal

Describe the milestone outcome and user value.

## Primary roadmap

[DOCUMENT_TITLE](REPOSITORY_DOCUMENT_URL)

This document is the main operational reference for the milestone because
REASON.

## Milestone

[MILESTONE_NAME](MILESTONE_URL), target YYYY-MM-DD.

## Scope

- INCLUDED_OUTCOME

## Non-goals

- EXCLUDED_OUTCOME

## Dependencies and decisions

- DEPENDENCY_OR_DECISION

## Definition of done

- [ ] MEASURABLE_RESULT
- [ ] Required validation passes.
- [ ] Documentation matches delivered behavior.
- [ ] Residual risks and deferred work are linked.

## Execution plan

- [ ] #CHILD_1 - STEP
- [ ] #CHILD_2 - STEP

## Implementation traceability

| Step | Child issue | PR | Commit | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| STEP | #CHILD | pending | pending | planned | pending |

## Validation

- COMMAND_OR_EVIDENCE

## Risks and blockers

- RISK_OR_NONE
```

## Child issue

```markdown
## Parent and milestone

- Parent issue: #PARENT
- Milestone: [MILESTONE_NAME](MILESTONE_URL)

## Problem

Describe the concrete problem or gap.

## Outcome

Describe the observable result of this micro-step.

## Scope

- INCLUDED_CHANGE

## Non-goals

- EXCLUDED_CHANGE

## Acceptance criteria

- [ ] CRITERION
- [ ] Targeted tests pass.
- [ ] User and system documentation are updated when behavior changes.

## Validation

- COMMAND_OR_SCENARIO

## Dependencies

- ISSUE_OR_NONE
```

## Commit

```text
<type>(<scope>): <imperative subject>

Refs #CHILD.

Problem:
Explain the gap or risk.

Solution:
Explain what changed and why this approach was chosen.

Function notes:
- ENTRY_POINT calls HELPER to RESPONSIBILITY.
- Omit this section for editorial or mechanically trivial changes.

Contracts and docs:
Explain observable, configuration, data, security, or documentation impact.

Validation:
- COMMAND: RESULT

Modified files:
- path/one
- path/two
```

## Pull request

```markdown
## Summary

- Explain what changes and why.

Closes #CHILD

- Parent issue: #PARENT
- Milestone: [MILESTONE_NAME](MILESTONE_URL)

## Scope

- [ ] Code
- [ ] Tests
- [ ] Documentation
- [ ] Build or CI

## What changed

- CONCRETE_CHANGE

## Contract

- Observable behavior: IMPACT_OR_NONE
- Configuration: IMPACT_OR_NONE
- Data and compatibility: IMPACT_OR_NONE
- Security and privacy: IMPACT_OR_NONE

## Function notes

- ENTRY_POINT -> HELPER -> OBSERVABLE_EFFECT
- Not applicable for documentation-only changes.

## Non-goals

- EXCLUDED_WORK

## Documentation

- UPDATED_DOCUMENT_OR_REASON_NOT_NEEDED

## Validation

- [ ] `git diff --check`
- [ ] TARGETED_COMMAND
- [ ] Full test suite when required
- [ ] CI checks

## Residual risks

- RISK_AND_MITIGATION_OR_NONE

## Review history

No review rounds recorded yet.
```

## Review round

```markdown
## Review round N

Summary:
- Explain the code paths and risk dimensions reviewed.
- Explain the main risk found or why no findings remain.

Clean review status:
- New findings: yes/no.
- Consecutive clean reviews: 0/1/2.
- Draft status: keep draft / ready after two clean reviews.

Findings:
- Finding: FINDING_URL
  Fix: [SHORT_SHA](COMMIT_URL) - Explain why the fix closes the finding.
- None.

Validation:
- COMMAND_OR_CI_CHECK: RESULT
```

Do not write `Review update #1`; GitHub turns `#1` into an unrelated issue or
PR link.

## Inline finding

```markdown
**SEVERITY: concise finding title**

Current behavior:
Explain what the changed code does.

Risk:
Explain the concrete failure, violated contract, or regression.

Required change:
Explain the smallest acceptable correction.

Regression protection:
Name the test, assertion, or documentation contract that must be added or
updated.
```

## Finding fix commit

```text
fix(<scope>): <imperative subject>

Fixes review finding:
- PR: PR_URL
- Finding: FINDING_URL

Risk:
Explain why the reviewed behavior was unsafe or incorrect.

Solution:
Explain how this commit closes the finding without expanding scope.

Regression protection:
- TEST_OR_CONTRACT

Validation:
- COMMAND: RESULT

Modified files:
- path/one
- path/two
```

## Inline finding reply

```markdown
Fixed in [SHORT_SHA](COMMIT_URL).

The finding exposed RISK. The fix now SOLUTION. REGRESSION_TEST_OR_CONTRACT
prevents the behavior from returning.
```

## Parent progress update

```markdown
## Progress update - YYYY-MM-DD

- Child issue: #CHILD
- Pull request: #PR
- Result: DELIVERED_RESULT
- Review state: FINDINGS_OPEN / CLEAN_ROUND_1 / CLEAN_ROUND_2
- Validation: COMMAND_OR_CI_RESULT
- Documentation: DOCUMENT_LINK_OR_NOT_APPLICABLE
- Residual risk or blocker: DETAILS_OR_NONE

The execution checklist and implementation traceability table have been updated
to match this state.
```

## Clean review comment

```markdown
Review round N completed with no new findings.

- Consecutive clean reviews: COUNT/2
- Required checks: STATUS
- Unresolved actionable threads: COUNT
- Residual risk: DETAILS_OR_NONE
- Draft status: KEEP_DRAFT_OR_READY
```
