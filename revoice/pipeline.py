"""The five stages, wired together.

    1. extract   video → 16 kHz mono WAV                    (media.extract_audio)
    2. transcribe WAV → Deepgram → transcript.json          (stt + timeline)   ← editable
    3. synthesize each segment → Cartesia                    (tts)
    4. fit + assemble  every clip stretched into its original slot on a fixed-length
                       silent canvas, so pauses and total duration survive exactly
    5. mux       new track back into the video, video stream copied

Stages 2–5 are independently runnable, which is what makes the editable transcript useful:
edit transcript.json (or the web editor), re-run stage 3 onward, and only the segments whose
text actually changed hit the TTS API again — the rest come from the on-disk cache.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import _http, audio, media, stt, timeline, tts
from .config import Options
from .timeline import Segment, Transcript

Progress = Callable[[str, float, str], None]


class FatalApiError(RuntimeError):
    """The provider refused the whole run (bad key, no credits) — retrying won't help."""


def _noop(stage: str, fraction: float, message: str) -> None:
    pass


# --------------------------------------------------------------------------- reports


@dataclass
class SegmentResult:
    index: int
    text: str
    start: float
    target: float                 # the slot's duration in the original
    tts_seconds: float = 0.0      # what Cartesia produced
    tempo: float = 1.0            # time-stretch applied (>1 = sped up)
    final_seconds: float = 0.0    # after stretching/trimming
    placed_start: float = 0.0     # where it actually landed
    drift: float = 0.0            # placed_start - start
    overflow: float = 0.0         # how far it ran past its slot
    retried: bool = False
    cached: bool = False
    skipped: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items()
        }


@dataclass
class SynthesisReport:
    track_path: str = ""
    duration: float = 0.0
    source_duration: float = 0.0
    segments: list[SegmentResult] = field(default_factory=list)
    api_calls: int = 0
    cache_hits: int = 0
    seconds: float = 0.0

    def summary(self) -> dict:
        placed = [s for s in self.segments if not s.skipped and not s.error]
        drifts = [abs(s.drift) for s in placed]
        return {
            "segments": len(self.segments),
            "spoken": len(placed),
            "skipped": sum(1 for s in self.segments if s.skipped),
            "failed": sum(1 for s in self.segments if s.error),
            "stretched": sum(1 for s in placed if abs(s.tempo - 1.0) > 0.01),
            "max_tempo": round(max([s.tempo for s in placed], default=1.0), 3),
            "max_drift": round(max(drifts, default=0.0), 3),
            "mean_drift": round(sum(drifts) / len(drifts), 4) if drifts else 0.0,
            "overflowing": sum(1 for s in placed if s.overflow > 0.05),
            "api_calls": self.api_calls,
            "cache_hits": self.cache_hits,
            "duration": round(self.duration, 3),
            "source_duration": round(self.source_duration, 3),
            "length_delta": round(self.duration - self.source_duration, 4),
            "seconds": round(self.seconds, 1),
        }

    def to_dict(self) -> dict:
        return {
            "track_path": self.track_path,
            "summary": self.summary(),
            "segments": [s.to_dict() for s in self.segments],
        }


# ------------------------------------------------------------------ stage 1: extract

STT_SAMPLE_RATE = 16000  # Deepgram's native rate; more is wasted upload


def extract_stage(src: str | Path, work: Path, opts: Options, progress: Progress = _noop):
    """video/audio file → (MediaInfo, path to 16 kHz mono WAV, exact duration in seconds)."""
    src = Path(src).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"input file not found: {src}")

    progress("extract", 0.1, f"probing {src.name}")
    info = media.probe(src)
    if not info.has_audio:
        raise media.MediaError(f"{src.name} has no audio track to work from")

    work.mkdir(parents=True, exist_ok=True)
    wav = work / "source_16k.wav"
    progress("extract", 0.4, "demuxing audio")
    media.extract_audio(src, wav, sample_rate=STT_SAMPLE_RATE, mono=True)

    # The canvas length comes from the extracted PCM's own frame count, not from container
    # metadata — that is what makes "same length" exact rather than approximately right.
    pcm, rate = audio.read_wav(wav)
    duration = audio.duration_of(pcm, rate)
    progress("extract", 1.0, f"{duration:.2f}s of audio at {rate} Hz")
    return info, wav, duration


# --------------------------------------------------------------- stage 2: transcribe


