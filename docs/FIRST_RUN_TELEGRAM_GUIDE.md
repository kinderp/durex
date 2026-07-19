# First Setup and Telegram Usage Guide

This guide is the recommended first-read document for configuring Durex from a
fresh checkout and using it from Telegram.

It focuses on practical setup, first smoke tests, command syntax, voice
commands, and troubleshooting. For deeper design details, follow the links in
each section.

Related documentation:

- [USER_GUIDE.md](USER_GUIDE.md) for the complete CLI user guide.
- [TELEGRAM_REMOTE_CONTROL.md](TELEGRAM_REMOTE_CONTROL.md) for the Telegram
  remote-control architecture.
- [TELEGRAM_APPROVALS.md](TELEGRAM_APPROVALS.md) for Codex approval prompts
  over Telegram.
- [TELEGRAM_UPDATE_DISPATCHER.md](TELEGRAM_UPDATE_DISPATCHER.md) for shared
  polling, callback routing, and worker approvals.
- [TELEGRAM_VOICE_COMMANDS.md](TELEGRAM_VOICE_COMMANDS.md) for local
  speech-to-text command parsing.
- [CONFIGURATION.md](CONFIGURATION.md) for YAML and environment configuration.
- [OPERATING_RULES.md](OPERATING_RULES.md) for safe operating expectations.

---

## 1. Prerequisites

Install or verify:

- Python 3.10 or newer.
- Codex CLI installed and authenticated.
- A local project directory where Codex can work.
- A Telegram account.
- Optional for voice commands: the dependencies in `requirements-voice.txt`.

Check Python:

```bash
python3 --version
```

Check Codex:

```bash
codex --help
```

Initialize the Durex queue database:

```bash
python3 codex_queue.py init
```

Show the CLI help:

```bash
python3 codex_queue.py --help
```

---

## 2. Optional Python Environment

Durex can run with the system Python when only standard-library features are
used. Voice commands need optional packages.

On a normal filesystem:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-voice.txt
```

On Vagrant or shared filesystems, `venv` can fail while creating `lib64`
symlinks. Use copied files instead:

```bash
python3 -m venv --copies .venv
. .venv/bin/activate
pip install -r requirements-voice.txt
```

If the shared filesystem still rejects the virtual environment, place it outside
the repository:

```bash
python3 -m venv /tmp/durex-venv
. /tmp/durex-venv/bin/activate
pip install -r /lab/durex/requirements-voice.txt
```

---

## 3. Create a Telegram Bot

Open Telegram and talk to `@BotFather`.

1. Send `/newbot`.
2. Choose a display name.
3. Choose a username ending in `bot`, for example `my_durex_bot`.
4. Copy the token returned by BotFather.

Export the token in the shell that will run Durex:

```bash
export DUREX_TELEGRAM_BOT_TOKEN="token-from-botfather"
```

Validate the token:

```bash
python3 codex_queue.py telegram-check
```

If the token is valid, Durex prints the bot username.

### Recover a Lost Token

Telegram does not show bot tokens in normal chats. Use `@BotFather`:

1. Send `/mybots`.
2. Select the Durex bot.
3. Open `API Token`.
4. Copy the current token or regenerate it.

Regenerating a token invalidates the previous value immediately. Update
`DUREX_TELEGRAM_BOT_TOKEN` before restarting Durex.

---

## 4. Discover the Chat ID

Durex accepts commands only from one configured Telegram chat.

For a private chat:

1. Open the bot chat.
2. Send `/start`.
3. Send any short message, for example `hello`.

For a group:

1. Add the bot to the group.
2. Send `/start@YourBotUsername` or any normal message in the group.

Then discover recent chat ids:

```bash
unset DUREX_TELEGRAM_CHAT_ID
python3 codex_queue.py telegram-check --discover-chat-id --poll-timeout 30
```

Export the exact printed value:

```bash
export DUREX_TELEGRAM_CHAT_ID="the-printed-chat-id"
```

Private chat ids are usually positive integers. Group and supergroup ids are
often negative, frequently beginning with `-100`. Keep the minus sign.

Send a test message:

```bash
python3 codex_queue.py telegram-check --send-test
```

Only continue after the test message arrives in Telegram.

If `--send-test` returns `HTTP Error 403: Forbidden`, the token is valid but the
bot cannot write to that chat. Usually the chat id is wrong, the bot was not
started in private chat, the bot was removed from a group, or the bot was
blocked. Send `/start`, rediscover the chat id, export it, and retry
`--send-test`.

---

## 5. Configure Allowed Workdirs

Remote Telegram commands can add tasks only inside allowed directories. This is
intentional: Telegram input must not become unrestricted filesystem access.

For one project:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /lab/durex
```

