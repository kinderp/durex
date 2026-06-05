"""
Tests for queue output parsing helpers.
"""

import unittest

from codex_queue import extract_session_id


class SessionIdExtractionTests(unittest.TestCase):
    def test_extract_session_id_returns_latest_candidate(self):
        old_session = "11111111-1111-1111-1111-111111111111"
        new_session = "22222222-2222-2222-2222-222222222222"
        output = f"""
Session: {old_session}
Some retry output
resuming work with {new_session}
"""

        self.assertEqual(extract_session_id(output), new_session)

    def test_extract_session_id_supports_session_id_label(self):
        session = "33333333-3333-3333-3333-333333333333"

        self.assertEqual(extract_session_id(f"session_id: {session}"), session)


if __name__ == "__main__":
    unittest.main()