def transcribe_stage(
    src: str | Path, work: Path, opts: Options, progress: Progress = _noop
) -> Transcript:
    """Run stage 1 + 2 and write the editable transcript.json / transcript.srt."""
    opts = opts.resolved()
    info, wav, duration = extract_stage(src, work, opts, progress)

    progress("transcribe", 0.2, f"sending {duration:.0f}s to Deepgram ({opts.stt_model})")
    response = stt.transcribe_file(wav, opts)
    (work / "deepgram.json").write_text(json.dumps(response, indent=2))

    progress("transcribe", 0.8, "building timeline")
    transcript = timeline.from_deepgram(
        response,
        source=str(Path(src).expanduser()),
        audio_path=str(wav),
        duration=duration,
        language=opts.language,
        stt_model=opts.stt_model,
        utt_split=opts.utt_split,
        max_segment_chars=opts.max_segment_chars,
    )
    transcript.save(work / "transcript.json")
    (work / "transcript.srt").write_text(transcript.to_srt())

    if not transcript.segments:
        progress("transcribe", 1.0, "no speech detected")
    else:
        progress(
            "transcribe",
            1.0,
            f"{len(transcript.segments)} segments, "
            f"{transcript.speech_duration:.1f}s speech / {duration - transcript.speech_duration:.1f}s silence",
        )
    return transcript


# ------------------------------------------------- stages 3+4: synthesize, fit, place


