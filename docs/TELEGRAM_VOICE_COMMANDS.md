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

The first transcription may download the selected faster-whisper model.

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
```

With `DUREX_VOICE_LANGUAGE=auto`, Durex probes the allowed languages
explicitly in order. For the default above it transcribes with an Italian hint
first, then an English hint, and accepts the first transcript that parses as a
supported Durex command. Free Whisper language detection is used only as a final
diagnostic fallback.

Start remote control:

```bash
python3 codex_queue.py telegram-control --allowed-workdir /lab/durex
```

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
