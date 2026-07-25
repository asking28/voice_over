"""Deepgram pre-recorded transcription — word-level timings are the point.

We ask for utterances so the response already carries natural speech groupings with the
silence between them, and for filler words so "um"s occupy their real time instead of
collapsing into a pause that the TTS would then have to invent.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from . import _http
from .config import Options, require_key

LISTEN_URL = "https://api.deepgram.com/v1/listen"


def transcribe_file(wav_path: str | Path, opts: Options, *, timeout: int = 900) -> dict:
    """POST a WAV to Deepgram and return the raw JSON response."""
    opts = opts.resolved()
    params = {
        "model": opts.stt_model,
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "utt_split": f"{opts.utt_split}",
        "filler_words": "true" if opts.filler_words else "false",
        "diarize": "true" if opts.diarize else "false",
    }
    if opts.language:
        params["language"] = opts.language

    url = f"{LISTEN_URL}?{urllib.parse.urlencode(params)}"
    body = Path(wav_path).read_bytes()
    return _http.request_json(
        url,
        service="Deepgram",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Token {require_key('DEEPGRAM_API_KEY')}",
            "Content-Type": "audio/wav",
        },
        timeout=timeout,
    )
