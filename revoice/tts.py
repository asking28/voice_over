"""Text-to-speech, with two interchangeable providers.

    deepgram — Aura-2 (/v1/speak). Same key as the STT stage, so one provider covers the
               whole pipeline. No voice cloning, no speed control.
    cartesia — Sonic (/tts/bytes). Supports voice cloning and a speed hint.

Both return WAV bytes; the pipeline normalizes everything through ffmpeg afterwards, so
provider-specific sample rates never leak downstream.

Raw REST rather than the SDKs: the endpoint shapes are stable across SDK releases, and it
keeps the pipeline importable on a stock interpreter.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from . import _http
from .config import Options, env, require_key

TTS_URL = "https://api.cartesia.ai/tts/bytes"
VOICES_URL = "https://api.cartesia.ai/voices/"
CLONE_URL = "https://api.cartesia.ai/voices/clone"
SPEAK_URL = "https://api.deepgram.com/v1/speak"

# Aura-2 ships as a fixed catalogue — the model name *is* the voice, and there's no list
# endpoint, so the picker is populated from here.
AURA_VOICES = [
    ("aura-2-thalia-en", "Thalia", "f", "clear, confident, energetic"),
    ("aura-2-andromeda-en", "Andromeda", "f", "casual, expressive, comfortable"),
    ("aura-2-helena-en", "Helena", "f", "caring, natural, positive"),
    ("aura-2-apollo-en", "Apollo", "m", "confident, comfortable, casual"),
    ("aura-2-arcas-en", "Arcas", "m", "natural, smooth, clear"),
    ("aura-2-aries-en", "Aries", "m", "warm, energetic, caring"),
    ("aura-2-amalthea-en", "Amalthea", "f", "engaging, natural, cheerful"),
    ("aura-2-asteria-en", "Asteria", "f", "clear, confident, knowledgeable"),
    ("aura-2-athena-en", "Athena", "f", "calm, smooth, professional"),
    ("aura-2-atlas-en", "Atlas", "m", "enthusiastic, confident, approachable"),
    ("aura-2-aurora-en", "Aurora", "f", "cheerful, expressive, energetic"),
    ("aura-2-callista-en", "Callista", "f", "clear, energetic, professional"),
    ("aura-2-cora-en", "Cora", "f", "smooth, melodic, caring"),
    ("aura-2-cordelia-en", "Cordelia", "f", "approachable, warm, polite"),
    ("aura-2-delia-en", "Delia", "f", "casual, friendly, cheerful"),
    ("aura-2-draco-en", "Draco", "m", "warm, approachable, trustworthy (British)"),
    ("aura-2-electra-en", "Electra", "f", "professional, engaging, knowledgeable"),
    ("aura-2-harmonia-en", "Harmonia", "f", "empathetic, clear, calm"),
    ("aura-2-hera-en", "Hera", "f", "smooth, warm, professional"),
    ("aura-2-hermes-en", "Hermes", "m", "expressive, engaging, professional"),
    ("aura-2-hyperion-en", "Hyperion", "m", "caring, warm, empathetic (Australian)"),
    ("aura-2-iris-en", "Iris", "f", "cheerful, positive, approachable"),
    ("aura-2-janus-en", "Janus", "f", "southern, smooth, trustworthy"),
    ("aura-2-juno-en", "Juno", "f", "natural, engaging, melodic"),
    ("aura-2-jupiter-en", "Jupiter", "m", "expressive, knowledgeable, baritone"),
    ("aura-2-luna-en", "Luna", "f", "friendly, natural, engaging"),
    ("aura-2-mars-en", "Mars", "m", "smooth, patient, trustworthy"),
    ("aura-2-minerva-en", "Minerva", "f", "positive, friendly, natural"),
    ("aura-2-neptune-en", "Neptune", "m", "professional, patient, polite"),
    ("aura-2-odysseus-en", "Odysseus", "m", "calm, smooth, comfortable"),
    ("aura-2-ophelia-en", "Ophelia", "f", "expressive, enthusiastic, cheerful"),
    ("aura-2-orion-en", "Orion", "m", "approachable, comfortable, calm"),
    ("aura-2-orpheus-en", "Orpheus", "m", "professional, clear, confident"),
    ("aura-2-pandora-en", "Pandora", "f", "smooth, calm, melodic (British)"),
    ("aura-2-phoebe-en", "Phoebe", "f", "energetic, warm, casual"),
    ("aura-2-pluto-en", "Pluto", "m", "smooth, calm, empathetic"),
    ("aura-2-saturn-en", "Saturn", "m", "knowledgeable, confident, baritone"),
    ("aura-2-selene-en", "Selene", "f", "expressive, engaging, energetic"),
    ("aura-2-theia-en", "Theia", "f", "expressive, polite, sincere (Australian)"),
    ("aura-2-vesta-en", "Vesta", "f", "natural, expressive, patient"),
    ("aura-2-zeus-en", "Zeus", "m", "deep, trustworthy, smooth"),
]


def _headers(extra: dict | None = None) -> dict:
    headers = {
        "Cartesia-Version": env("CARTESIA_VERSION", "2024-11-13"),
        "X-API-Key": require_key("CARTESIA_API_KEY"),
    }
    headers.update(extra or {})
    return headers


class SpeedUnsupported(RuntimeError):
    """Raised when the model rejects the `speed` control, so the caller can stop sending it."""


def synthesize(
    text: str,
    opts: Options,
    *,
    voice_id: str = "",
    speed: str | float | None = None,
    timeout: int = 180,
) -> bytes:
    """text → WAV bytes, from whichever provider opts.tts_provider names.

    `speed` is an optional hint ("slow"/"normal"/"fast"). It's a nicety: exact duration is
    enforced later by time-stretching, but asking the model to speak faster sounds better
    than compressing a normal-paced take. Providers that don't support it raise
    SpeedUnsupported, which tells the pipeline to stop asking.
    """
    opts = opts.resolved()
    if opts.tts_provider == "deepgram":
        return _deepgram_speak(text, opts, voice_id=voice_id, speed=speed, timeout=timeout)
    return _cartesia_tts(text, opts, voice_id=voice_id, speed=speed, timeout=timeout)


# --------------------------------------------------------------------- Deepgram Aura

# linear16 accepts a fixed set of rates; we take the highest and let ffmpeg resample to
# whatever the job actually works at.
AURA_SAMPLE_RATE = 48000


def _deepgram_speak(
    text: str, opts: Options, *, voice_id: str = "", speed=None, timeout: int = 180
) -> bytes:
    if speed is not None:
        raise SpeedUnsupported("Deepgram Aura has no speed control")
    params = {
        "model": voice_id or opts.voice_id,
        "encoding": "linear16",
        "container": "wav",
        "sample_rate": str(AURA_SAMPLE_RATE),
    }
    return _http.request(
        f"{SPEAK_URL}?{urllib.parse.urlencode(params)}",
        service="Deepgram",
        data=json.dumps({"text": text}).encode(),
        method="POST",
        headers={
            "Authorization": f"Token {require_key('DEEPGRAM_API_KEY')}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )


# -------------------------------------------------------------------- Cartesia Sonic


def _cartesia_tts(
    text: str, opts: Options, *, voice_id: str = "", speed=None, timeout: int = 180
) -> bytes:
    payload: dict = {
        "model_id": opts.tts_model,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id or opts.voice_id},
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": opts.sample_rate,
        },
        "language": opts.language,
    }
    if speed is not None:
        payload["speed"] = speed

    try:
        return _http.request(
            TTS_URL,
            service="Cartesia",
            data=json.dumps(payload).encode(),
            method="POST",
            headers=_headers({"Content-Type": "application/json"}),
            timeout=timeout,
        )
    except _http.ApiError as exc:
        if speed is not None and exc.status in (400, 422) and "speed" in exc.detail.lower():
            raise SpeedUnsupported(exc.detail) from None
        raise


def list_voices(provider: str = "cartesia", limit: int = 100) -> list[dict]:
    """Voices available for a provider — used to populate the picker in the web UI."""
    if (provider or "").lower() == "deepgram":
        return [
            {
                "id": model,
                "name": f"{name} ({'♀' if gender == 'f' else '♂'})",
                "description": description,
                "language": "en",
                "is_owner": False,
            }
            for model, name, gender, description in AURA_VOICES
        ]

    data = _http.request_json(
        f"{VOICES_URL}?limit={limit}", service="Cartesia", headers=_headers(), timeout=60
    )
    voices = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(voices, list):
        return []
    return [
        {
            "id": v.get("id", ""),
            "name": v.get("name", ""),
            "description": (v.get("description") or "")[:160],
            "language": v.get("language", ""),
            "is_owner": bool(v.get("is_owner", False)),
        }
        for v in voices
        if v.get("id")
    ]


def clone_voice(
    clip_path: str | Path,
    *,
    name: str,
    language: str = "en",
    mode: str = "similarity",
    enhance: bool = True,
    timeout: int = 300,
) -> dict:
    """Clone the speaker in `clip_path` (a few seconds of clean speech) into a new voice.

    Opt-in only — cloning someone's voice is a decision the operator should make explicitly,
    so nothing calls this unless --clone / the UI toggle is set.
    """
    clip = Path(clip_path).read_bytes()
    body, content_type = _http.multipart(
        {
            "name": name[:80],
            "description": "Cloned by revoice from the source video",
            "language": language,
            "mode": mode,
            "enhance": "true" if enhance else "false",
        },
        {"clip": (Path(clip_path).name, clip, "audio/wav")},
    )
    return _http.request_json(
        CLONE_URL,
        service="Cartesia",
        data=body,
        method="POST",
        headers=_headers({"Content-Type": content_type}),
        timeout=timeout,
    )
