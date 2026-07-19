# Telegram Voice Commands

Durex can accept Telegram voice messages as remote-control commands.

Voice commands are disabled by default. When enabled, Durex downloads the voice
attachment from Telegram, transcribes it locally with `faster-whisper`, parses
the transcript into a structured Durex command, and executes only the supported
remote-control operation.

Voice input is not shell input. The transcript is mapped to one of the same safe
operations used by text remote control: status, tasks, tail, add, run or stop.

## Privacy Model

The supported speech-to-text provider is local:

```text
Telegram voice file -> local download -> local faster-whisper -> Durex parser
```

No audio is sent to a third-party transcription API by Durex.

Telegram still stores and serves the original voice message because it is sent
through Telegram. Durex downloads it through the Telegram Bot API.

Durex stores each download in a private, unpredictable temporary file and
deletes it immediately after local transcription, including when transcription
or command parsing fails. The audio is removed before a parsed command is
executed. Telegram's own copy follows Telegram's retention behavior.

## Install Voice Dependencies

Create and activate a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
```

Install the optional voice dependency:

```bash
pip install -r requirements-voice.txt
```

The first transcription may download the selected faster-whisper model. The
same optional requirements file also installs PyYAML, which is needed only when
you load Telegram control settings from `config.yaml`.

## Enable Voice Commands

Set the usual Telegram credentials first:

```bash
export DUREX_TELEGRAM_BOT_TOKEN="your-bot-token"
export DUREX_TELEGRAM_CHAT_ID="your-chat-id"
python3 codex_queue.py telegram-check --send-test
```

Enable voice support:

```bash
export DUREX_VOICE_ENABLED=1
export DUREX_VOICE_PROVIDER=faster_whisper
export DUREX_VOICE_MODEL=base
export DUREX_VOICE_LANGUAGE=auto
export DUREX_VOICE_ALLOWED_LANGUAGES=it,en
export DUREX_VOICE_ALIASES_FILE=.durex_voice_aliases.json
export DUREX_VOICE_MAX_FILE_BYTES=10485760
export DUREX_VOICE_MAX_DURATION_SECONDS=300
```

With `DUREX_VOICE_LANGUAGE=auto`, Durex probes the allowed languages
explicitly in order. For the default above it transcribes with an Italian hint
first, then an English hint, and accepts the first transcript that parses as a
supported Durex command. Free Whisper language detection is used only as a final
diagnostic fallback.

The add-task wizard treats its free-form voice prompt differently: in `auto`
mode it uses Whisper language detection once and accepts the transcript only
when the detected language is in `DUREX_VOICE_ALLOWED_LANGUAGES`. Setting an
explicit `DUREX_VOICE_LANGUAGE` keeps that language as the prompt hint.

Start remote control:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /lab/durex
```

Or configure Telegram control in `config.yaml`:

```yaml
telegram_control:
  allowed_workdirs:
    - /lab/durex
  workdir_choices:
    durex: /lab/durex
  voice:
    enabled: true
    model: base
    language: auto
    allowed_languages: [it, en]
    aliases_file: .durex_voice_aliases.json
    debug: true
    max_file_bytes: 10485760
    max_duration_seconds: 300
    workdir_aliases:
      durex: /lab/durex
```

Durex rejects voice attachments above either configured limit before local
transcription. The byte limit is also enforced while streaming the download,
so missing or incorrect Telegram metadata cannot cause an unbounded read.

## Workdir Aliases

Spoken filesystem paths are fragile. Prefer aliases:

```bash
export DUREX_VOICE_WORKDIR_ALIASES="durex=/lab/durex,lab durex=/lab/durex"
```

Then say:

```text
aggiungi task titolo smoke test cartella durex prompt leggi il readme
```

instead of trying to dictate:

```text
/lab/durex
```

## Supported Voice Commands

Task-list requests accept between 1 and 50 rows. Task ids, priorities, and
maximum-attempt values sent through Telegram are validated before they reach
SQLite; invalid values are returned as command rejections without stopping the
control daemon.

### Status

Italian:

```text
stato
```

English:

```text
status
```

### Task List

Italian:

```text
lista task
lista task cinque
```

English:

```text
list tasks
show tasks five
```

### Output Tail

Italian:

```text
mostra output
mostra output task cinque
```

English:

```text
show output
show output task five
```

### Add Task

Italian:

```text
aggiungi task titolo smoke test cartella durex priorita uno prompt leggi il readme e riassumi cosa fa durex
```