def _cache_key(seg: Segment, opts: Options, voice_id: str, speed) -> str:
    payload = "|".join(
        [seg.text, voice_id, opts.tts_model, opts.language, str(opts.sample_rate), str(speed)]
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def _render(
    seg: Segment,
    opts: Options,
    cache: Path,
    scratch: Path,
    state: dict,
) -> tuple[bytes, SegmentResult]:
    """Synthesize one segment and stretch it to fit its slot. Runs on a worker thread."""
    result = SegmentResult(index=seg.index, text=seg.text, start=seg.start, target=seg.duration)
    voice_id = seg.voice_id or opts.voice_id

    def render_at(speed) -> tuple[bytes, float]:
        key = _cache_key(seg, opts, voice_id, speed)
        wav_path = cache / f"{key}.wav"
        if not wav_path.exists():
            raw = tts.synthesize(seg.text, opts, voice_id=voice_id, speed=speed)
            # Temp names carry the segment index: two segments with identical text render
            # concurrently and must not fight over the same scratch file.
            raw_path = scratch / f"{key}.{seg.index}.raw.wav"
            staged = scratch / f"{key}.{seg.index}.norm.wav"
            raw_path.write_bytes(raw)
            media.normalize_wav(raw_path, staged, sample_rate=opts.sample_rate)
            staged.replace(wav_path)  # atomic publish into the shared cache
            raw_path.unlink(missing_ok=True)
            with state["lock"]:
                state["api_calls"] += 1
        else:
            with state["lock"]:
                state["cache_hits"] += 1
            result.cached = True
        pcm, _ = audio.read_wav(wav_path)
        return pcm, audio.duration_of(pcm, opts.sample_rate)

    pcm, generated = render_at(None)
    result.tts_seconds = generated

    # Ask the model to speak faster before resorting to time-stretching — a genuinely faster
    # take sounds better than a WSOLA-compressed normal one.
    if (
        opts.adaptive_retry
        and state.get("speed_ok", True)
        and result.target > 0
        and generated / result.target > opts.retry_threshold
    ):
        try:
            fast_pcm, fast_generated = render_at("fast")
            result.retried = True
            if abs(fast_generated - result.target) < abs(generated - result.target):
                pcm, generated = fast_pcm, fast_generated
                result.tts_seconds = generated
        except tts.SpeedUnsupported:
            state["speed_ok"] = False  # model doesn't take `speed`; stop trying for this run

    # ---- fit to the slot
    target = result.target
    tempo = 1.0
    if target > 0.05 and generated > 0:
        ratio = generated / target
        if ratio > 1.0:                     # too long → speed up (capped)
            tempo = min(ratio, opts.max_tempo)
        elif opts.fit_mode == "exact":      # too short → only stretch in exact mode
            tempo = max(ratio, opts.min_tempo)

    if abs(tempo - 1.0) > 0.01:
        key = _cache_key(seg, opts, voice_id, None)
        stretched = scratch / f"{key}.{seg.index}.{tempo:.4f}.wav"
        src_wav = scratch / f"{key}.{seg.index}.fit-src.wav"
        audio.write_wav(src_wav, pcm, opts.sample_rate)
        media.time_stretch(src_wav, stretched, tempo, sample_rate=opts.sample_rate)
        pcm, _ = audio.read_wav(stretched)
        src_wav.unlink(missing_ok=True)
        stretched.unlink(missing_ok=True)
    result.tempo = tempo

    if opts.fit_mode == "exact" and target > 0:
        pcm = audio.fit_frames(pcm, int(round(target * opts.sample_rate)))

    result.final_seconds = audio.duration_of(pcm, opts.sample_rate)
    result.overflow = max(0.0, result.final_seconds - target)
    pcm = audio.apply_fades(pcm, opts.sample_rate, opts.fade_ms)
    return pcm, result


def _match_loudness(pcm: bytes, seg: Segment, source_pcm: bytes, opts: Options) -> bytes:
    """Scale the synthesized clip toward the loudness of the speech it replaces."""
    ref = audio.slice_frames(
        source_pcm,
        int(seg.start * STT_SAMPLE_RATE),
        int(seg.end * STT_SAMPLE_RATE),
    )
    source_rms, new_rms = audio.rms(ref), audio.rms(pcm)
    if source_rms < 50 or new_rms < 50:  # near-silence on either side → leave it alone
        return pcm
    return audio.scale(pcm, max(0.5, min(2.0, source_rms / new_rms)))


def synthesize_stage(
    transcript: Transcript,
    work: Path,
    opts: Options,
    progress: Progress = _noop,
    *,
    match_loudness: bool = True,
) -> SynthesisReport:
    """Stages 3 and 4: speak every segment, fit it to its slot, stamp it on the canvas."""
    opts = opts.resolved()
    started = time.time()
    work.mkdir(parents=True, exist_ok=True)
    cache = work / "cache"
    scratch = work / "scratch"
    cache.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)

    transcript.normalize()
    canvas = audio.Canvas.for_duration(transcript.duration, opts.sample_rate)
    report = SynthesisReport(
        duration=canvas.duration, source_duration=transcript.duration, track_path=""
    )

    todo = [s for s in transcript.segments if s.text and not s.skip]
    skipped = [
        SegmentResult(
            index=s.index, text=s.text, start=s.start, target=s.duration, skipped=True
        )
        for s in transcript.segments
        if not s.text or s.skip
    ]

    state = {"api_calls": 0, "cache_hits": 0, "speed_ok": True, "lock": threading.Lock()}
    rendered: dict[int, bytes] = {}
    results: dict[int, SegmentResult] = {r.index: r for r in skipped}
    done = 0

    def work_one(seg: Segment):
        nonlocal done
        blank = SegmentResult(index=seg.index, text=seg.text, start=seg.start, target=seg.duration)
        if state.get("fatal"):  # key/credits problem — don't burn calls on the rest
            blank.error = "not attempted (run aborted)"
            return seg.index, b"", blank
        try:
            pcm, result = _render(seg, opts, cache, scratch, state)
        except Exception as exc:  # keep going — one bad segment shouldn't lose the whole run
            blank.error = (
                exc.brief() if isinstance(exc, _http.ApiError) else f"{type(exc).__name__}: {exc}"
            )[:300]
            if isinstance(exc, _http.ApiError) and exc.fatal:
                with state["lock"]:
                    state.setdefault("fatal", exc.brief())
            result, pcm = blank, b""
        done += 1
        progress(
            "synthesize",
            done / max(1, len(todo)),
            f"segment {done}/{len(todo)}" + (f" — {result.error}" if result.error else ""),
        )
        return seg.index, pcm, result

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
            for index, pcm, result in pool.map(work_one, todo):
                rendered[index] = pcm
                results[index] = result

    # A bad key or an exhausted plan can't produce a usable track. Persist what we learned,
    # then fail loudly instead of muxing a mostly-silent video that looks like a success.
    if state.get("fatal"):
        report.segments = [results[i] for i in sorted(results)]
        report.api_calls, report.cache_hits = state["api_calls"], state["cache_hits"]
        report.seconds = time.time() - started
        (work / "report.json").write_text(json.dumps(report.to_dict(), indent=2))
        shutil.rmtree(scratch, ignore_errors=True)
        raise FatalApiError(state["fatal"])

    # ---- placement: stamp each clip at its original start, cascading only on collision
    source_pcm = b""
    if match_loudness:
        try:
            source_pcm, _ = audio.read_wav(transcript.audio_path)
        except (OSError, ValueError):
            match_loudness = False

    min_gap_frames = int(opts.min_gap * opts.sample_rate)
    cursor = 0
    for seg in transcript.segments:
        result = results[seg.index]
        pcm = rendered.get(seg.index, b"")
        if not pcm:
            continue
        if match_loudness and source_pcm:
            pcm = _match_loudness(pcm, seg, source_pcm, opts)

        want = int(round(seg.start * opts.sample_rate))
        at = want if cursor == 0 else max(want, cursor + min_gap_frames)
        start_frame, end_frame = canvas.place(at, pcm)
        cursor = end_frame
        result.placed_start = start_frame / opts.sample_rate
        result.drift = result.placed_start - seg.start
        result.final_seconds = (end_frame - start_frame) / opts.sample_rate

    track = work / "revoiced.wav"
    canvas.to_wav(track)
    shutil.rmtree(scratch, ignore_errors=True)

    report.track_path = str(track)
    report.segments = [results[i] for i in sorted(results)]
    report.api_calls = state["api_calls"]
    report.cache_hits = state["cache_hits"]
    report.seconds = time.time() - started
    (work / "report.json").write_text(json.dumps(report.to_dict(), indent=2))

    progress("synthesize", 1.0, f"track built: {report.duration:.2f}s")
    return report


