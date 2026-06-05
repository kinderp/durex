"""
Tests for approval_policy.py.

The policy engine is intentionally deterministic. These tests verify that the
same command always produces the expected classification.
"""

import unittest

from approval_policy import (
    PolicyAction,
    action_from_string,
    default_policy,
    policy_from_dict,
)


class ApprovalPolicyTests(unittest.TestCase):
    def test_default_policy_auto_allows_pytest(self):
        decision = default_policy().classify_command("pytest -q")
        self.assertEqual(decision.action, PolicyAction.AUTO_ALLOW)

    def test_default_policy_auto_allows_git_status(self):
        decision = default_policy().classify_command("git status --short")
        self.assertEqual(decision.action, PolicyAction.AUTO_ALLOW)

    def test_default_policy_asks_for_git_push(self):
        decision = default_policy().classify_command("git push origin main")
        self.assertEqual(decision.action, PolicyAction.ASK_TELEGRAM)

    def test_default_policy_asks_for_dependency_installation(self):
        decision = default_policy().classify_command("pip install requests")
        self.assertEqual(decision.action, PolicyAction.ASK_TELEGRAM)

    def test_default_policy_denies_sudo(self):
        decision = default_policy().classify_command("sudo rm -rf /tmp/example")
        self.assertEqual(decision.action, PolicyAction.AUTO_DENY)

    def test_missing_command_requires_human_approval(self):
        decision = default_policy().classify_command(None)
        self.assertEqual(decision.action, PolicyAction.ASK_TELEGRAM)

    def test_action_aliases_are_supported(self):
        self.assertEqual(action_from_string("ask"), PolicyAction.ASK_TELEGRAM)
        self.assertEqual(action_from_string("approve"), PolicyAction.AUTO_ALLOW)
        self.assertEqual(action_from_string("deny"), PolicyAction.AUTO_DENY)

    def test_policy_can_be_loaded_from_dict(self):
        policy = policy_from_dict(
            {
                "default_decision": "ask",
                "auto_allow": ["example-safe*"],
                "ask_telegram": ["example-review*"],
                "auto_deny": ["example-blocked*"],
            }
        )

        self.assertEqual(policy.classify_command("example-safe run").action, PolicyAction.AUTO_ALLOW)
        self.assertEqual(policy.classify_command("example-review run").action, PolicyAction.ASK_TELEGRAM)
        self.assertEqual(policy.classify_command("example-blocked run").action, PolicyAction.AUTO_DENY)

    def test_default_action_is_used_when_no_rule_matches(self):
        policy = policy_from_dict({"default_decision": "ask"})
        decision = policy.classify_command("totally-unknown-command")

        self.assertEqual(decision.action, PolicyAction.ASK_TELEGRAM)
        self.assertIsNone(decision.matched_rule)


if __name__ == "__main__":
    unittest.main()
