# Operating Rules

This document records the working rules to follow in future Durex sessions.

Use it before starting a new change, review, documentation pass, or pull request.
The goal is to keep work traceable, reviewable, and consistent across sessions.

---

## Session start

At the start of a session:

1. check the current branch;
2. check whether the working tree is clean;
3. identify whether the task belongs on an existing branch or a new branch;
4. read the relevant files before changing them;
5. prefer small scoped changes over broad unrelated edits.

Useful commands:

```bash
git status --short --branch
git branch --show-current
git log --oneline --max-count=10
```

If the working tree contains unrelated changes, leave them alone. Do not revert
or overwrite changes that were not part of the current task.

---

## Branch rules

Use a dedicated branch for each coherent area of work.

Examples:

```text
docs-fix-telegram-setup
docs-code-comments-sphinx
feature-telegram-remote-control
fix-approval-dedup
```

Branch naming should describe the work, not the implementation detail alone.

Start a new branch when:

- the current PR is done or ready to close;
- the new task changes a different area;
- the work should be reviewed separately;
- the user explicitly asks to separate the work.

Stay on the current branch when:

- the task fixes findings from the current PR review;
- the task updates the same documentation or feature scope;
- the change is a direct follow-up to the current branch.

---

## Commit rules

Commits should be in English.

Use a short subject plus a detailed body with these sections:

```text
Summary:
...

What changed:
- ...

Added files:
- ...

Modified files:
- ...

Validation:
- ...
```

Only include `Added files` when files were added.

Commit only the files related to the current task. Do not include generated
cache files, unrelated local changes, or accidental artifacts.

Before committing:

```bash
git status --short --branch
git diff --stat
git diff --check
```

After committing:

```bash
git status --short --branch
git log -1 --stat --format=fuller
```

---

## Pull request rules

Open or update a PR only after the relevant branch has been pushed.

PR descriptions should include:

- a concise summary;
- what changed;
- added files;
- modified files;
- commit list when useful;
- validation performed;
- whether the change is documentation-only, code-only, or mixed.

When updating a PR after new commits, add an explicit update section instead of
silently replacing context.

---

## Review rules

A review should be strict and concrete.

Lead with findings, ordered by severity. Each finding should include:

- file and line reference;
- what is wrong;
- why it matters;
- what should change.

For documentation PRs, review:

- whether the docs match the current code;
- whether diagrams render and have readable text;
- whether Mermaid nodes and edges are explained;
- whether links and anchors resolve;
- whether future/planned behavior is clearly marked as future;
- whether setup steps are complete and executable;
- whether examples match the CLI.

For code PRs, review:

- behavior changes;
- edge cases;
- error handling;
- security boundaries;
- test coverage;
- regression risks;
- maintainability.

If there are no findings, say that clearly and mention residual risk or test
gaps.

---

## Documentation rules

Documentation should be written so a reader can understand the system before
reading the code.

When adding or changing diagrams:

- use readable Mermaid theme settings;
- explain every important node;
- explain every important edge trigger;
- distinguish current behavior from planned behavior;
- link to deeper documents when details are elsewhere.

For user-facing docs:

- prefer step-by-step workflows;
- include exact commands;
- include expected effects;
- include troubleshooting;
- keep secrets out of examples;
- avoid implying unsupported behavior.

For system docs:

- explain responsibilities by module;
- explain data flow;
- explain lifecycle states and transitions;
- explain security boundaries;
- link to tests or validation where useful.

---

## CLI documentation rules

When changing `codex_queue.py` CLI commands or flags, update
[USER_GUIDE.md](USER_GUIDE.md).

Then run:

```bash
python3 scripts/check_cli_docs.py
```

The check verifies that the user guide mentions every current top-level command
and long option exposed by `argparse`.

See [CLI_DOC_AUTOMATION.md](CLI_DOC_AUTOMATION.md) for the roadmap toward
generated CLI reference documentation.

---

## Validation rules

Run the smallest validation that proves the change.

For documentation-only changes:

```bash
git diff --check
python3 scripts/check_cli_docs.py
```

Run the CLI documentation check only when the user guide or CLI-related docs are
affected.

For Python code changes:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile approval_detector.py approval_policy.py pty_runner.py telegram_bridge.py telegram_control.py codex_queue.py
```

If `pytest` is available:

```bash
python3 -m pytest -q
```

For Telegram-related changes, prefer tests that do not require network access.
Live Telegram validation should be explicitly called out when it was not run.

---

## Safety rules

Never commit secrets.

Do not commit:

- Telegram bot tokens;
- chat ids unless they are fake examples;
- local database files;
- logs with sensitive output;
- `__pycache__` or compiled Python files;
- unrelated local edits.

When documenting Telegram, always mention:

- `DUREX_TELEGRAM_BOT_TOKEN`;
- `DUREX_TELEGRAM_CHAT_ID`;
- authorized chat boundaries;
- long-polling constraints when approvals and remote control interact.

---

## Communication rules

User-facing status updates should be concise and concrete.

When running commands, explain what is being checked and why.

When a command fails, report:

- command intent;
- failure reason;
- whether it blocks the task;
- next step.

Final responses should include:

- what changed;
- files added or modified;
- validation performed;
- commit hash when a commit was created;
- PR link when a PR was opened or updated.

---

## Future code-comment branch

For the planned code-comment and Sphinx work, use a separate branch.

Suggested branch:

```text
docs-code-comments-sphinx
```

Recommended first steps:

1. audit current docstrings in all Python modules;
2. choose a docstring style before editing;
3. add or improve docstrings for public functions, dataclasses, and classes;
4. avoid excessive comments for obvious code;
5. add comments only where they explain non-obvious behavior or constraints;
6. add Sphinx configuration after docstring style is stable;
7. generate API docs and fix warnings.

The goal is not to comment every line. The goal is to make the public API,
module responsibilities, and non-obvious control flow clear enough for Sphinx and
future maintainers.
