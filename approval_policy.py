#!/usr/bin/env python3
"""
approval_policy.py

Policy engine for Durex approval requests.

Why this module exists
----------------------
The PTY detector can recognize that Codex is asking for approval, but it should
not decide what to do. Detection and decision-making are separate concerns:

- approval_detector.py detects approval prompts in terminal output;
- approval_policy.py classifies the detected command;
- telegram_bridge.py asks the user when the policy says ASK_TELEGRAM;
- pty_runner.py writes the final decision back into the pseudo-terminal.

This module is intentionally independent from Codex, Telegram and SQLite. That
makes it easy to unit-test and safe to reuse from both a PTY runner and a future
structured-event runner.

Decision model
--------------
The policy returns one of three normalized decisions:

- AUTO_ALLOW: approve locally without asking Telegram;
- ASK_TELEGRAM: ask the user through Telegram;
- AUTO_DENY: deny locally without asking Telegram.

Security note
-------------
The default configuration should be conservative. If a command does not match a
known rule, ASK_TELEGRAM is the safest default for a human-in-the-loop tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import fnmatch
import re
import shlex
import time
from typing import Iterable, Optional


class PolicyAction(str, Enum):
    """
    Normalized policy action returned by the policy engine.

    The values are strings so they are easy to serialize in logs, audit records
    and future JSON APIs.

    Values:
        AUTO_ALLOW:
            The command may be approved locally without user interaction.
        ASK_TELEGRAM:
            The command requires a human decision through Telegram.
        AUTO_DENY:
            The command should be denied locally without user interaction.
    """

    AUTO_ALLOW = "auto_allow"
    ASK_TELEGRAM = "ask_telegram"
    AUTO_DENY = "auto_deny"


@dataclass(frozen=True)
class PolicyDecision:
    """
    Result of policy classification.

    Attributes:
        action:
            The normalized action selected by the policy engine.

        reason:
            Human-readable explanation useful for logs and Telegram messages.

        matched_rule:
            The rule that matched the command, if any. None means the default
            decision was used.

        created_at:
            Local Unix timestamp for audit records.
    """

    action: PolicyAction
    reason: str
    matched_rule: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class PolicyRule:
    """
    One command-matching rule.

    The first v0.2 implementation uses shell-style glob patterns because they
    are easy to understand in configuration files. A future version can add
    richer rule types such as regex, command AST parsing or per-directory rules.

    Example patterns:
        pytest *
        python -m pytest *
        git status*
    """

    pattern: str
    action: PolicyAction
    description: str = ""

    def matches(self, command: str) -> bool:
        """
        Return True when this rule matches the command.

        Matching is case-sensitive by default because shell commands are usually
        case-sensitive. Whitespace is normalized to reduce accidental mismatch.

        Args:
            command:
                Shell command extracted from an approval prompt.

        Returns:
            True when the normalized command satisfies this rule's glob pattern.
        """

        normalized_command = normalize_command(command)
        normalized_pattern = normalize_command(self.pattern)
        return fnmatch.fnmatchcase(normalized_command, normalized_pattern)


@dataclass
class ApprovalPolicy:
    """
    Collection of rules plus a default action.

    Rule evaluation order matters. The policy checks explicit deny rules first,
    then allow rules, then ask rules, and finally falls back to the default
    action. Deny-first behavior is intentionally conservative.
    """

    auto_allow: list[PolicyRule] = field(default_factory=list)
    ask_telegram: list[PolicyRule] = field(default_factory=list)
    auto_deny: list[PolicyRule] = field(default_factory=list)
    default_action: PolicyAction = PolicyAction.ASK_TELEGRAM

    def classify_command(self, command: Optional[str]) -> PolicyDecision:
        """
        Classify a command into AUTO_ALLOW, ASK_TELEGRAM or AUTO_DENY.

        Args:
            command:
                Command extracted from the terminal approval prompt. It can be
                None when the detector could not confidently extract it.

        Returns:
            PolicyDecision with action, reason and matched rule.
        """

        if not command:
            return PolicyDecision(
                action=PolicyAction.ASK_TELEGRAM,
                reason="No command was extracted, so human approval is required.",
                matched_rule=None,
            )

        for rule in self.auto_deny:
            if rule.matches(command):
                return PolicyDecision(
                    action=PolicyAction.AUTO_DENY,
                    reason=rule.description or "Command matched an auto-deny rule.",
                    matched_rule=rule.pattern,
                )

        for rule in self.auto_allow:
            if rule.matches(command):
                return PolicyDecision(
                    action=PolicyAction.AUTO_ALLOW,
                    reason=rule.description or "Command matched an auto-allow rule.",
                    matched_rule=rule.pattern,
                )

        for rule in self.ask_telegram:
            if rule.matches(command):
                return PolicyDecision(
                    action=PolicyAction.ASK_TELEGRAM,
                    reason=rule.description or "Command matched an ask-Telegram rule.",
                    matched_rule=rule.pattern,
                )

        return PolicyDecision(
            action=self.default_action,
            reason=f"No explicit rule matched; using default action: {self.default_action.value}.",
            matched_rule=None,
        )


def normalize_command(command: str) -> str:
    """
    Normalize command whitespace while preserving shell content as much as possible.

    This is intentionally simple. It does not try to parse and rewrite shell
    syntax. It only trims leading/trailing whitespace and collapses repeated
    whitespace between tokens.

    Args:
        command:
            Shell command text as extracted from terminal output or config.

    Returns:
        Command text with normalized whitespace. Quoting and token boundaries
        are not interpreted here.
    """

    return re.sub(r"\s+", " ", command.strip())


def first_token(command: str) -> Optional[str]:
    """
    Return the first shell token of a command when possible.

    This helper is not currently used for final decisions, but it is useful for
    debugging, logging and future rule engines.

    Args:
        command:
            Shell command text.

    Returns:
        The first parsed shell token, or None when the command cannot be parsed
        or contains no tokens.
    """

    try:
        parts = shlex.split(command)
    except ValueError:
        return None

    return parts[0] if parts else None


def build_rules(patterns: Iterable[str], action: PolicyAction, description: str = "") -> list[PolicyRule]:
    """
    Convert a list of patterns into PolicyRule objects.

    Args:
        patterns:
            Glob-style command patterns.
        action:
            Policy action assigned to every generated rule.
        description:
            Human-facing reason reused when a generated rule matches.

    Returns:
        A list of immutable PolicyRule instances.
    """

    return [PolicyRule(pattern=pattern, action=action, description=description) for pattern in patterns]


def default_policy() -> ApprovalPolicy:
    """
    Return a conservative default policy suitable for v0.2 development.

    The defaults are deliberately small. Real users should customize the policy
    in config.yaml once configuration loading is implemented.

    Returns:
        ApprovalPolicy with safe local validations auto-allowed, state-changing
        commands escalated to Telegram, and elevated-privilege commands denied.
    """

    return ApprovalPolicy(
        auto_allow=build_rules(
            patterns=[
                "pytest*",
                "python -m pytest*",
                "python3 -m pytest*",
                "ruff check*",
                "mypy*",
                "git status*",
                "git diff*",
                "git log*",
            ],
            action=PolicyAction.AUTO_ALLOW,
            description="Command is read-only or commonly safe for local validation.",
        ),
        ask_telegram=build_rules(
            patterns=[
                "git add*",
                "git commit*",
                "git push*",
                "pip install*",
                "pip3 install*",
                "npm install*",
                "pnpm install*",
                "yarn install*",
                "docker*",
                "curl*",
                "wget*",
            ],
            action=PolicyAction.ASK_TELEGRAM,
            description="Command may change project state, dependencies or external state.",
        ),
        auto_deny=build_rules(
            patterns=[
                "sudo*",
            ],
            action=PolicyAction.AUTO_DENY,
            description="Command requires elevated privileges and is denied by default.",
        ),
        default_action=PolicyAction.ASK_TELEGRAM,
    )


def action_from_string(value: str) -> PolicyAction:
    """
    Convert configuration strings into PolicyAction values.

    Accepted user-facing strings:
        ask
        ask_telegram
        approve
        auto_allow
        deny
        auto_deny

    Args:
        value:
            User-facing action string from configuration.

    Returns:
        The normalized PolicyAction.

    Raises:
        ValueError:
            Raised when the string does not map to a known action.
    """

    normalized = value.strip().lower().replace("-", "_")

    aliases = {
        "ask": PolicyAction.ASK_TELEGRAM,
        "ask_telegram": PolicyAction.ASK_TELEGRAM,
        "approve": PolicyAction.AUTO_ALLOW,
        "allow": PolicyAction.AUTO_ALLOW,
        "auto_allow": PolicyAction.AUTO_ALLOW,
        "deny": PolicyAction.AUTO_DENY,
        "auto_deny": PolicyAction.AUTO_DENY,
    }

    if normalized not in aliases:
        raise ValueError(f"Unknown policy action: {value!r}")

    return aliases[normalized]


def policy_from_dict(data: dict) -> ApprovalPolicy:
    """
    Build an ApprovalPolicy from a plain dictionary.

    This function allows the future config loader to remain simple. It does not
    require PyYAML directly; any loader can parse YAML/JSON/TOML into a dict and
    pass the relevant section here.

    Expected shape::

        {
          "default_decision": "ask",
          "auto_allow": ["pytest*"],
          "ask_telegram": ["git push*"],
          "auto_deny": ["sudo*"]
        }

    Args:
        data:
            Plain configuration dictionary, typically decoded from a future
            YAML/JSON/TOML loader.

    Returns:
        ApprovalPolicy built from configured pattern groups.

    Raises:
        ValueError:
            Propagated when ``default_decision`` uses an unknown action string.
    """

    default_action = action_from_string(data.get("default_decision", "ask"))

    return ApprovalPolicy(
        auto_allow=build_rules(
            patterns=data.get("auto_allow", []),
            action=PolicyAction.AUTO_ALLOW,
            description="Command matched configured auto_allow rule.",
        ),
        ask_telegram=build_rules(
            patterns=data.get("ask_telegram", []),
            action=PolicyAction.ASK_TELEGRAM,
            description="Command matched configured ask_telegram rule.",
        ),
        auto_deny=build_rules(
            patterns=data.get("auto_deny", []),
            action=PolicyAction.AUTO_DENY,
            description="Command matched configured auto_deny rule.",
        ),
        default_action=default_action,
    )


def _demo() -> None:
    """
    Manual demonstration.

    Run:
        python3 approval_policy.py

    Returns:
        None. The function prints sample policy decisions to stdout.
    """

    policy = default_policy()
    examples = [
        "pytest -q",
        "git status --short",
        "git push origin main",
        "pip install example-package",
        "sudo example-command",
        "unknown-tool --flag",
        None,
    ]

    for command in examples:
        decision = policy.classify_command(command)
        print("-" * 72)
        print(f"command: {command}")
        print(f"action: {decision.action.value}")
        print(f"reason: {decision.reason}")
        print(f"matched_rule: {decision.matched_rule}")


if __name__ == "__main__":
    _demo()
