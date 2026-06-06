#!/usr/bin/env python3
"""
Check that docs/USER_GUIDE.md mentions the current codex_queue.py CLI surface.

This script intentionally verifies presence, not prose quality. It catches the
most common documentation drift: adding, removing, or renaming argparse commands
and flags without updating the user guide.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "codex_queue.py"
GUIDE = REPO_ROOT / "docs" / "USER_GUIDE.md"

IGNORED_OPTIONS = {"--help"}


@dataclass(frozen=True)
class CommandHelp:
    """
    One argparse help page and the long options discovered in it.

    Attributes:
        command:
            Top-level codex_queue.py subcommand.
        options:
            Long options discovered in that command's help output.
    """

    command: str
    options: set[str]


def run_help(*args: str) -> str:
    """
    Return argparse help output for codex_queue.py.

    Args:
        *args:
            Optional subcommand path placed before ``--help``.

    Returns:
        stdout emitted by argparse.

    Raises:
        subprocess.CalledProcessError:
            Raised when the CLI help command exits with a non-zero status.
    """

    completed = subprocess.run(
        [sys.executable, str(CLI), *args, "--help"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout


def discover_commands(root_help: str) -> list[str]:
    """
    Extract top-level subcommands from argparse's ``{a,b,c}`` usage block.

    Args:
        root_help:
            Root ``codex_queue.py --help`` output.

    Returns:
        Ordered list of top-level subcommand names.

    Raises:
        RuntimeError:
            Raised when argparse output no longer contains the expected usage
            block.
    """

    match = re.search(r"\{([^}]+)\}", root_help)
    if not match:
        raise RuntimeError("Could not discover top-level commands from CLI help.")
    return [command.strip() for command in match.group(1).split(",") if command.strip()]


def discover_options(help_output: str) -> set[str]:
    """
    Extract long options from an argparse help page.

    Args:
        help_output:
            Help text for one command.

    Returns:
        Set of long option names, excluding globally ignored options such as
        ``--help``.
    """

    definition_lines = [
        line.lstrip()
        for line in help_output.splitlines()
        if line.lstrip().startswith("-")
    ]
    option_pattern = r"(?<![\w-])--[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
    return {
        option
        for line in definition_lines
        for option in re.findall(option_pattern, line)
        if option not in IGNORED_OPTIONS
    }


def load_cli_surface() -> tuple[list[str], list[CommandHelp]]:
    """
    Return top-level commands and per-command options from argparse.

    Returns:
        Tuple containing command names and structured help metadata for every
        command.
    """

    root_help = run_help()
    commands = discover_commands(root_help)
    command_help = []
    for command in commands:
        output = run_help(command)
        command_help.append(CommandHelp(command=command, options=discover_options(output)))
    return commands, command_help


def missing_mentions(guide_text: str, commands: list[str], command_help: list[CommandHelp]) -> list[str]:
    """
    Return missing command and option mentions in the guide.

    Args:
        guide_text:
            Current user guide text.
        commands:
            Top-level CLI commands discovered from argparse.
        command_help:
            Per-command option metadata.

    Returns:
        Human-readable list of missing command/option mentions.
    """

    missing: list[str] = []

    for command in commands:
        if command not in guide_text:
            missing.append(f"command `{command}`")

    for help_page in command_help:
        for option in sorted(help_page.options):
            if option not in guide_text:
                missing.append(f"{help_page.command}: `{option}`")

    return missing


def main() -> int:
    """
    Run the documentation drift check.

    Returns:
        Process exit code: 0 when docs mention the CLI surface, 1 when drift is
        detected.
    """

    guide_text = GUIDE.read_text()
    commands, command_help = load_cli_surface()
    missing = missing_mentions(guide_text, commands, command_help)

    if missing:
        print("CLI documentation drift detected in docs/USER_GUIDE.md:")
        for item in missing:
            print(f"- missing {item}")
        return 1

    print("CLI documentation check passed.")
    print(f"Commands checked: {', '.join(commands)}")
    for help_page in command_help:
        if help_page.options:
            options = ", ".join(sorted(help_page.options))
        else:
            options = "no long options"
        print(f"- {help_page.command}: {options}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
