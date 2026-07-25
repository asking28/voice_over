"""ffmpeg / ffprobe wrappers: demux, probe, time-stretch, mux."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg", ".wmv", ".flv"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".caf"}


class MediaError(RuntimeError):
    pass


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(
            f"{name} not found on PATH. Install it with:  brew install ffmpeg"
        )
    return path


def run(args: list[str], *, timeout: int = 3600) -> str:
    """Run an ffmpeg-family command, raising with the tail of stderr on failure."""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise MediaError(f"{Path(args[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc.stdout


def ffmpeg(args: list[str], **kwargs) -> str:
    return run([_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args], **kwargs)


def ffmpeg_stderr(args: list[str], *, timeout: int = 3600) -> str:
    """Run ffmpeg and return stderr. Filters like silencedetect report their findings there,
    not on stdout, so the caller wants the log rather than the (empty) output."""
    proc = subprocess.run(
        [_tool("ffmpeg"), "-hide_banner", "-nostats", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise MediaError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
    return proc.stderr or ""


def ffprobe_json(path: str | Path) -> dict:
    out = run(
        [
            _tool("ffprobe"), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
    )
    return json.loads(out)


@dataclass
class MediaInfo:
    path: str
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    audio_sample_rate: int = 0
    audio_channels: int = 0
    video_codec: str = ""
    audio_codec: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def probe(path: str | Path) -> MediaInfo:
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or 0.0)
    if not duration:  # some containers only carry per-stream durations
        for stream in streams:
            duration = max(duration, float(stream.get("duration") or 0.0))

    fps = 0.0
    if video and video.get("avg_frame_rate", "0/0") != "0/0":
        num, _, den = video["avg_frame_rate"].partition("/")
        fps = float(num) / float(den) if float(den or 0) else 0.0

    return MediaInfo(
        path=str(path),
        duration=duration,
        # cover art in an mp3 shows up as a video stream — ignore those
        has_video=bool(video) and video.get("disposition", {}).get("attached_pic", 0) != 1,
        has_audio=bool(audio),
        width=int(video.get("width", 0)) if video else 0,
        height=int(video.get("height", 0)) if video else 0,
        fps=fps,
        audio_sample_rate=int(audio.get("sample_rate", 0)) if audio else 0,
        audio_channels=int(audio.get("channels", 0)) if audio else 0,
        video_codec=(video or {}).get("codec_name", ""),
        audio_codec=(audio or {}).get("codec_name", ""),
    )


# ------------------------------------------------------------------- step 1: extract


def extract_audio(src: str | Path, dst: str | Path, *, sample_rate: int = 16000, mono: bool = True) -> Path:
    """Demux the audio track to a PCM WAV. 16 kHz mono is what we hand to Deepgram."""
    ffmpeg([
        "-i", str(src),
        "-vn", "-sn", "-dn",
        "-ac", "1" if mono else "2",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(dst),
    ])
    return Path(dst)


def normalize_wav(src: str | Path, dst: str | Path, *, sample_rate: int) -> Path:
    """Force any audio file into the canonical working format: mono, pcm_s16le, sample_rate."""
    ffmpeg(["-i", str(src), "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)])
    return Path(dst)


# --------------------------------------------------------------- step 4: time-stretch


def atempo_chain(factor: float) -> list[float]:
    """Decompose a tempo factor into steps each inside atempo's well-behaved [0.5, 2.0] range.

    >>> atempo_chain(1.25)
    [1.25]
    >>> atempo_chain(5.0)
    [2.0, 2.0, 1.25]
    """
    steps: list[float] = []
    while factor > 2.0:
        steps.append(2.0)
        factor /= 2.0
    while factor < 0.5:
        steps.append(0.5)
        factor /= 0.5
    steps.append(round(factor, 6))
    return steps


def time_stretch(src: str | Path, dst: str | Path, factor: float, *, sample_rate: int) -> Path:
    """Change playback tempo by `factor` (>1 = faster/shorter) while preserving pitch.

    atempo is a phase-vocoder-free WSOLA implementation: cheap, and transparent for the
    ±30% range we normally need.
    """
    if abs(factor - 1.0) < 1e-4:
        return normalize_wav(src, dst, sample_rate=sample_rate)
    chain = ",".join(f"atempo={step:.6f}" for step in atempo_chain(factor))
    ffmpeg([
        "-i", str(src),
        "-filter:a", chain,
        "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le",
        str(dst),
    ])
    return Path(dst)


# ----------------------------------------------------------------------- step 5: mux


LOSSLESS_CODECS = {"alac", "flac", "pcm_s16le", "pcm_s24le", "copy"}


def mux(
    video: str | Path,
    audio: str | Path,
    dst: str | Path,
    *,
    keep_original_track: bool = False,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> Path:
    """Replace the video's audio with `audio`. The video stream is stream-copied — no
    re-encode, no quality loss, and the original frame timing is untouched.

    Note on exactness: AAC codes 1024 samples per frame, so an AAC track rounds *up* to the
    next frame boundary — up to ~23 ms of trailing silence past the assembled length. Pick a
    lossless `audio_codec` (alac in .mp4/.mov, or pcm_s16le in .mov/.mkv) when the audio
    track has to match the source sample count exactly.
    """

    def build(with_subs: bool) -> list[str]:
        args = ["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
        if keep_original_track:
            args += ["-map", "0:a:0?"]
        if with_subs:
            args += ["-map", "0:s?", "-c:s", "copy"]
        args += ["-c:v", "copy", "-c:a", audio_codec]
        if audio_codec not in LOSSLESS_CODECS:
            args += ["-b:a", audio_bitrate]
        args += ["-metadata:s:a:0", "title=revoice"]
        if keep_original_track:
            args += ["-metadata:s:a:1", "title=original", "-disposition:a:0", "default"]
        return args + ["-movflags", "+faststart", str(dst)]

    try:
        ffmpeg(build(True))  # carry any subtitle tracks across untouched
    except MediaError:
        ffmpeg(build(False))  # ...unless the container won't take them
    return Path(dst)


def encode_preview(src: str | Path, dst: str | Path) -> Path:
    """Web-playable copy for the browser preview (used when the source codec isn't H.264)."""
    ffmpeg([
        "-i", str(src),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-vf", "scale='min(1280,iw)':-2",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ])
    return Path(dst)
