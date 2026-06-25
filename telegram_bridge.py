#!/usr/bin/env python3
"""
telegram_bridge.py

Telegram approval bridge for Durex.

Why this module exists
----------------------
The PTY runner can detect that Codex is waiting for a human confirmation. When
that happens, Durex needs a way to ask the user without requiring the user to be
in front of the computer.

This module implements a small Telegram Bot API client using only the Python
standard library. It intentionally avoids third-party dependencies so the first
v0.2 implementation remains easy to run on a local machine.

Important security boundary
---------------------------
Telegram does not execute shell commands. Telegram only returns a decision:

- approve
- deny
- show_context
- stop
- timeout

The local PTY runner remains responsible for deciding what to write back into
Codex's terminal input.

Transport model
---------------
The first implementation uses long polling through getUpdates. This is easier
for local usage than webhooks because it does not require a public HTTPS server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import time
from typing import Any, Optional
from urllib import parse, request, error


class TelegramDecisionAction(str, Enum):
    """
    Normalized user actions returned by Telegram callbacks.

    Values:
        APPROVE:
            Allow the PTY runner to answer the prompt positively.
        DENY:
            Ask the PTY runner to answer the prompt negatively.
        SHOW_CONTEXT:
            Send more terminal context without completing the approval.
        STOP:
            Ask the PTY runner to terminate the current task.
        TIMEOUT:
            Internal timeout outcome when no callback arrives.
    """

    APPROVE = "approve"
    DENY = "deny"
    SHOW_CONTEXT = "show_context"
    STOP = "stop"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class TelegramApprovalRequest:
    """
    Request sent to the Telegram bridge.

    This object is intentionally close to the ApprovalRequest produced by
    approval_detector.py, but it also includes task metadata useful for the
    message shown on the phone.

    Attributes:
        request_id:
            Detector fingerprint used to correlate callbacks.
        task_id:
            Queue task id, if the request came from codex_queue.py.
        task_title:
            Human-readable task title.
        workdir:
            Working directory of the task that triggered the prompt.
        command:
            Extracted command, or None when the detector could not identify it.
        reason:
            Policy/detector explanation shown to the user.
        context:
            Redacted terminal context around the approval prompt.
        verbosity:
            Message detail level requested for this approval.
        created_at:
            Local Unix timestamp for message construction.
    """

    request_id: str
    task_id: Optional[int]
    task_title: str
    workdir: str
    command: Optional[str]
    reason: str
    context: str
    verbosity: str = "normal"
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class TelegramApprovalDecision:
    """
    Decision returned by TelegramApprovalBridge.wait_for_decision().

    Attributes:
        request_id:
            Approval request id that this decision answers.
        action:
            Normalized callback action.
        source:
            Decision origin, usually ``telegram`` or ``timeout``.
        telegram_user_id:
            Telegram user id that pressed the button, when available.
        telegram_message_id:
            Telegram message id that contained the inline keyboard, when
            available.
        decided_at:
            Local Unix timestamp for audit/event consumers.
    """

    request_id: str
    action: TelegramDecisionAction
    source: str
    telegram_user_id: Optional[int] = None
    telegram_message_id: Optional[int] = None
    decided_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class TelegramBridgeConfig:
    """
    Runtime configuration for the Telegram bridge.

    bot_token:
        Telegram bot token. Do not commit it to Git. Load it from an environment
        variable in real usage.

    allowed_chat_id:
        Only callbacks from this chat id are accepted. This prevents random
        Telegram users from controlling local approvals.

    verbosity:
        compact, normal or verbose.

    approval_timeout_seconds:
        Maximum time to wait for a callback.

    timeout_default_decision:
        Decision applied when no callback arrives before timeout. The safest
        default is deny.
    """

    bot_token: str
    allowed_chat_id: int
    verbosity: str = "normal"
    approval_timeout_seconds: int = 900
    timeout_default_decision: TelegramDecisionAction = TelegramDecisionAction.DENY
    api_base: str = "https://api.telegram.org"


class TelegramBridgeError(RuntimeError):
    """
    Raised when the Telegram API returns an error or an invalid response.
    """


def extract_chat_ids_from_updates(updates: list[dict[str, Any]]) -> list[int]:
    """
    Return unique Telegram chat ids found in getUpdates payloads.

    Telegram returns different update shapes for normal messages and inline
    callbacks. Durex accepts both depending on the feature being used.

    Args:
        updates:
            Raw Telegram getUpdates result list.

    Returns:
        Unique chat ids in first-seen order. Invalid or missing chat ids are
        ignored because discovery is an operator aid, not a security decision.
    """

    chat_ids: list[int] = []
    seen: set[int] = set()

    for update in updates:
        candidates = [
            update.get("message", {}),
            update.get("edited_message", {}),
            update.get("callback_query", {}).get("message", {}),
        ]
        for message in candidates:
            chat = message.get("chat", {}) if isinstance(message, dict) else {}
            raw_chat_id = chat.get("id")
            try:
                chat_id = int(raw_chat_id)
            except (TypeError, ValueError):
                continue
            if chat_id not in seen:
                seen.add(chat_id)
                chat_ids.append(chat_id)

    return chat_ids


class TelegramApprovalBridge:
    """
    Small Telegram Bot API client for approval requests.

    The bridge exposes a deliberately small interface:

        send_approval_request(request) -> message_id
        wait_for_decision(request) -> TelegramApprovalDecision

    The PTY runner can call those methods whenever the policy returns
    ASK_TELEGRAM.
    """

    def __init__(self, config: TelegramBridgeConfig) -> None:
        """
        Initialize a Telegram approval bridge.

        Args:
            config:
                Runtime Telegram configuration. The bot token and allowed chat
                id define the transport and authorization boundary.

        Returns:
            None.
        """

        self.config = config
        self._last_update_id: Optional[int] = None

    @classmethod
    def from_env(
        cls,
        bot_token_env: str = "DUREX_TELEGRAM_BOT_TOKEN",
        chat_id_env: str = "DUREX_TELEGRAM_CHAT_ID",
        verbosity: str = "normal",
        approval_timeout_seconds: int = 900,
        timeout_default_decision: TelegramDecisionAction = TelegramDecisionAction.DENY,
    ) -> "TelegramApprovalBridge":
        """
        Build a bridge from environment variables.

        This keeps secrets out of config files and source code.

        Args:
            bot_token_env:
                Environment variable containing the bot token.
            chat_id_env:
                Environment variable containing the authorized chat id.
            verbosity:
                Default Telegram message verbosity.
            approval_timeout_seconds:
                Maximum wait time for approval callbacks.
            timeout_default_decision:
                Decision returned when the timeout expires.

        Returns:
            Configured TelegramApprovalBridge.

        Raises:
            TelegramBridgeError:
                Raised when required environment values are missing or invalid.
        """

        token = os.environ.get(bot_token_env)
        chat_id = os.environ.get(chat_id_env)

        if not token:
            raise TelegramBridgeError(f"Missing Telegram bot token environment variable: {bot_token_env}")
        if not chat_id:
            raise TelegramBridgeError(f"Missing Telegram chat id environment variable: {chat_id_env}")

        try:
            allowed_chat_id = int(chat_id)
        except ValueError as exc:
            raise TelegramBridgeError(f"Telegram chat id must be an integer: {chat_id_env}") from exc

        return cls(
            TelegramBridgeConfig(
                bot_token=token,
                allowed_chat_id=allowed_chat_id,
                verbosity=verbosity,
                approval_timeout_seconds=approval_timeout_seconds,
                timeout_default_decision=timeout_default_decision,
            )
        )

    def api_url(self, method: str) -> str:
        """
        Return the full Telegram API URL for one bot method.

        Args:
            method:
                Telegram Bot API method name, for example ``sendMessage``.

        Returns:
            Fully qualified HTTPS API URL for this bot token.
        """

        return f"{self.config.api_base}/bot{self.config.bot_token}/{method}"

    def file_url(self, file_path: str) -> str:
        """
        Return the full Telegram file download URL.

        Args:
            file_path:
                Telegram file path returned by ``getFile``.

        Returns:
            URL used to download file bytes for this bot token.
        """

        return f"{self.config.api_base}/file/bot{self.config.bot_token}/{file_path}"

    def api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Call one Telegram Bot API method using JSON POST.

        Args:
            method:
                Telegram Bot API method name.
            payload:
                JSON-serializable request payload.

        Returns:
            Decoded Telegram response dictionary.

        Raises:
            TelegramBridgeError:
                Raised for transport failures, Telegram API errors or malformed
                responses.
        """

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.api_url(method),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.URLError as exc:
            raise TelegramBridgeError(f"Telegram API request failed: {exc}") from exc

        if not data.get("ok"):
            raise TelegramBridgeError(f"Telegram API returned an error: {data}")

        return data

    def get_me(self) -> dict[str, Any]:
        """
        Return Telegram bot metadata.

        Returns:
            Telegram ``User`` object for the configured bot.

        Raises:
            TelegramBridgeError:
                Raised when Telegram does not return a dictionary result.
        """

        data = self.api_call("getMe", {})
        result = data.get("result")
        if not isinstance(result, dict):
            raise TelegramBridgeError(f"Telegram getMe returned an invalid response: {data}")
        return result

    def get_file(self, file_id: str) -> dict[str, Any]:
        """
        Return Telegram file metadata for a file id.

        Args:
            file_id:
                Telegram file identifier from a message attachment.

        Returns:
            Telegram file metadata dictionary containing ``file_path``.
        """

        data = self.api_call("getFile", {"file_id": file_id})
        result = data.get("result")
        if not isinstance(result, dict):
            raise TelegramBridgeError(f"Telegram getFile returned an invalid response: {data}")
        return result

    def download_file(self, file_path: str, destination: str) -> str:
        """
        Download one Telegram file to a local path.

        Args:
            file_path:
                Telegram file path returned by ``getFile``.
            destination:
                Local destination path.

        Returns:
            Destination path.
        """

        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            with request.urlopen(self.file_url(file_path), timeout=60) as response:
                target.write_bytes(response.read())
        except error.URLError as exc:
            raise TelegramBridgeError(f"Telegram file download failed: {exc}") from exc

        return str(target)

    def build_message_text(self, approval: TelegramApprovalRequest) -> str:
        """
        Build a Telegram message according to the selected verbosity.

        Args:
            approval:
                Approval request to render for the human operator.

        Returns:
            Plain-text Telegram message. The returned text intentionally avoids
            Markdown formatting so shell characters cannot break rendering.
        """

        verbosity = approval.verbosity or self.config.verbosity
        command = approval.command or "<command not detected>"

        if verbosity == "compact":
            return (
                f"Durex approval required\n\n"
                f"Task: {approval.task_title}\n"
                f"Command: {command}\n\n"
                f"Approve?"
            )

        if verbosity == "verbose":
            return (
                f"Durex approval required\n\n"
                f"Task: {approval.task_title}\n"
                f"Task ID: {approval.task_id}\n"
                f"Directory: {approval.workdir}\n"
                f"Command: {command}\n"
                f"Reason: {approval.reason}\n\n"
                f"Recent terminal context:\n"
                f"{approval.context}\n\n"
                f"Approve this action?"
            )

        return (
            f"Durex approval required\n\n"
            f"Task: {approval.task_title}\n"
            f"Task ID: {approval.task_id}\n"
            f"Directory: {approval.workdir}\n"
            f"Command: {command}\n"
            f"Reason: {approval.reason}\n\n"
            f"Approve this action?"
        )

    def build_inline_keyboard(self, request_id: str) -> dict[str, Any]:
        """
        Build Telegram inline keyboard payload.

        Callback data is intentionally compact because Telegram limits callback
        data length.

        Args:
            request_id:
                Approval request fingerprint embedded into callback data.

        Returns:
            Telegram ``reply_markup`` payload with approval actions.
        """

        return {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"durex:{request_id}:approve"},
                    {"text": "Deny", "callback_data": f"durex:{request_id}:deny"},
                ],
                [
                    {"text": "Show context", "callback_data": f"durex:{request_id}:show_context"},
                    {"text": "Stop task", "callback_data": f"durex:{request_id}:stop"},
                ],
            ]
        }

    def send_message(self, text: str, reply_markup: Optional[dict[str, Any]] = None) -> int:
        """
        Send a message to the configured chat and return message_id.

        Args:
            text:
                Message body.
            reply_markup:
                Optional Telegram inline keyboard payload.

        Returns:
            Telegram message id.

        Raises:
            TelegramBridgeError:
                Raised when the Telegram API call fails.
        """

        payload: dict[str, Any] = {
            "chat_id": self.config.allowed_chat_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        data = self.api_call("sendMessage", payload)
        return int(data["result"]["message_id"])

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> None:
        """
        Acknowledge one Telegram inline callback query.

        Args:
            callback_query_id:
                Telegram callback query id.
            text:
                Optional short toast text shown by Telegram.

        Returns:
            None.
        """

        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.api_call("answerCallbackQuery", payload)

    def send_approval_request(self, approval: TelegramApprovalRequest) -> int:
        """
        Send the approval request message with inline buttons.

        Args:
            approval:
                Approval request rendered into text and inline buttons.

        Returns:
            Telegram message id for the sent approval prompt.
        """

        return self.send_message(
            text=self.build_message_text(approval),
            reply_markup=self.build_inline_keyboard(approval.request_id),
        )

    def send_context(self, approval: TelegramApprovalRequest) -> None:
        """
        Send a longer context message when the user taps Show context.

        Args:
            approval:
                Approval request whose context should be displayed.

        Returns:
            None.
        """

        text = (
            f"Context for request {approval.request_id}\n\n"
            f"Task: {approval.task_title}\n"
            f"Directory: {approval.workdir}\n\n"
            f"Terminal context:\n{approval.context}"
        )
        self.send_message(text=text)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """
        Acknowledge a Telegram inline-button callback.

        Args:
            callback_query_id:
                Telegram callback query id.
            text:
                Optional short acknowledgement shown by Telegram.

        Returns:
            None.
        """

        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.api_call("answerCallbackQuery", payload)

    def poll_updates(self, timeout: int = 20, allowed_updates: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """
        Poll Telegram updates.

        Long polling keeps the connection open for up to timeout seconds. The
        returned updates may include normal messages, callback queries and other
        Telegram events.

        Args:
            timeout:
                Telegram long-poll timeout in seconds.
            allowed_updates:
                Optional Telegram update type filter.

        Returns:
            Raw update dictionaries. The bridge advances its offset after every
            received update to avoid processing the same callback repeatedly.
        """

        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": allowed_updates or ["callback_query"],
        }

        if self._last_update_id is not None:
            payload["offset"] = self._last_update_id + 1

        data = self.api_call("getUpdates", payload)
        updates = data.get("result", [])

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._last_update_id = update_id

        return updates

    def discover_chat_ids(self, timeout: int = 0) -> list[int]:
        """
        Poll recent updates and return chat ids that can be used for config.

        Args:
            timeout:
                Long-poll timeout used for discovery.

        Returns:
            Unique chat ids extracted from recent messages/callbacks.
        """

        return extract_chat_ids_from_updates(
            self.poll_updates(timeout=timeout, allowed_updates=["message", "callback_query"])
        )

    def parse_callback(self, update: dict[str, Any], expected_request_id: str) -> Optional[TelegramApprovalDecision]:
        """
        Convert one Telegram callback update into an approval decision.

        The callback must:
        - come from the allowed chat id;
        - use callback_data format durex:request_id:action;
        - refer to the expected request id.

        Args:
            update:
                Raw Telegram update dictionary.
            expected_request_id:
                Request id currently waiting for a decision.

        Returns:
            TelegramApprovalDecision when the update is a valid matching
            callback, otherwise None.
        """

        callback = update.get("callback_query")
        if not callback:
            return None

        message = callback.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if int(chat_id) != self.config.allowed_chat_id:
            return None

        data = callback.get("data", "")
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "durex":
            return None

        _, request_id, action_text = parts
        if request_id != expected_request_id:
            return None

        try:
            action = TelegramDecisionAction(action_text)
        except ValueError:
            return None

        self.answer_callback_query(callback.get("id", ""), text=f"Durex: {action.value}")

        user = callback.get("from", {})
        return TelegramApprovalDecision(
            request_id=request_id,
            action=action,
            source="telegram",
            telegram_user_id=user.get("id"),
            telegram_message_id=message.get("message_id"),
        )

    def wait_for_decision(self, approval: TelegramApprovalRequest) -> TelegramApprovalDecision:
        """
        Wait until the user decides or the approval timeout expires.

        Show context is handled inside this loop. It does not finish the
        approval request; after sending context, the bridge keeps waiting for an
        approve, deny or stop decision.
        """

        deadline = time.time() + self.config.approval_timeout_seconds

        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            poll_timeout = min(20, remaining)

            for update in self.poll_updates(timeout=poll_timeout):
                decision = self.parse_callback(update, expected_request_id=approval.request_id)
                if decision is None:
                    continue

                if decision.action == TelegramDecisionAction.SHOW_CONTEXT:
                    self.send_context(approval)
                    continue

                return decision

        return TelegramApprovalDecision(
            request_id=approval.request_id,
            action=self.config.timeout_default_decision,
            source="timeout",
        )


def _demo() -> None:
    """
    Manual demo.

    Required environment variables:
        DUREX_TELEGRAM_BOT_TOKEN
        DUREX_TELEGRAM_CHAT_ID

    Run:
        python3 telegram_bridge.py

    Returns:
        None. The demo sends a real Telegram request and prints the decision.
    """

    bridge = TelegramApprovalBridge.from_env(approval_timeout_seconds=120)
    approval = TelegramApprovalRequest(
        request_id="demo123",
        task_id=1,
        task_title="Demo task",
        workdir=os.getcwd(),
        command="pytest -q",
        reason="Manual demonstration of Telegram approval bridge.",
        context="Codex wants to run:\n$ pytest -q\nApprove this command? [y/N]",
        verbosity="normal",
    )

    message_id = bridge.send_approval_request(approval)
    print(f"Sent approval request message_id={message_id}")
    decision = bridge.wait_for_decision(approval)
    print(decision)


if __name__ == "__main__":
    _demo()