For a PTY worker that can ask for Codex approvals through the same bot:

```bash
python3 codex_queue.py telegram-control \
  --allowed-workdir /lab/durex \
  --runner-mode pty \
  --worker-telegram-approvals
```

For multiple projects:

```bash
python3 codex_queue.py telegram-control \
  --allowed-workdir /lab/durex \
  --allowed-workdir /lab/another-project
```

Or configure roots with an environment variable:

```bash
export DUREX_TELEGRAM_ALLOWED_WORKDIRS="/lab/durex:/lab/another-project"
python3 codex_queue.py telegram-control
```

For button-based workflows, configure named choices. This makes mobile use much
easier because Telegram can show a select-like list of project directories.

```bash
export DUREX_TELEGRAM_WORKDIR_CHOICES="durex=/lab/durex,other=/lab/another-project"
```

---

## 6. YAML Configuration

For persistent local configuration, use `config.yaml` or set
`DUREX_CONFIG=/path/to/config.yaml`.

Example:

```yaml
telegram_control:
  allowed_workdirs:
    - /lab/durex
    - /lab/another-project
  workdir_choices:
    durex: /lab/durex
    other: /lab/another-project
  interactive_state_ttl_seconds: 900
  interactive_state_max_entries: 100
  voice:
    enabled: true
    provider: faster_whisper
    model: base
    language: auto
    allowed_languages: [it, en]
    aliases_file: .durex_voice_aliases.json
    max_file_bytes: 10485760
    max_duration_seconds: 300
    debug: true
    workdir_aliases:
      durex: /lab/durex
      other: /lab/another-project
```

Start with the config file:

```bash
export DUREX_CONFIG=/lab/durex/config.yaml
python3 codex_queue.py telegram-control
```

Environment variables override YAML settings. Keep secrets such as
`DUREX_TELEGRAM_BOT_TOKEN` and `DUREX_TELEGRAM_CHAT_ID` in the environment, not
in committed YAML.

Details: [CONFIGURATION.md](CONFIGURATION.md).

---

## 7. Enable Local Voice Commands

Voice commands are disabled by default.

Install optional dependencies:

```bash
pip install -r requirements-voice.txt
```

Enable local transcription:

```bash
export DUREX_VOICE_ENABLED=1
export DUREX_VOICE_PROVIDER=faster_whisper
export DUREX_VOICE_MODEL=base
export DUREX_VOICE_LANGUAGE=auto
export DUREX_VOICE_ALLOWED_LANGUAGES=it,en
export DUREX_VOICE_ALIASES_FILE=.durex_voice_aliases.json
export DUREX_VOICE_MAX_FILE_BYTES=10485760
export DUREX_VOICE_MAX_DURATION_SECONDS=300
export DUREX_VOICE_DEBUG=1
```

With `DUREX_VOICE_LANGUAGE=auto`, Durex probes the allowed languages explicitly
in order. With `DUREX_VOICE_ALLOWED_LANGUAGES=it,en`, it tries Italian first,
then English, and accepts the first transcript that parses as a supported Durex
command.

For the wizard's free-form task prompt, `auto` performs one unrestricted local
language-detection pass and accepts only a detected language from the same
allow-list. An explicit `DUREX_VOICE_LANGUAGE` remains a fixed prompt hint.

Use workdir aliases instead of dictating filesystem paths:

```bash
export DUREX_VOICE_WORKDIR_ALIASES="durex=/lab/durex,other=/lab/another-project"
```

The first faster-whisper transcription can download the selected local model.

Details: [TELEGRAM_VOICE_COMMANDS.md](TELEGRAM_VOICE_COMMANDS.md).

---

## 8. Start Telegram Control

