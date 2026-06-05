# User Guide

This guide explains how to use Durex from the command line.

It is written for someone who wants to run real work, not only understand the
architecture. For system internals, start from [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md).

---

## What Durex does

Durex is a local task queue for Codex CLI.

You add tasks to a SQLite queue, then start a worker. The worker runs Codex on
each task, records output, retries failures when allowed, waits through usage
limits, and can use a PTY runner to answer approval prompts.

The most common workflows are:

- queue several Codex tasks and run them unattended;
- run simple non-interactive tasks with the default subprocess runner;
- run interactive tasks with the PTY runner;
- approve Codex prompts from Telegram;
- operate the queue from Telegram with `/status`, `/tasks`, `/add`, `/run`,
  `/tail`, and `/stop`;
- prepare overnight work and inspect the results later.

---

## Requirements

You need:

- Python 3.10 or newer;
- Codex CLI installed and authenticated;
- a project directory where Codex can run;
- optional: Telegram bot token and chat id for approvals or remote control.

Check Python:

```bash
python3 --version
```

Check Codex:

```bash
codex --help
```

---

## First run

Initialize the local SQLite database:

```bash
python3 codex_queue.py init
```

Add one task:

```bash
python3 codex_queue.py add \
  --title "Inspect project" \
  --workdir /path/to/project \
  --priority 100 \
  --prompt "Read the project, run the tests if available, and write a short summary of what you found."
```

Show the queue:

```bash
python3 codex_queue.py list
```

Run the worker with the default runner:

```bash
python3 codex_queue.py run --stop-when-empty
```

`--stop-when-empty` is useful for manual runs because the worker exits after no
task is ready. Without it, the worker keeps polling.

---

## Command reference

### `init`

Creates the SQLite task database if it does not already exist.

```bash
python3 codex_queue.py init
```

Use it once before adding tasks. It is safe to run again.

### `add`

Adds one task to the queue.

```bash
python3 codex_queue.py add \
  --title "Fix failing tests" \
  --workdir /path/to/project \
  --priority 10 \
  --max-attempts 3 \
  --prompt "Run the test suite, fix the failures, and summarize the changes."
```

Options:

| Option | Required | Meaning |
|---|---:|---|
| `--title` | yes | Human-readable task title |
| `--prompt` | yes | Instruction passed to Codex |
| `--workdir` | no | Directory where Codex runs. Default: current directory |
| `--priority` | no | Lower numbers run earlier. Default: `100` |
| `--max-attempts` | no | Maximum attempts before permanent failure. Default: `3` |

Priority examples:

```text
1      urgent
10     important
100    normal
999    low priority
```

### `seed`

Adds example tasks for learning and testing.

```bash
python3 codex_queue.py seed --workdir /path/to/project
```

Use `seed` when you want to see the queue work without designing your own tasks.
For production work, prefer explicit `add` commands.

### `list`

Prints the current queue.

```bash
python3 codex_queue.py list
```

The output includes task id, title, status, priority, attempts, reset time, and
working directory.

### `run`

Starts the local worker.

```bash
python3 codex_queue.py run
```

Useful options:

| Option | Meaning |
|---|---|
| `--interval SECONDS` | Polling interval when no task is ready. Default: `60` |
| `--stop-when-empty` | Exit when no task is ready |
| `--runner-mode subprocess` | Use the default non-interactive runner |
| `--runner-mode pty` | Use the interactive PTY runner |
| `--telegram` | Enable Telegram approvals in PTY mode |
| `--telegram-verbosity compact` | Send shorter approval messages |
| `--telegram-verbosity normal` | Default Telegram message detail |
| `--telegram-verbosity verbose` | Include more terminal context |
| `--no-echo` | Do not mirror PTY output to local stdout |

Examples:

```bash
python3 codex_queue.py run --stop-when-empty
python3 codex_queue.py run --runner-mode pty --stop-when-empty
python3 codex_queue.py run --runner-mode pty --telegram
```

### `telegram-check`

Validates Telegram bot setup.

Check that the bot token works:

```bash
export DUREX_TELEGRAM_BOT_TOKEN="your-bot-token"
python3 codex_queue.py telegram-check
```

