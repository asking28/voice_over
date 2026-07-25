"""Audio presence: the waveform envelope, and where speech actually is.

Two questions the Cut tab needs answered about a file you haven't transcribed yet: what does
the audio look like, and which parts of it are silence. The envelope is computed here in
Python (slicing an array and taking max/min is C-speed, so a 6-minute file is milliseconds);
the silence detection is handed to ffmpeg's `silencedetect`, which is both faster and better
calibrated than anything worth hand-rolling.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import audio, media
from .config import JOBS_DIR

ANALYSIS_DIR = JOBS_DIR / "_analysis"
STT_SAMPLE_RATE = 16000


def analysis_dir(src: str | Path) -> Path:
    """A stable per-file scratch dir, keyed by path + mtime + size so an edited file
    re-analyses instead of serving a stale envelope."""
    path = Path(src).expanduser().resolve()
    stat = path.stat()
    key = hashlib.sha1(f"{path}|{int(stat.st_mtime)}|{stat.st_size}".encode()).hexdigest()[:16]
    return ANALYSIS_DIR / key


def ensure_audio(src: str | Path) -> tuple[Path, float]:
    """Extract (once) the 16 kHz mono WAV this module works from. Returns (path, duration)."""
    work = analysis_dir(src)
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "audio16k.wav"
    if not wav.exists():
        media.extract_audio(src, wav, sample_rate=STT_SAMPLE_RATE, mono=True)
    pcm, rate = audio.read_wav(wav)
    return wav, audio.duration_of(pcm, rate)


# Zooming re-reads the same file repeatedly, so hold the last decoded PCM. One entry is
# enough — you zoom around inside one file at a time.
_PCM_CACHE: dict[str, tuple[float, bytes, int]] = {}


def _load(wav_path: str | Path) -> tuple[bytes, int]:
    key = str(wav_path)
    mtime = Path(wav_path).stat().st_mtime
    cached = _PCM_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    pcm, rate = audio.read_wav(wav_path)
    _PCM_CACHE.clear()
    _PCM_CACHE[key] = (mtime, pcm, rate)
    return pcm, rate


def envelope(
    wav_path: str | Path,
    buckets: int = 1600,
    *,
    start: float = 0.0,
    end: float = 0.0,
    normalize: bool = True,
) -> dict:
    """Peak envelope over [start, end), normalized to 0..1 — one value per pixel of the graph.

    Zooming asks for a narrower window at the same bucket count, which is what makes the
    detail real rather than a stretched version of the full-file envelope.
    """
    pcm, rate = _load(wav_path)
    samples = audio._as_array(pcm)  # noqa: SLF001 — same package, avoids a copy
    duration = len(samples) / rate if rate else 0.0

    first = max(0, int(max(0.0, start) * rate))
    last = min(len(samples), int(end * rate)) if end > start else len(samples)
    span = samples[first:last]
    if not len(span):
        return {"peaks": [], "duration": duration, "sample_rate": rate, "start": start, "end": end}

    buckets = max(1, min(buckets, len(span)))
    width = len(span) / buckets
    peaks = []
    for i in range(buckets):
        a, b = int(i * width), max(int(i * width) + 1, int((i + 1) * width))
        chunk = span[a:b]
        peaks.append(max(max(chunk), -min(chunk)) / 32768.0)

    # Normalize against the WHOLE file, not the window — otherwise zooming into a quiet
    # passage would inflate it to look as loud as a shout.
    ceiling = (max(max(samples), -min(samples)) / 32768.0) if normalize else 1.0
    ceiling = ceiling or 1.0
    return {
        "peaks": [round(p / ceiling, 4) for p in peaks],
        "absolute_peak": round(ceiling, 4),
        "duration": duration,
        "sample_rate": rate,
        "start": round(first / rate, 4),
        "end": round(last / rate, 4),
    }


_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def silences(
    wav_path: str | Path, *, noise_db: float = -35.0, min_duration: float = 0.4
) -> list[tuple[float, float]]:
    """Silent stretches, via ffmpeg's silencedetect. Returns [(start, end), ...]."""
    log = media.ffmpeg_stderr([
        "-i", str(wav_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
        "-f", "null", "-",
    ])
    starts = [float(m) for m in _SILENCE_START.findall(log)]
    ends = [float(m) for m in _SILENCE_END.findall(log)]
    out = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else None
        if end is None:  # trailing silence runs to the end of the file
            end = float("inf")
        out.append((max(0.0, start), end))
    return out


def invert(spans: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Complement of a set of spans within [0, duration] — silence ↔ speech."""
    out, cursor = [], 0.0
    for start, end in sorted(spans):
        start, end = max(0.0, start), min(duration, end if end != float("inf") else duration)
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        out.append((cursor, duration))
    return [(round(a, 3), round(b, 3)) for a, b in out if b - a > 0.01]


@dataclass
class Analysis:
    source: str
    duration: float
    peaks: list[float]
    speech: list[tuple[float, float]]
    silence: list[tuple[float, float]]
    media: dict

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "duration": round(self.duration, 3),
            "peaks": self.peaks,
            "speech": [list(s) for s in self.speech],
            "silence": [list(s) for s in self.silence],
            "media": self.media,
            "speech_seconds": round(sum(b - a for a, b in self.speech), 2),
            "silence_seconds": round(sum(b - a for a, b in self.silence), 2),
        }


def analyze(
    src: str | Path, *, buckets: int = 1600, noise_db: float = -35.0, min_silence: float = 0.4
) -> Analysis:
    """Everything the Cut tab needs about a local file, in one call."""
    info = media.probe(src)
    if not info.has_audio:
        raise media.MediaError(f"{Path(src).name} has no audio track")

    wav, duration = ensure_audio(src)
    env = envelope(wav, buckets)
    quiet = silences(wav, noise_db=noise_db, min_duration=min_silence)
    quiet = [(a, min(b, duration) if b != float("inf") else duration) for a, b in quiet]

    return Analysis(
        source=str(Path(src).expanduser()),
        duration=duration,
        peaks=env["peaks"],
        speech=invert(quiet, duration),
        silence=[(round(a, 3), round(b, 3)) for a, b in quiet],
        media=info.to_dict(),
    )
