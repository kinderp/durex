"""
Tests for voice transcription provider wiring.
"""

import unittest

from voice_transcriber import StaticVoiceTranscriber, VoiceTranscriptionError, build_voice_transcriber


class VoiceTranscriberTests(unittest.TestCase):
    """Tests that do not require the optional faster-whisper dependency."""

    def test_static_transcriber_returns_configured_result(self):
        """Static transcriber supports deterministic integration tests."""

        transcriber = StaticVoiceTranscriber("stato", language="it")
        result = transcriber.transcribe("/tmp/example.ogg")

        self.assertEqual(result.text, "stato")
        self.assertEqual(result.language, "it")
        self.assertEqual(result.provider, "static")
        self.assertEqual(result.confidence, 1.0)

    def test_build_voice_transcriber_rejects_unknown_provider(self):
        """Unsupported providers should fail with a clear project error."""

        with self.assertRaises(VoiceTranscriptionError):
            build_voice_transcriber(provider="unknown")

    def test_build_voice_transcriber_creates_faster_whisper_lazily(self):
        """Factory should not import faster-whisper until transcription."""

        transcriber = build_voice_transcriber(provider="faster_whisper", model_name="base")

        self.assertEqual(transcriber.provider_name, "faster_whisper")


if __name__ == "__main__":
    unittest.main()