Discover chat ids from recent bot updates:

```bash
python3 codex_queue.py telegram-check --discover-chat-id
```

Send a test message to the configured chat:

```bash
export DUREX_TELEGRAM_CHAT_ID="the-chat-id"
python3 codex_queue.py telegram-check --send-test
```

Optional flags:

| Option | Meaning |
|---|---|
| `--discover-chat-id` | Poll recent updates and print candidate chat ids |
| `--send-test` | Send a test message to `DUREX_TELEGRAM_CHAT_ID` |
| `--message TEXT` | Test message text |
| `--poll-timeout SECONDS` | Wait for updates while discovering chat ids |

### `telegram-control`

Starts the Telegram remote-control daemon.

```bash
python3 codex_queue.py telegram-control --allowed-workdir /path/to/projects
```

Useful options:

| Option | Meaning |
|---|---|
| `--allowed-workdir PATH` | Allowed root for remote `/add --workdir`. Can be repeated |
| `--runner-mode subprocess` | Runner used when Telegram `/run` starts the worker |
| `--runner-mode pty` | Default remote-control runner |
| `--echo-output` | Mirror worker PTY output locally |
| `--worker-telegram-approvals` | Reserved for future shared dispatcher; currently rejected |

Remote control uses Telegram text commands, not shell input. It can operate the
queue, but it cannot type arbitrary commands into Codex.

---

## Runner modes

### Subprocess runner

Subprocess mode is the default:

```bash
python3 codex_queue.py run --runner-mode subprocess
```

Use it when:

- the task is non-interactive;
- you do not expect Codex to ask live approval questions;
- you want the simplest execution path;
- output after process exit is enough.

Subprocess mode runs Codex with `subprocess.run()`. Durex sees the final output
and return code only after Codex exits.

### PTY runner

PTY mode runs Codex inside a pseudo-terminal:

```bash
python3 codex_queue.py run --runner-mode pty
```

Use it when:

- Codex may show interactive approval prompts;
- you want Durex to detect prompts while Codex is still running;
- you want policy-based auto-allow or auto-deny;
- you want Telegram approvals.

PTY mode reads terminal output incrementally. When an approval prompt is
detected, Durex can write `y\n`, write `n\n`, ask Telegram, or stop the task.

Details: [PTY_VS_EVENTS.md](PTY_VS_EVENTS.md)

---

## Telegram approvals

Telegram approvals let you approve Codex prompts from your phone.

### 1. Create the bot

Open Telegram and talk to `@BotFather`:

1. send `/newbot`;
2. choose a display name;
3. choose a username ending in `bot`;
4. copy the token returned by BotFather.

Export the token:

```bash
export DUREX_TELEGRAM_BOT_TOKEN="your-bot-token"
```

### 2. Validate the token

```bash
python3 codex_queue.py telegram-check
```

### 3. Discover the chat id

Send any message to your bot from the Telegram chat you want to authorize.

Then run:

```bash
python3 codex_queue.py telegram-check --discover-chat-id
```

Export the printed chat id:

```bash
export DUREX_TELEGRAM_CHAT_ID="the-chat-id-from-the-check"
```

Private chat ids are usually positive. Group and supergroup ids are often
negative. Use the exact value printed by the check command.

### 4. Send a test message

```bash
python3 codex_queue.py telegram-check --send-test
```

If the message arrives in Telegram, approval routing is ready.

### 5. Run with Telegram approvals

```bash
python3 codex_queue.py run --runner-mode pty --telegram
```

When Codex asks for approval, Telegram buttons can:

| Button | Effect |
|---|---|
| Approve | Writes positive input to Codex |
| Deny | Writes negative input to Codex |
| Show context | Sends more terminal context and keeps waiting |
| Stop task | Terminates the current task process |

Details: [TELEGRAM_APPROVALS.md](TELEGRAM_APPROVALS.md)

---

## Telegram remote control

Telegram remote control lets you operate the Durex queue from your phone.

It is separate from Telegram approvals:

- approvals answer Codex prompts;
- remote control sends queue commands to Durex.

### 1. Configure Telegram

Use the same bot setup from the approval section:

```bash
export DUREX_TELEGRAM_BOT_TOKEN="your-bot-token"
export DUREX_TELEGRAM_CHAT_ID="your-chat-id"
python3 codex_queue.py telegram-check --send-test
```

### 2. Start the daemon

Allow one project root:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /path/to/projects
```

Allow multiple roots:

```bash
python3 codex_queue.py telegram-control \
  --allowed-workdir /path/to/project-a \
  --allowed-workdir /path/to/project-b
```

Or configure roots through the environment:

```bash
export DUREX_TELEGRAM_ALLOWED_WORKDIRS="/path/to/project-a:/path/to/project-b"
python3 codex_queue.py telegram-control
```

### 3. Use Telegram commands

```text
/status
/tasks
/tasks 20
/tail
/tail 42
/run
/stop
```

Add a task remotely:

```text
/add --title "Fix tests" --workdir /path/to/projects/my-repo --priority 10
Run the tests, fix the failures, and summarize the changes.
```

Start the worker:

```text
/run
```

Show output from the latest task:

```text
/tail
```

Request a graceful stop:

```text
/stop
```

`/stop` does not forcibly kill the current Codex process. It asks the background
worker to stop before starting another task.

Details: [TELEGRAM_REMOTE_CONTROL.md](TELEGRAM_REMOTE_CONTROL.md)

---

## Common workflows

### Workflow 1: run one simple task

Use this for a small non-interactive job.

```bash
python3 codex_queue.py init

python3 codex_queue.py add \
  --title "Summarize project" \
  --workdir /path/to/project \
  --prompt "Read the project and create project_summary.md."

python3 codex_queue.py run --stop-when-empty
```

### Workflow 2: run a prioritized queue

Use this when you have multiple tasks and want urgent ones first.

```bash
python3 codex_queue.py add \
  --title "Fix production bug" \
  --workdir /path/to/project \
  --priority 1 \
  --prompt "Investigate the failing production test and propose a minimal fix."

python3 codex_queue.py add \
  --title "Improve docs" \
  --workdir /path/to/project \
  --priority 100 \
  --prompt "Review the README and improve unclear setup instructions."

python3 codex_queue.py list
python3 codex_queue.py run --stop-when-empty
```

### Workflow 3: interactive task with local PTY approvals

Use this when Codex may ask approval questions and you are near the computer.

```bash
python3 codex_queue.py add \
  --title "Fix tests interactively" \
  --workdir /path/to/project \
  --prompt "Run tests, inspect failures, and fix them."

python3 codex_queue.py run --runner-mode pty --stop-when-empty
```

### Workflow 4: interactive task with Telegram approvals

Use this when you want to leave the computer but still approve prompts.

```bash
export DUREX_TELEGRAM_BOT_TOKEN="your-bot-token"
export DUREX_TELEGRAM_CHAT_ID="your-chat-id"

python3 codex_queue.py add \
  --title "Overnight test fix" \
  --workdir /path/to/project \
  --prompt "Run the full test suite, fix failures, and summarize all changes."

python3 codex_queue.py run --runner-mode pty --telegram
```

### Workflow 5: overnight queue

Use this when you want to prepare multiple tasks and review results later.

```bash
python3 codex_queue.py init

python3 codex_queue.py add \
  --title "Grade student A" \
  --workdir /path/to/student-a \
  --priority 10 \
  --prompt "Run tests, evaluate the assignment, and create report_A.md."

python3 codex_queue.py add \
  --title "Grade student B" \
  --workdir /path/to/student-b \
  --priority 20 \
  --prompt "Run tests, evaluate the assignment, and create report_B.md."

nohup python3 codex_queue.py run --runner-mode pty --telegram > durex.log 2>&1 &
```

Watch local output:

```bash
tail -f durex.log
```

Inspect queue state later:

```bash
python3 codex_queue.py list
```

### Workflow 6: operate the queue from Telegram

Use this when Durex is running on a machine you do not want to keep checking.

Start the daemon:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /path/to/projects
```

From Telegram:

```text
/status
/add --title "Review docs" --workdir /path/to/projects/durex --priority 50
Review the documentation and list unclear sections.
/run
/tail
```