Start the daemon:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /lab/durex
```

With YAML:

```bash
export DUREX_CONFIG=/lab/durex/config.yaml
python3 codex_queue.py telegram-control
```

Only one Telegram-control daemon should poll the same bot token at a time.
Running multiple daemons with the same bot can make updates appear to disappear
because one process consumes them before the other.

---

## 9. First Smoke Test from Telegram

Send these commands one by one.

Check the bot is alive:

```text
/help
```

Check queue and worker state:

```text
/status
```

List tasks:

```text
/tasks
```

Start the guided task wizard:

```text
/add-wizard
```

In the wizard:

1. Pick the `durex` workdir button.
2. Pick `100 normal`.
3. Send this prompt as text or voice:

```text
Read the README and summarize what Durex does.
```

4. Tap `Create Task`.

Start the worker:

```text
/run
```

Show the latest output:

```text
/tail
```

Request a graceful worker stop:

```text
/stop
```

Voice smoke test, if enabled:

```text
stato
lista task
aggiungi task
avvia worker
mostra output
ferma worker
```

English voice equivalents:

```text
status
list tasks
add task
start worker
show output
stop worker
```

---

## 10. CLI Command Reference

### `init`

Create the SQLite task database if it does not exist:

```bash
python3 codex_queue.py init
```

Safe to run multiple times.

### `add`

Add one task:

```bash
python3 codex_queue.py add \
  --title "Fix tests" \
  --workdir /lab/durex \
  --priority 10 \
  --prompt "Run the tests, fix failures, and summarize the changes."
```

Priority uses lower numbers first:

```text
1      urgent
10     high
100    normal
999    low
```

### `list`

Show queue rows:

```bash
python3 codex_queue.py list
```

### `run`

Run queued tasks:

```bash
python3 codex_queue.py run
```

Useful variants:

```bash
python3 codex_queue.py run --stop-when-empty
python3 codex_queue.py run --runner-mode subprocess
python3 codex_queue.py run --runner-mode pty
python3 codex_queue.py run --runner-mode pty --telegram
```

`subprocess` is best for non-interactive work. `pty` is required for interactive
Codex approvals.

### `telegram-check`

Validate token, discover chat id, and send a test:

```bash
python3 codex_queue.py telegram-check
python3 codex_queue.py telegram-check --discover-chat-id --poll-timeout 30
python3 codex_queue.py telegram-check --send-test
```

### `telegram-control`

Start the Telegram command daemon:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /lab/durex
```

Add `--runner-mode pty --worker-telegram-approvals` when `/run` must support
approval buttons. Queue commands and approval callbacks then share the sole
`getUpdates` loop. Do not run chat-id discovery or standalone `run --telegram`
concurrently with the same bot token.

Standalone `run --telegram` accepts approval buttons only. Control commands sent
while it owns the token are unsupported and may be lost; Telegram filtering does
not provide deferred delivery to a later daemon. Prefer the shared daemon above
when operating from a phone.

---

## 11. Telegram Text Command Reference

### `/help`

Show available Telegram commands:

```text
/help
```

### `/status`

Show worker state and queue counts:

```text
/status
```

### `/tasks`

List recent tasks and show task buttons:

```text
/tasks
/tasks 20
```

Tap a task button to open details. From the detail view, use `Tail Output`,
`Run`, or `Stop`.

### `/tail`

Show output from the latest task or a specific task:

```text
/tail
/tail 42
```

### `/add`

Add a task from Telegram.

Multiline form:

```text
/add --title "Smoke test" --workdir /lab/durex --priority 100
Read the README and summarize what Durex does.
```

Single-line form:

```text
/add --title "Smoke test" --workdir /lab/durex --priority 100 --prompt "Read the README and summarize what Durex does."
```

If Telegram sends the command when you tap `OK`, use the single-line form or
the guided wizard.

### `/add-wizard`

Start the button-based add-task wizard:

```text
/add-wizard
```

The wizard uses:

- workdir buttons as a select control;
- priority presets: `1 urgent`, `10 high`, `100 normal`, `999 low`;
- priority stepper buttons: `-10`, `-5`, `-1`, `+1`, `+5`, `+10`;
- text or voice prompt entry;
- a final `Create Task` confirmation button.

### `/run`

Start the local Durex worker:

```text
/run
```

The worker runs in the daemon process until the queue is empty, a stop is
requested, or an error occurs.

### `/stop`

Ask the worker to stop before starting another task:

```text
/stop
```

This is a graceful stop request. It does not blindly kill an already running
Codex process.

### `/config`

Show button-based runtime controls:

```text
/config
```

Current controls include a checkbox-style toggle for voice debug output.

### `/learn`

Teach Durex a local voice alias:

```text
/learn run abbia walker
/learn tasks lista tac
/learn status stato coda
```

Supported learned actions:

```text
status
tasks
tail
run
stop
```

Aliases are stored in `DUREX_VOICE_ALIASES_FILE`, usually
`.durex_voice_aliases.json`. This file is local and should not be committed.

---