# -------------------------------------------------------------- optional: voice clone


def clone_voice_from_source(
    src: str | Path,
    transcript: Transcript,
    work: Path,
    opts: Options,
    progress: Progress = _noop,
    *,
    seconds: float = 15.0,
) -> dict:
    """Clone the source speaker into a new Cartesia voice and return the voice object.

    Opt-in: only called for --clone / the UI toggle. Picks the densest window of speech in
    the recording (fewest pauses) so the clip is clean material rather than mostly silence.
    """
    if opts.resolved().tts_provider != "cartesia":
        raise RuntimeError(
            "voice cloning needs the Cartesia provider — Deepgram Aura only offers its "
            "fixed voice catalogue"
        )
    segments = [s for s in transcript.segments if s.duration > 0.2]
    if not segments:
        raise RuntimeError("no speech found to clone from")

    best, best_score = (segments[0].start, segments[0].end), 0.0
    for i, first in enumerate(segments):
        span_end, speech = first.end, 0.0
        for seg in segments[i:]:
            if seg.end - first.start > seconds:
                break
            span_end, speech = seg.end, speech + seg.duration
        if speech > best_score:
            best, best_score = (first.start, span_end), speech

    start, end = best
    clip = work / "clone_clip.wav"
    progress("clone", 0.3, f"clipping {end - start:.1f}s of source speech for cloning")
    media.ffmpeg([
        "-ss", f"{start:.3f}", "-t", f"{max(1.0, end - start):.3f}", "-i", str(Path(src).expanduser()),
        "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(clip),
    ])

    progress("clone", 0.6, "uploading to Cartesia")
    voice = tts.clone_voice(
        clip,
        name=f"revoice · {Path(src).stem}"[:80],
        language=opts.resolved().language,
    )
    if not voice.get("id"):
        raise RuntimeError(f"Cartesia clone returned no voice id: {voice}")
    (work / "cloned_voice.json").write_text(json.dumps(voice, indent=2))
    progress("clone", 1.0, f"cloned voice {voice['id']}")
    return voice


# ---------------------------------------------------------------------- stage 5: mux


def mux_stage(
    src: str | Path, track: str | Path, out: str | Path, opts: Options, progress: Progress = _noop
) -> Path:
    """Put the new audio back into the original container, copying the video stream."""
    src, out = Path(src).expanduser(), Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    info = media.probe(src)

    if not info.has_video:  # audio-only input → the track itself is the deliverable
        progress("mux", 0.5, "audio-only input, encoding audio output")
        if out.suffix.lower() == ".wav":
            shutil.copyfile(track, out)
        else:
            media.ffmpeg(["-i", str(track), "-b:a", opts.audio_bitrate, str(out)])
        progress("mux", 1.0, f"wrote {out.name}")
        return out

    progress("mux", 0.3, f"muxing (video stream copied, audio → {opts.audio_codec})")
    media.mux(
        src, track, out,
        keep_original_track=opts.keep_original_track,
        audio_codec=opts.audio_codec,
        audio_bitrate=opts.audio_bitrate,
    )
    progress("mux", 1.0, f"wrote {out.name}")
    return out


# ------------------------------------------------------------------------ everything


def run(
    src: str | Path,
    out: str | Path,
    work: Path,
    opts: Options,
    progress: Progress = _noop,
    *,
    transcript: Transcript | None = None,
) -> dict:
    """Full 1→5 run. Pass `transcript` to reuse (or replace) an edited one."""
    opts = opts.resolved()
    work.mkdir(parents=True, exist_ok=True)
    if transcript is None:
        transcript = transcribe_stage(src, work, opts, progress)
    report = synthesize_stage(transcript, work, opts, progress)
    output = mux_stage(src, report.track_path, out, opts, progress)
    return {
        "output": str(output),
        "track": report.track_path,
        "transcript": str(work / "transcript.json"),
        "srt": str(work / "transcript.srt"),
        "report": report.to_dict(),
    }
