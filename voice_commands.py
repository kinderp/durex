#!/usr/bin/env python3
"""
Voice command parsing for Durex Telegram remote control.

The parser intentionally maps transcripts to Durex queue operations, not shell
commands. It supports a small Italian and English grammar that can be tested
without audio, network access or speech-to-text dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional


DEFAULT_PRIORITY = 100
DEFAULT_TASK_LIMIT = 10


class VoiceCommandError(ValueError):
    """Raised when a transcript cannot be mapped to a Durex command."""


@dataclass(frozen=True)
class VoiceCommand:
    """
    Structured Durex command parsed from a voice transcript.

    Attributes:
        action:
            One of ``status``, ``tasks``, ``tail``, ``add``, ``run`` or
            ``stop``.
        title:
            Task title for ``add``.
        workdir:
            Requested workdir for ``add``.
        prompt:
            Prompt body for ``add``.
        priority:
            Queue priority for ``add``.
        task_id:
            Optional task id for ``tail``.
        limit:
            Optional task count for ``tasks``.
        transcript:
            Normalized transcript that produced this command.
    """

    action: str
    title: Optional[str] = None
    workdir: Optional[str] = None
    prompt: Optional[str] = None
    priority: int = DEFAULT_PRIORITY
    task_id: Optional[int] = None
    limit: Optional[int] = None
    transcript: str = ""


NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "undici": 11,
    "dodici": 12,
    "tredici": 13,
    "quattordici": 14,
    "quindici": 15,
    "uno": 1,
    "una": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
    "cinque": 5,
    "sei": 6,
    "sette": 7,
    "otto": 8,
    "nove": 9,
    "dieci": 10,
}


CYRILLIC_PHONETIC_TRANSLATION = str.maketrans(
    {
        "\u0430": "a",
        "\u0431": "b",
        "\u0432": "v",
        "\u0433": "g",
        "\u0434": "d",
        "\u0435": "e",
        "\u0451": "e",
        "\u0436": "zh",
        "\u0437": "z",
        "\u0438": "i",
        "\u0439": "i",
        "\u043a": "k",
        "\u043b": "l",
        "\u043c": "m",
        "\u043d": "n",
        "\u043e": "o",
        "\u043f": "p",
        "\u0440": "r",
        "\u0441": "s",
        "\u0442": "t",
        "\u0443": "u",
        "\u0444": "f",
        "\u0445": "h",
        "\u0446": "ts",
        "\u0447": "ch",
        "\u0448": "sh",
        "\u0449": "sh",
        "\u044b": "y",
        "\u044d": "e",
        "\u044e": "yu",
        "\u044f": "ya",
    }
)


def normalize_transcript(text: str) -> str:
    """
    Normalize speech-to-text output for deterministic command parsing.

    Args:
        text:
            Raw transcription.

    Returns:
        Lowercased text with punctuation collapsed to spaces, while preserving
        slashes, dots, underscores and hyphens for paths.
    """

    lowered = text.strip().lower().translate(CYRILLIC_PHONETIC_TRANSLATION)
    lowered = re.sub(r"[,:;!?()\[\]{}\"']", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def parse_number(text: str) -> Optional[int]:
    """
    Parse a small integer from digits or common Italian/English number words.

    Args:
        text:
            Candidate number token.

    Returns:
        Parsed integer, or None when the token is not recognized.
    """

    token = text.strip().lower()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def find_first_number(text: str) -> Optional[int]:
    """
    Return the first number found in a transcript.

    Args:
        text:
            Normalized transcript.

    Returns:
        First parsed number, or None.
    """

    for token in text.split():
        value = parse_number(token)
        if value is not None:
            return value
    return None


def resolve_workdir(value: str, aliases: dict[str, str]) -> str:
    """
    Resolve a spoken workdir through aliases or literal path text.

    Args:
        value:
            Workdir phrase from the transcript.
        aliases:
            Mapping of spoken aliases to real paths.

    Returns:
        Mapped path when an alias exists, otherwise the stripped phrase.
    """

    key = normalize_transcript(value)
    return aliases.get(key, value.strip())


def extract_between_markers(text: str, start_markers: list[str], end_markers: list[str]) -> Optional[str]:
    """
    Extract text after one marker and before the next marker.

    Args:
        text:
            Normalized transcript.
        start_markers:
            Candidate start marker words.
        end_markers:
            Candidate end marker words.

    Returns:
        Extracted phrase, or None if no marker matched.
    """

    starts = sorted(start_markers, key=len, reverse=True)
    ends = sorted(end_markers, key=len, reverse=True)
    for marker in starts:
        match = re.search(rf"(?:^|\s){re.escape(marker)}\s+", text)
        if not match:
            continue

        start = match.end()
        end = len(text)
        for end_marker in ends:
            end_match = re.search(rf"\s{re.escape(end_marker)}\s+", text[start:])
            if end_match:
                candidate_end = start + end_match.start()
                end = min(end, candidate_end)
        value = text[start:end].strip()
        return value or None
    return None


def parse_voice_command(transcript: str, workdir_aliases: Optional[dict[str, str]] = None) -> VoiceCommand:
    """
    Parse Italian or English speech text into a Durex remote-control command.

    Args:
        transcript:
            Speech-to-text transcript.
        workdir_aliases:
            Spoken workdir aliases, for example ``{"durex": "/lab/durex"}``.

    Returns:
        Structured VoiceCommand.

    Raises:
        VoiceCommandError:
            Raised when the transcript is missing required command structure.
    """

    aliases = {normalize_transcript(key): value for key, value in (workdir_aliases or {}).items()}
    text = normalize_transcript(transcript)
    if not text:
        raise VoiceCommandError("Empty voice transcript.")

    if text in {"status", "stato", "stato coda", "queue status"}:
        return VoiceCommand(action="status", transcript=text)

    if text in {"tasks", "task", "lista task", "lista tasks", "mostra task", "mostra tasks", "list tasks", "show tasks"}:
        return VoiceCommand(action="tasks", limit=DEFAULT_TASK_LIMIT, transcript=text)

    if text.startswith(("lista task ", "lista tasks ", "mostra task ", "mostra tasks ", "list tasks ", "show tasks ")):
        limit = find_first_number(text)
        return VoiceCommand(action="tasks", limit=limit or DEFAULT_TASK_LIMIT, transcript=text)

    if text in {"tail", "output", "mostra output", "show output", "latest output", "ultimo output"}:
        return VoiceCommand(action="tail", transcript=text)

    if text.startswith(("tail ", "mostra output task ", "show output task ", "output task ")):
        task_id = find_first_number(text)
        return VoiceCommand(action="tail", task_id=task_id, transcript=text)

    if text in {"run", "start", "start worker", "avvia", "avvia worker", "esegui", "parti"}:
        return VoiceCommand(action="run", transcript=text)

    if text in {"stop", "stop worker", "ferma", "ferma worker", "fermati"}:
        return VoiceCommand(action="stop", transcript=text)

    if text.startswith(("add task", "aggiungi task", "aggiungi un task", "crea task")):
        return parse_add_voice_command(text, aliases)

    raise VoiceCommandError(f"Voice command not recognized: {transcript}")


def parse_add_voice_command(text: str, workdir_aliases: dict[str, str]) -> VoiceCommand:
    """
    Parse a marker-based voice add command.

    Supported shapes:
        ``aggiungi task titolo X cartella Y priorita Z prompt P``
        ``add task title X directory Y priority Z prompt P``

    Args:
        text:
            Normalized transcript.
        workdir_aliases:
            Spoken workdir aliases.

    Returns:
        Add VoiceCommand.

    Raises:
        VoiceCommandError:
            Raised when title, workdir or prompt cannot be extracted.
    """

    title = extract_between_markers(
        text,
        ["titolo", "title"],
        ["cartella", "directory", "dir", "percorso", "path", "priorita", "priorità", "priority", "prompt"],
    )
    workdir = extract_between_markers(
        text,
        ["cartella", "directory", "dir", "percorso", "path"],
        ["priorita", "priorità", "priority", "prompt"],
    )
    priority_text = extract_between_markers(
        text,
        ["priorita", "priorità", "priority"],
        ["prompt"],
    )
    prompt = extract_between_markers(text, ["prompt"], [])

    if not title:
        raise VoiceCommandError("Voice add command is missing title/titolo.")
    if not workdir:
        raise VoiceCommandError("Voice add command is missing workdir/cartella.")
    if not prompt:
        raise VoiceCommandError("Voice add command is missing prompt.")

    priority = DEFAULT_PRIORITY
    if priority_text:
        parsed_priority = find_first_number(priority_text)
        if parsed_priority is None:
            raise VoiceCommandError(f"Voice add command has invalid priority: {priority_text}")
        priority = parsed_priority

    return VoiceCommand(
        action="add",
        title=title,
        workdir=resolve_workdir(workdir, workdir_aliases),
        prompt=prompt,
        priority=priority,
        transcript=text,
    )
