"""
Tests for approval_policy.py.

The policy engine is intentionally deterministic. These tests verify that the
same command always produces the expected classification.
"""

from approval_policy import (
    PolicyAction,
    action_from_string,
    default_policy,
    policy_from_dict,
)


def test_default_policy_auto_allows_pytest():
    decision = default_policy().classify_command("pytest -q")
    assert decision.action == PolicyAction.AUTO_ALLOW


def test_default_policy_auto_allows_git_status():
    decision = default_policy().classify_command("git status --short")
    assert decision.action == PolicyAction.AUTO_ALLOW


def test_default_policy_asks_for_git_push():
    decision = default_policy().classify_command("git push origin main")
    assert decision.action == PolicyAction.ASK_TELEGRAM


def test_default_policy_asks_for_dependency_installation():
    decision = default_policy().classify_command("pip install requests")
    assert decision.action == PolicyAction.ASK_TELEGRAM


def test_default_policy_denies_sudo():
    decision = default_policy().classify_command("sudo rm -rf /tmp/example")
    assert decision.action == PolicyAction.AUTO_DENY


def test_missing_command_requires_human_approval():
    decision = default_policy().classify_command(None)
    assert decision.action == PolicyAction.ASK_TELEGRAM


def test_action_aliases_are_supported():
    assert action_from_string("ask") == PolicyAction.ASK_TELEGRAM
    assert action_from_string("approve") == PolicyAction.AUTO_ALLOW
    assert action_from_string("deny") == PolicyAction.AUTO_DENY


def test_policy_can_be_loaded_from_dict():
    policy = policy_from_dict(
        {
            "default_decision": "ask",
            "auto_allow": ["example-safe*"],
            "ask_telegram": ["example-review*"],
            "auto_deny": ["example-blocked*"],
        }
    )

    assert policy.classify_command("example-safe run").action == PolicyAction.AUTO_ALLOW
    assert policy.classify_command("example-review run").action == PolicyAction.ASK_TELEGRAM
    assert policy.classify_command("example-blocked run").action == PolicyAction.AUTO_DENY


def test_default_action_is_used_when_no_rule_matches():
    policy = policy_from_dict({"default_decision": "ask"})
    decision = policy.classify_command("totally-unknown-command")

    assert decision.action == PolicyAction.ASK_TELEGRAM
    assert decision.matched_rule is None