---

## Statuses and retries

Tasks move through these statuses:

| Status | Meaning |
|---|---|
| `PENDING` | Ready to run |
| `RUNNING` | Currently selected by the worker |
| `WAITING_LIMIT` | Blocked until a usage-limit reset time |
| `COMPLETED` | Codex exited successfully |
| `FAILED` | No more retries or local runner error |

When a task fails with a non-zero return code, Durex retries it while attempts
remain. When it sees usage-limit output, it moves the task to `WAITING_LIMIT`
and schedules a later retry.

If Codex output includes a session id, Durex stores it and uses `codex exec
resume` on a later attempt.

Details:

- [SEQUENCE_DIAGRAMS.md - Usage limit reached](SEQUENCE_DIAGRAMS.md#2-usage-limit-reached)
- [SEQUENCE_DIAGRAMS.md - Automatic resume after reset_at](SEQUENCE_DIAGRAMS.md#3-automatic-resume-after-reset_at)

---

## Inspecting the database

The default database file is:

```text
codex_tasks.db
```

Open it with SQLite:

```bash
sqlite3 codex_tasks.db
```

Useful queries:

```sql
.mode column
.headers on

SELECT id, title, status, priority, attempts, reset_at, workdir
FROM tasks
ORDER BY id;

SELECT id, title, last_error
FROM tasks
WHERE status = 'FAILED';
```

Exit SQLite:

```sql
.quit
```

---

## Troubleshooting

### `codex: command not found`

Codex CLI is missing or not in `PATH`.

Check:

```bash
which codex
codex --help
```

### The worker keeps running after the queue is empty

Use:

```bash
python3 codex_queue.py run --stop-when-empty
```

Without `--stop-when-empty`, the worker keeps polling for future tasks.

### Telegram token is missing

Set:

```bash
export DUREX_TELEGRAM_BOT_TOKEN="your-bot-token"
```

Then run:

```bash
python3 codex_queue.py telegram-check
```

### Telegram chat id is missing or wrong

Send a message to the bot, then run:

```bash
python3 codex_queue.py telegram-check --discover-chat-id
```

Set:

```bash
export DUREX_TELEGRAM_CHAT_ID="the-printed-chat-id"
```

Then verify:

```bash
python3 codex_queue.py telegram-check --send-test
```

### A task stays in `WAITING_LIMIT`

Check the reset time:

```bash
python3 codex_queue.py list
```

The task becomes runnable again only after `reset_at` has passed.

### PTY prompts are not detected

Use verbose local output first:

```bash
python3 codex_queue.py run --runner-mode pty
```

If Telegram is enabled, try verbose Telegram messages:

```bash
python3 codex_queue.py run --runner-mode pty --telegram --telegram-verbosity verbose
```

Prompt detection is based on terminal text. Some prompts may need detector
updates if their wording is unusual.

### Telegram remote control rejects `/add`

Check that the requested workdir is inside an allowed root:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /path/to/projects
```

Then use a workdir below that root:

```text
/add --title "Fix tests" --workdir /path/to/projects/my-repo
Run tests and fix failures.
```

---

## Safety notes

Durex runs local Codex tasks. Treat prompts as commands that may affect your
filesystem, repositories, dependencies, or external services.

Recommended practices:

- use separate workdirs for unrelated projects;
- review Telegram approvals carefully;
- keep bot tokens out of Git;
- restrict `DUREX_TELEGRAM_CHAT_ID` to your intended chat;
- use remote control only with narrow `--allowed-workdir` roots;
- prefer PTY plus Telegram when leaving long-running tasks unattended;
- inspect results before pushing or merging generated changes.

---

## Where to go next

- System map: [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md)
- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)
- Runtime sequences: [SEQUENCE_DIAGRAMS.md](SEQUENCE_DIAGRAMS.md)
- Runner design: [PTY_VS_EVENTS.md](PTY_VS_EVENTS.md)
- Telegram approvals: [TELEGRAM_APPROVALS.md](TELEGRAM_APPROVALS.md)
- Telegram remote control: [TELEGRAM_REMOTE_CONTROL.md](TELEGRAM_REMOTE_CONTROL.md)
