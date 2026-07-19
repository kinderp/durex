"""
Tests for voice transcription provider wiring.
"""

import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from voice_transcriber import (
    FasterWhisperTranscriber,
    StaticVoiceTranscriber,
    VoiceTranscriptionError,
    build_voice_transcriber,
)


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

    def test_model_initialization_failure_uses_transcription_error(self):
        """Model setup failures should remain inside the daemon error boundary."""

        class BrokenWhisperModel:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("model cache unavailable")

        fake_module = SimpleNamespace(WhisperModel=BrokenWhisperModel)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_module}):
            transcriber = FasterWhisperTranscriber(model_name="base")

            with self.assertRaisesRegex(
                VoiceTranscriptionError,
                "Could not load faster-whisper model base: model cache unavailable",
            ):
                transcriber.transcribe("/tmp/example.ogg", language="it")


if __name__ == "__main__":
    unittest.main()
