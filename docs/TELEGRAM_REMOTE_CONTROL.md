# Telegram Remote Control

This document describes the first Telegram remote-control mode for Durex.

Remote control is separate from Telegram approvals. Approval mode only answers
Codex confirmation prompts. Remote-control mode accepts Telegram messages that
operate the Durex queue and worker.

## Current Scope

The first implementation supports safe queue control:

- show queue and worker status;
- list recent tasks;
- add a new Codex task;
- start the Durex worker until the queue is empty;
- show the tail of task output;
- request that the worker stops before starting another task.

It does not execute arbitrary shell input from Telegram. Direct live control of
Codex terminal input is intentionally left for a future policy layer, such as
Alfred.

## Starting Remote Control

Create a Telegram bot and set the credentials:

1. Open `@BotFather` in Telegram.
2. Send `/newbot`.
3. Choose a display name.
4. Choose a username ending in `bot`, for example `my_durex_bot`.
5. Copy the token returned by BotFather.

```bash
export DUREX_TELEGRAM_BOT_TOKEN="your-bot-token"
```

Validate the token:

```bash
python3 codex_queue.py telegram-check
```

Send any message to the bot from the Telegram chat that should control Durex,
then discover and validate the chat id:

```bash
python3 codex_queue.py telegram-check --discover-chat-id
export DUREX_TELEGRAM_CHAT_ID="the-chat-id-from-the-check"
python3 codex_queue.py telegram-check --send-test
```

Private chat ids are usually positive integers. Group and supergroup ids are
often negative. Use exactly the value printed by `telegram-check`.

Official Telegram references:

- Bot overview: https://core.telegram.org/bots
- BotFather guide: https://core.telegram.org/bots/features#botfather
- Bot API reference: https://core.telegram.org/bots/api
- Bot API tutorial: https://core.telegram.org/bots/tutorial
- `getMe`: https://core.telegram.org/bots/api#getme
- `getUpdates`: https://core.telegram.org/bots/api#getupdates
- `sendMessage`: https://core.telegram.org/bots/api#sendmessage

Start the remote-control daemon:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /path/to/projects
```

Multiple allowed roots can be configured:

```bash
python3 codex_queue.py telegram-control \
  --allowed-workdir /path/to/project-a \
  --allowed-workdir /path/to/project-b
```

You can also use an environment variable:

```bash
export DUREX_TELEGRAM_ALLOWED_WORKDIRS="/path/to/project-a:/path/to/project-b"
python3 codex_queue.py telegram-control
```

If no allowed root is provided, Durex allows only the current working directory.

## Telegram Commands

### `/status`

Shows worker state and task counts by status.

```text
/status
```

### `/tasks`

Lists recent tasks. The optional limit defaults to 10.

```text
/tasks
/tasks 20
```

### `/add`

Adds a new Codex task.

Syntax:

```text
/add --title "Fix tests" --workdir /path/to/project --priority 10
Run the tests, fix failures, and summarize the changes.
```

Options:

| Option | Meaning |
|---|---|
| `--title` | Human-readable task title. Optional. |
| `--workdir` | Working directory for Codex. Must be inside an allowed root. |
| `--priority` | Queue priority. Lower values run first. Default: `100`. |
| `--max-attempts` | Maximum retry attempts. Default: `3`. |

The prompt must be placed on the lines after the `/add` header.

### `/run`

Starts a background Durex worker. The worker runs ready tasks until the queue is
empty or no task can currently run.

```text
/run
```

The command is idempotent while the worker is already running.

### `/tail`

Shows the output tail for the latest task, or for a specific task id.

```text
/tail
/tail 42
```

### `/stop`

Requests that the background worker stops before starting another task.

```text
/stop
```

This does not forcibly kill the currently running Codex process. Hard process
control should be added only with stronger policy and audit support.

## Worker Options

Remote-control mode can start workers using either runner:

```bash
python3 codex_queue.py telegram-control --runner-mode subprocess
python3 codex_queue.py telegram-control --runner-mode pty
```

Existing Telegram approval prompts can be enabled for worker tasks:

```bash
python3 codex_queue.py telegram-control \
  --allowed-workdir /path/to/project \
  --worker-telegram-approvals
```

This uses the existing approval bridge. It is still an approval channel, not a
free-form command channel.

## Security Boundaries

Remote control currently enforces these boundaries:

1. Only `DUREX_TELEGRAM_CHAT_ID` can issue commands.
2. `/add --workdir` must stay inside an allowed root.
3. Telegram commands operate Durex queue APIs, not the shell.
4. Output sent back to Telegram is truncated to fit Telegram message limits.
5. Direct PTY input from Telegram is not implemented.

Future Alfred integration should own the higher-risk policy decisions for live
Codex control, command authorization, hard process stops and arbitrary input.

## Implementation Notes

The remote-control daemon uses Telegram long polling for `message` updates. It
shares the same Bot API client class as Telegram approvals, but command routing
lives in `telegram_control.py`.

The command router is tested without network access using a fake bridge.
