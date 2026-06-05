# CLI Documentation Automation

This document explains how Durex keeps the user guide aligned with the current
command-line interface.

The current implementation is intentionally simple: it verifies that
`docs/USER_GUIDE.md` mentions every top-level command and every long option
exposed by `codex_queue.py`.

---

## Current check

Run:

```bash
python3 scripts/check_cli_docs.py
```

The script:

1. runs `python3 codex_queue.py --help`;
2. discovers top-level argparse subcommands;
3. runs `python3 codex_queue.py <command> --help` for each subcommand;
4. extracts long options such as `--runner-mode` and `--telegram`;
5. reads `docs/USER_GUIDE.md`;
6. fails if a discovered command or option is missing from the guide.

Successful output looks like:

```text
CLI documentation check passed.
Commands checked: init, add, seed, list, run, telegram-check, telegram-control
- init: no long options
- add: --max-attempts, --priority, --prompt, --title, --workdir
- seed: --workdir
- list: no long options
- run: --interval, --no-echo, --runner-mode, --stop-when-empty, --telegram, --telegram-verbosity
- telegram-check: --discover-chat-id, --message, --poll-timeout, --send-test
- telegram-control: --allowed-workdir, --echo-output, --runner-mode, --telegram-verbosity, --worker-telegram-approvals
```

Failure output lists the missing command or option:

```text
CLI documentation drift detected in docs/USER_GUIDE.md:
- missing run: `--no-echo`
```

---

## What this catches

This check catches the most common documentation drift:

- a new subcommand was added but not documented;
- a subcommand was renamed;
- a new long option was added but not documented;
- an option was renamed and the guide still has the old name;
- Telegram-specific flags changed but the user guide was not updated.

It does not judge whether the guide explains an option well. It only verifies
that the option is mentioned.

---

## What this deliberately does not generate

The user guide is meant to be readable. It contains workflows, context, examples,
safety notes, and troubleshooting. Raw `argparse` output is not a good
replacement for that.

For this reason the current script checks coverage instead of generating guide
content.

---

## Recommended validation workflow

Before merging a PR that changes CLI behavior or user-facing documentation, run:

```bash
python3 scripts/check_cli_docs.py
python3 -m unittest discover -s tests -v
python3 -m py_compile approval_detector.py approval_policy.py pty_runner.py telegram_bridge.py telegram_control.py codex_queue.py
```

If `pytest` is installed:

```bash
python3 -m pytest -q
```

---

## Roadmap

### Step 1: coverage check

Current state.

`scripts/check_cli_docs.py` checks that `docs/USER_GUIDE.md` mentions all
commands and long options discovered from argparse.

### Step 2: generated CLI reference file

Add a generator such as:

```bash
python3 scripts/generate_cli_reference.py > docs/CLI_REFERENCE.md
```

The generated document should contain:

- top-level usage;
- one section per subcommand;
- raw usage text;
- option tables;
- links back to `docs/USER_GUIDE.md` for workflows.

This keeps `USER_GUIDE.md` human-written while giving maintainers an exact
reference generated from argparse.

### Step 3: generated section with markers

If a separate file is not enough, generate a bounded section inside
`docs/USER_GUIDE.md`:

```md
<!-- BEGIN GENERATED CLI REFERENCE -->
...
<!-- END GENERATED CLI REFERENCE -->
```

The generator should replace only the marked section. Everything else in the user
guide should stay hand-written.

### Step 4: CI enforcement

Add CI steps that run:

```bash
python3 scripts/check_cli_docs.py
git diff --check
```

If generated reference output is added later, CI should also verify that running
the generator does not change the working tree.

---

## Design constraints for future generators

Future CLI-reference generation should:

- import or execute the same argparse definitions used by `codex_queue.py`;
- avoid duplicating command metadata by hand;
- keep generated output deterministic;
- preserve readable human-written workflow docs;
- fail loudly when `argparse` output changes;
- avoid requiring network access;
- avoid mutating the database or running Codex.
