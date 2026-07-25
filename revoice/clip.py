"""Cut sections out of a video and close the gap.

You mark the ranges to remove; everything else is kept and joined end to end, so the video
continues from the next kept moment. Two ways to do it:

    precise  one ffmpeg pass with select/aselect, re-encoding the video. Cuts land exactly
             where you put them. This is the default, because a cut that lands half a second
             from where you asked is worse than a re-encode.
    fast     stream-copy each kept range and concat them. No re-encode and near-instant, but
             every cut snaps to the nearest keyframe before it, so boundaries drift.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from . import media

Progress = Callable[[str, float, str], None]


def normalize_cuts(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Clamp to the media, drop empties, and merge cuts that touch or overlap."""
    spans = []
    for start, end in cuts:
        start, end = max(0.0, min(float(start), duration)), max(0.0, min(float(end), duration))
        if end - start > 0.001:
            spans.append((start, end))
    spans.sort()

    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 0.001:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def keep_ranges(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """What survives: the complement of the cuts."""
    keep, cursor = [], 0.0
    for start, end in normalize_cuts(cuts, duration):
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        keep.append((cursor, duration))
    return [(a, b) for a, b in keep if b - a > 0.02]


def plan(cuts: list[tuple[float, float]], duration: float) -> dict:
    """Summarize a cut list without touching the file — what the UI shows before you commit."""
    removed = normalize_cuts(cuts, duration)
    kept = keep_ranges(cuts, duration)
    removed_seconds = sum(b - a for a, b in removed)
    return {
        "cuts": [[round(a, 3), round(b, 3)] for a, b in removed],
        "keep": [[round(a, 3), round(b, 3)] for a, b in kept],
        "removed_seconds": round(removed_seconds, 3),
        "final_duration": round(max(0.0, duration - removed_seconds), 3),
        "source_duration": round(duration, 3),
        "pieces": len(kept),
    }


def _select_expr(kept: list[tuple[float, float]]) -> str:
    # `+` acts as OR across the ranges; between() is inclusive at both ends.
    return "+".join(f"between(t,{a:.4f},{b:.4f})" for a, b in kept)


def cut(
    src: str | Path,
    dst: str | Path,
    cuts: list[tuple[float, float]],
    *,
    duration: float = 0.0,
    mode: str = "precise",
    crf: int = 20,
    preset: str = "veryfast",
    audio_bitrate: str = "192k",
    progress: Progress | None = None,
) -> dict:
    """Remove `cuts` from `src`, writing the joined remainder to `dst`. Returns the plan."""
    src, dst = Path(src).expanduser(), Path(dst).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"input file not found: {src}")
    info = media.probe(src)
    duration = duration or info.duration

    outline = plan(cuts, duration)
    if not outline["keep"]:
        raise ValueError("those cuts would remove the entire file")
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not outline["cuts"]:  # nothing to remove — just hand back a copy
        if progress:
            progress("cut", 0.5, "no cuts marked — copying")
        shutil.copyfile(src, dst)
        if progress:
            progress("cut", 1.0, f"wrote {dst.name}")
        return outline

    kept = [tuple(k) for k in outline["keep"]]
    if progress:
        progress(
            "cut", 0.15,
            f"{len(outline['cuts'])} cut(s), {outline['removed_seconds']:.1f}s removed → "
            f"{outline['final_duration']:.1f}s in {len(kept)} piece(s)",
        )

    if mode == "fast":
        _cut_stream_copy(src, dst, kept, progress)
    else:
        _cut_precise(src, dst, kept, info, crf, preset, audio_bitrate, progress)

    if progress:
        progress("cut", 1.0, f"wrote {dst.name}")
    return outline


def _cut_precise(src, dst, kept, info, crf, preset, audio_bitrate, progress) -> None:
    """One pass: drop the unwanted frames/samples, then re-time what's left so it plays
    continuously. setpts/asetpts are what actually close the gap — without them the kept
    frames keep their original timestamps and the player just stalls through the hole."""
    if progress:
        progress("cut", 0.3, "re-encoding (exact cut points)")
    expression = _select_expr(kept)
    args = ["-i", str(src)]
    if info.has_video:
        args += [
            "-vf", f"select='{expression}',setpts=N/FRAME_RATE/TB",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        ]
    args += [
        "-af", f"aselect='{expression}',asetpts=N/SR/TB",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        str(dst),
    ]
    media.ffmpeg(args)


def _cut_stream_copy(src, dst, kept, progress) -> None:
    """Stream-copy each kept range, then concat. No re-encode; cuts snap to keyframes."""
    with tempfile.TemporaryDirectory(prefix="revoice-cut-") as tmp:
        tmpdir = Path(tmp)
        pieces = []
        for i, (start, end) in enumerate(kept):
            if progress:
                progress("cut", 0.2 + 0.6 * (i / max(1, len(kept))), f"piece {i + 1}/{len(kept)}")
            piece = tmpdir / f"piece{i:04d}{Path(src).suffix or '.mp4'}"
            media.ffmpeg([
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
                "-c", "copy", "-avoid_negative_ts", "make_zero", str(piece),
            ])
            pieces.append(piece)

        listing = tmpdir / "concat.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces))
        if progress:
            progress("cut", 0.85, "joining pieces")
        media.ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
            "-movflags", "+faststart", str(dst),
        ])
