#!/usr/bin/env python3
"""
Local voice transcription providers for Durex.

The runtime implementation is optional and imported lazily so Durex can still
run its normal queue and Telegram tests without installing speech-to-text
dependencies. The intended private provider is faster-whisper running locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class VoiceTranscriptionError(RuntimeError):
    """Raised when local voice transcription cannot be completed."""


@dataclass(frozen=True)
class TranscriptionResult:
    """
    Speech-to-text result.

    Attributes:
        text:
            Transcribed text.
        language:
            Detected or requested language code.
        provider:
            Provider implementation name.
        confidence:
            Optional confidence-like score. Providers may leave it unset.
    """

    text: str
    language: Optional[str]
    provider: str
    confidence: Optional[float] = None


class VoiceTranscriber:
    """Interface implemented by local voice transcription providers."""

    provider_name = "base"

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> TranscriptionResult:
        """
        Transcribe an audio file.

        Args:
            audio_path:
                Local audio file path.
            language:
                Optional language hint such as ``it`` or ``en``.

        Returns:
            TranscriptionResult.

        Raises:
            VoiceTranscriptionError:
                Raised when transcription fails.
        """

        raise NotImplementedError


class FasterWhisperTranscriber(VoiceTranscriber):
    """
    Local faster-whisper transcriber.

    The model is loaded lazily on first transcription. This keeps process start
    fast and avoids requiring faster-whisper for users who do not enable voice.
    """

    provider_name = "faster_whisper"

    def __init__(self, model_name: str = "base", device: str = "cpu", compute_type: str = "int8") -> None:
        """
        Initialize provider settings without loading the model yet.

        Args:
            model_name:
                faster-whisper model name, for example ``base`` or ``small``.
            device:
                Device passed to WhisperModel.
            compute_type:
                Compute type passed to WhisperModel.
        """

        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _load_model(self):
        """
        Load and cache the faster-whisper model.

        Returns:
            WhisperModel instance.

        Raises:
            VoiceTranscriptionError:
                Raised when faster-whisper is not installed.
        """

        if self._model is not None:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceTranscriptionError(
                "Voice transcription requires faster-whisper. Install it with: "
                "pip install -r requirements-voice.txt"
            ) from exc

        try:
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        except Exception as exc:
            raise VoiceTranscriptionError(
                f"Could not load faster-whisper model {self.model_name}: {exc}"
            ) from exc
        return self._model

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> TranscriptionResult:
        """
        Transcribe audio with faster-whisper.

        Args:
            audio_path:
                Local audio file path.
            language:
                Optional language hint. Use None for automatic detection.

        Returns:
            TranscriptionResult with joined segment text.
        """

        model = self._load_model()
        try:
            segments, info = model.transcribe(audio_path, language=language)
            text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
        except Exception as exc:
            raise VoiceTranscriptionError(f"Voice transcription failed: {exc}") from exc

        detected_language = getattr(info, "language", language)
        probability = getattr(info, "language_probability", None)
        return TranscriptionResult(
            text=text,
            language=detected_language,
            provider=self.provider_name,
            confidence=probability,
        )


class StaticVoiceTranscriber(VoiceTranscriber):
    """Test helper that always returns a configured transcript."""

    provider_name = "static"

    def __init__(self, text: str, language: str = "it") -> None:
        self.text = text
        self.language = language

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> TranscriptionResult:
        return TranscriptionResult(
            text=self.text,
            language=language or self.language,
            provider=self.provider_name,
            confidence=1.0,
        )


def build_voice_transcriber(
    provider: str = "faster_whisper",
    model_name: str = "base",
    device: str = "cpu",
    compute_type: str = "int8",
) -> VoiceTranscriber:
    """
    Build a voice transcriber by provider name.

    Args:
        provider:
            Provider name. Currently ``faster_whisper`` is supported.
        model_name:
            Model name for faster-whisper.
        device:
            Device for faster-whisper.
        compute_type:
            Compute type for faster-whisper.

    Returns:
        VoiceTranscriber implementation.

    Raises:
        VoiceTranscriptionError:
            Raised for unsupported providers.
    """

    if provider == "faster_whisper":
        return FasterWhisperTranscriber(model_name=model_name, device=device, compute_type=compute_type)
    raise VoiceTranscriptionError(f"Unsupported voice transcription provider: {provider}")