For normal smartphone usage, prefer the guided wizard:

```text
aggiungi task
```

or:

```text
/add-wizard
```

Durex then asks for workdir with select-like buttons, priority with preset and
stepper buttons, and the prompt as text or voice. The final screen has a
`Create Task` confirmation button.

English:

```text
add task title smoke test directory durex priority one prompt read the readme and summarize what Durex does
```

Required fields:

- title or titolo;
- directory, cartella, path or percorso;
- prompt.

Priority is optional and defaults to `100`.

### Run Worker

Italian:

```text
avvia worker
```

English:

```text
start worker
```

### Stop Worker

Italian:

```text
ferma worker
```

English:

```text
stop worker
```

## Voice Calibration

Short operational commands can be misheard by the local speech-to-text model.
For example, `avvia worker` may become `abbia walker`.

Use debug mode while calibrating:

```bash
export DUREX_VOICE_DEBUG=1
```

When a voice command fails, Durex replies with the transcripts tried for each
language and shows inline Learn buttons for the best candidate:

```text
Command rejected: Voice command not recognized after transcription attempts:
it: abbia walker (detected it); en: ...

Learn candidate: abbia walker
```

Tap the matching button, for example `Learn Run`, and Durex stores the alias
locally. After that, sending a voice message that transcribes as `abbia walker`
will run the same safe action as `avvia worker`.

You can also teach Durex with a text command:

```text
/learn run abbia walker
```

Learning the same normalized phrase again replaces its previous action. The
mapping therefore remains identical before and after restarting Telegram
control.

Durex rejects a phrase that already belongs to a different built-in action. For
example, `/learn run status` is rejected because `status` already has an
unambiguous built-in meaning.

Learned aliases can target only simple remote-control actions:

```text
status
tasks
tail
run
stop
```

They cannot target `add`, because task creation needs structured fields such as
title, workdir, priority, and prompt.

Aliases are stored locally in:

```bash
.durex_voice_aliases.json
```

You can choose another path:

```bash
export DUREX_VOICE_ALIASES_FILE=/path/to/voice_aliases.json
```

## Button-Based Task Flow

Say or send:

```text
lista task
```

Durex replies with recent tasks and inline buttons:

```text
[Task #12]
[Task #11]
[Refresh] [Run] [Stop]
```

Tapping a task opens details and exposes `Tail Output`, `Run`, and `Stop`.

For task creation, use:

```text
aggiungi task
```

The wizard uses:

- select-like workdir buttons from `telegram_control.workdir_choices`;
- priority presets: `1 urgent`, `10 high`, `100 normal`, `999 low`;
- priority stepper buttons: `-10`, `-5`, `-1`, `+1`, `+5`, `+10`;
- a final `Create Task` confirmation.

For runtime toggles, send:

```text
/config
```

`Voice debug` is shown as an inline toggle button.

## First Smoke Test

1. Start the daemon with voice enabled.
2. Send a Telegram voice message saying `stato`.
3. Durex should reply with the transcript and queue status.
4. Send a voice message saying:

```text
aggiungi task titolo smoke test cartella durex priorita uno prompt leggi il readme
```

5. Durex should reply with `Task added: smoke test`.
6. Send `lista task` as a voice message.

## Troubleshooting

### Voice commands are disabled

Set:

```bash
export DUREX_VOICE_ENABLED=1
```

Then restart `telegram-control`.

### faster-whisper is missing

Install:

```bash
pip install -r requirements-voice.txt
```

### Language Detection Drift

Durex probes these languages when `DUREX_VOICE_LANGUAGE=auto`:

```bash
export DUREX_VOICE_ALLOWED_LANGUAGES=it,en
```

Free automatic detection can be unreliable for very short voice messages such as
`stato`, `status` or `lista task`. Some models may detect Spanish, Russian, or
Arabic and then write the transcript in that language's script. Durex avoids
that path for normal command routing by trying the configured supported
languages first.

If you only want one language and want faster transcription, force it:

```bash
export DUREX_VOICE_LANGUAGE=it
```

or:

```bash
export DUREX_VOICE_LANGUAGE=en
```

### Workdir not allowed

Make sure the resolved alias is inside an allowed root:

```bash
export DUREX_VOICE_WORKDIR_ALIASES="durex=/lab/durex"
python3 codex_queue.py telegram-control --allowed-workdir /lab/durex
```
