"""Flask routes for Cloud Speech-to-Text transcription (voice input in UI)."""

from __future__ import annotations

import logging
import os
from typing import Any

from flask import Blueprint, jsonify, request
from google.api_core import exceptions as gcp_exceptions
from google.cloud.speech_v1 import SpeechClient
from google.cloud.speech_v1.types import cloud_speech

logger = logging.getLogger(__name__)

ui_speech_bp = Blueprint("ui_speech", __name__, url_prefix="/ui-api")

_MAX_AUDIO_BYTES = 10 * 1024 * 1024


def _speech_config() -> cloud_speech.RecognitionConfig:
    lang = (os.environ.get("SPEECH_LANGUAGE_CODE") or "en-US").strip()
    rate_raw = (os.environ.get("SPEECH_SAMPLE_RATE_HERTZ") or "48000").strip()
    try:
        sample_rate = int(rate_raw)
    except ValueError:
        sample_rate = 48000
    return cloud_speech.RecognitionConfig(
        encoding=cloud_speech.RecognitionConfig.AudioEncoding.WEBM_OPUS,
        sample_rate_hertz=sample_rate,
        language_code=lang,
    )


@ui_speech_bp.post("/speech/transcribe")
def transcribe() -> tuple[Any, int]:
    """Accept multipart ``audio`` file (WebM/Opus) and return ``{"text": "..."}``."""
    f = request.files.get("audio")
    if f is None or f.filename == "":
        return jsonify(error="missing_audio", detail="Expected multipart field 'audio'."), 400

    raw = f.read()
    if not raw:
        return jsonify(error="empty_audio"), 400
    if len(raw) > _MAX_AUDIO_BYTES:
        return jsonify(error="audio_too_large", detail=f"Max {_MAX_AUDIO_BYTES} bytes."), 400

    config = _speech_config()
    audio = cloud_speech.RecognitionAudio(content=raw)
    try:
        client = SpeechClient()
        response = client.recognize(config=config, audio=audio)
    except gcp_exceptions.GoogleAPICallError as e:
        logger.warning("Speech-to-Text API error: %s", e)
        return jsonify(error="speech_api_error", detail=str(e)), 503
    except Exception:
        logger.exception("Speech-to-Text transcribe failed")
        return jsonify(error="speech_failed", detail="Unexpected error calling Speech-to-Text."), 503

    pieces: list[str] = []
    for result in response.results:
        for alt in result.alternatives:
            t = (alt.transcript or "").strip()
            if t:
                pieces.append(t)
    text = " ".join(pieces).strip()
    return jsonify(text=text), 200