## 12. Voice Command Reference

Durex accepts short voice commands and maps them to safe Telegram-control
operations. Voice input is never forwarded as arbitrary shell input.

### Status

Italian:

```text
stato
stato coda
```

English:

```text
status
queue status
```

### Task List

Italian:

```text
task
lista task
lista task cinque
mostra task
mostra tasks
```

English:

```text
tasks
list tasks
list tasks five
show tasks
```

### Output Tail

Italian:

```text
output
mostra output
ultimo output
mostra output task cinque
```

English:

```text
tail
show output
latest output
show output task five
```

### Add Task Wizard

Italian:

```text
aggiungi task
aggiungi un task
crea task
```

English:

```text
add task
new task
create task
```

Use this for normal smartphone usage. It opens the button-based wizard.

### Structured Add Task

Structured dictation is available, but it is more fragile than the wizard.

Italian:

```text
aggiungi task titolo smoke test cartella durex priorita uno prompt leggi il readme
```

English:

```text
add task title smoke test directory durex priority one prompt read the readme
```

Required fields:

- title or `titolo`;
- directory, `cartella`, path, or `percorso`;
- prompt.

Priority is optional and defaults to `100`.

### Run Worker

Italian:

```text
avvia
avvia worker
esegui
parti
```

English:

```text
run
start
start worker
```

### Stop Worker

Italian:

```text
ferma
ferma worker
fermati
```

English:

```text
stop
stop worker
```

---

## 13. Voice Calibration Workflow

Enable debug output while calibrating:

```bash
export DUREX_VOICE_DEBUG=1
```

When Durex cannot recognize a voice command, it replies with the transcript and
inline Learn buttons. If you say `avvia worker` and Whisper hears `abbia
walker`, tap `Learn Run`. Future voice messages transcribed as `abbia walker`
will run the safe `run` action.

Manual equivalent:

```text
/learn run abbia walker
```

Recommended calibration sequence:

```text
stato
lista task
mostra output
aggiungi task
avvia worker
ferma worker
status
list tasks
show output
add task
start worker
stop worker
```

Calibrate only aliases that map to the intended safe action. Avoid teaching long
free-form task prompts as aliases.

---

## 14. Troubleshooting

### `python3 -m venv .venv` fails with `Protocol error: 'lib' -> .../lib64`

Use copied files:

```bash
python3 -m venv --copies .venv
```

Or place the virtual environment outside the shared folder:

```bash
python3 -m venv /tmp/durex-venv
```

### `telegram-check --send-test` returns `403 Forbidden`

The bot token works, but the bot cannot send to the configured chat. Send
`/start` to the bot, rediscover the chat id, export the new value, and retry.

### `/add` says `Missing prompt`

Telegram sent only the command line. Use the single-line `--prompt` form:

```text
/add --title "Smoke test" --workdir /lab/durex --priority 100 --prompt "Read the README."
```

Or use:

```text
/add-wizard
```

### Voice commands detect the wrong language

Use explicit allowed languages:

```bash
export DUREX_VOICE_LANGUAGE=auto
export DUREX_VOICE_ALLOWED_LANGUAGES=it,en
```

If one language consistently works better for you, force it:

```bash
export DUREX_VOICE_LANGUAGE=it
```

or:

```bash
export DUREX_VOICE_LANGUAGE=en
```

### Voice command is transcribed but not recognized

Enable debug, retry the phrase, then tap a Learn button or use `/learn`:

```text
/learn run transcribed phrase here
```

### Workdir is rejected

The task workdir must be inside an allowed root. Check:

```bash
echo "$DUREX_TELEGRAM_ALLOWED_WORKDIRS"
echo "$DUREX_TELEGRAM_WORKDIR_CHOICES"
```

Then restart `telegram-control` after changing config.

### YAML config is ignored or fails to load

Install `PyYAML` from `requirements-voice.txt` or remove `DUREX_CONFIG`.
Environment variables override YAML, so check for stale exported values.

### Buttons do not respond

Restart `telegram-control` after updating Durex code. Also make sure only one
daemon is polling the same bot token.

---

## 15. Safe Operating Notes

- Telegram remote control operates the Durex queue, not an unrestricted shell.
- Voice transcripts are mapped to supported commands before execution.
- Add-task workdirs must be inside allowed roots.
- Bot token and chat id are secrets. Keep them out of committed files.
- Approval mode and remote-control mode are separate features.
- Direct live Codex terminal control is intentionally left to a future policy
  layer such as Alfred.
