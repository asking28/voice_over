"""Environment loading and the tunable knobs shared by the CLI and the web backend."""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = Path(os.environ.get("REVOICE_JOBS_DIR") or ROOT / "jobs")

_ENV_LOADED = False
_ENV_LOCK = threading.Lock()


def load_env() -> None:
    """Load ROOT/.env into os.environ (existing vars win). Stdlib fallback if python-dotenv
    isn't installed, so the pipeline runs with a bare `python3 -m revoice.cli`.

    Guarded by a lock and only marked loaded *after* the work is done: worker threads call
    this concurrently, and a flag set up front would let the second thread through while the
    environment was still empty.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    with _ENV_LOCK:
        if _ENV_LOADED:  # another thread finished while we waited
            return
        path = ROOT / ".env"
        try:
            from dotenv import load_dotenv

            load_dotenv(path, override=False)
        except ImportError:
            if path.exists():
                for line in path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        _ENV_LOADED = True


def env(name: str, default: str = "") -> str:
    load_env()
    return os.environ.get(name) or default


def require_key(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"{name} is not set — add it to {ROOT / '.env'}")
    return value


# ---------------------------------------------------------------------------- defaults

DEFAULT_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"  # Cartesia public sample voice
DEFAULT_AURA_VOICE = "aura-2-thalia-en"                    # Deepgram Aura-2 default


@dataclass
class Options:
    """Every pipeline knob. The web UI posts a subset of these as JSON."""

    # --- voices / models
    tts_provider: str = "deepgram"      # deepgram (Aura) | cartesia (Sonic)
    voice_id: str = ""                  # Cartesia voice UUID, or a Deepgram aura model name
    tts_model: str = ""                 # Cartesia only — empty → CARTESIA_MODEL
    stt_model: str = ""                 # empty → DEEPGRAM_MODEL
    language: str = "en"

    # --- transcription / segmentation
    utt_split: float = 0.6              # a gap this long (s) starts a new utterance
    max_segment_chars: int = 320        # split longer utterances at sentence boundaries
    filler_words: bool = True           # keep "uh"/"um" so pause structure stays honest
    diarize: bool = False               # tag segments with a speaker id

    # --- synthesis
    sample_rate: int = 44100
    workers: int = 6                    # parallel Cartesia requests
    adaptive_retry: bool = True         # re-request at a faster rate before time-stretching
    retry_threshold: float = 1.18       # ...when generated/target exceeds this

    # --- duration fitting
    fit_mode: str = "natural"           # natural: speed up only when needed, pad with silence
                                        # exact:   stretch every segment to fill its slot
    max_tempo: float = 1.6              # hard cap on speed-up (higher = more chipmunk)
    min_tempo: float = 0.75             # hard cap on slow-down (exact mode only)
    fade_ms: int = 8                    # de-click fade on each placed segment
    min_gap: float = 0.03               # minimum silence kept between two segments

    # --- output
    keep_original_track: bool = False   # also keep the source audio as a 2nd audio track
    audio_codec: str = "aac"            # aac | alac | flac | pcm_s16le — see below
    audio_bitrate: str = "192k"         # ignored by the lossless codecs

    def resolved(self) -> "Options":
        """Fill blanks from the environment and keep the voice consistent with the provider."""
        out = Options(**asdict(self))
        out.tts_provider = (self.tts_provider or "deepgram").lower()
        out.tts_model = self.tts_model or env("CARTESIA_MODEL", "sonic-3.5")
        out.stt_model = self.stt_model or env("DEEPGRAM_MODEL", "nova-3")

        # A voice id belonging to the *other* provider would only produce a confusing 400,
        # so fall back to that provider's default instead of passing it through.
        voice = self.voice_id.strip()
        if out.tts_provider == "deepgram":
            looks_right = voice.startswith("aura")
            out.voice_id = voice if looks_right else env("DEEPGRAM_VOICE", DEFAULT_AURA_VOICE)
        else:
            looks_right = bool(voice) and not voice.startswith("aura")
            out.voice_id = voice if looks_right else env("CARTESIA_VOICE_ID", DEFAULT_VOICE_ID)
        return out

    @classmethod
    def from_dict(cls, data: dict | None) -> "Options":
        """Build from untrusted JSON — unknown keys ignored, types coerced."""
        opts = cls()
        if not data:
            return opts
        known = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key not in known or value is None or value == "":
                continue
            try:
                # bool first — bool is a subclass of int
                if isinstance(getattr(opts, key), bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                elif isinstance(getattr(opts, key), int):
                    value = int(value)
                elif isinstance(getattr(opts, key), float):
                    value = float(value)
                else:
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(opts, key, value)
        return opts

    def to_dict(self) -> dict:
        return asdict(self)
