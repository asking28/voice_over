"""Command line entry point.

    python3 -m revoice.cli run        video.mp4 -o out.mp4      # everything
    python3 -m revoice.cli transcribe video.mp4                 # stage 1+2 → transcript.json
    python3 -m revoice.cli synthesize work/transcript.json      # stage 3+4 → revoiced.wav
    python3 -m revoice.cli mux        video.mp4 revoiced.wav    # stage 5
    python3 -m revoice.cli voices                               # list Cartesia voices

The split exists so you can edit the transcript between stages 2 and 3.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import media, pipeline, tts
from .config import Options
from .timeline import Transcript

ROOT = Path(__file__).resolve().parent.parent


def _progress(stage: str, fraction: float, message: str) -> None:
    print(f"  [{stage:<10} {fraction:4.0%}] {message}", file=sys.stderr, flush=True)


def _work_dir(args, src: Path) -> Path:
    if args.work:
        return Path(args.work).expanduser()
    return ROOT / "work" / src.stem


def _options(args) -> Options:
    opts = Options()
    for name in vars(opts):
        value = getattr(args, name, None)
        if value is not None:
            setattr(opts, name, value)
    return opts.resolved()


def _default_out(src: Path, suffix: str | None = None) -> Path:
    return src.with_name(f"{src.stem}.revoiced{suffix or src.suffix}")


# --------------------------------------------------------------------------- printing


def _print_summary(report: pipeline.SynthesisReport, verbose: bool) -> None:
    summary = report.summary()
    print("\n  timing", file=sys.stderr)
    print(
        f"    source {summary['source_duration']:.3f}s → output {summary['duration']:.3f}s "
        f"(delta {summary['length_delta']:+.4f}s)",
        file=sys.stderr,
    )
    print(
        f"    drift: max {summary['max_drift']*1000:.0f} ms, mean {summary['mean_drift']*1000:.1f} ms",
        file=sys.stderr,
    )
    print(
        f"    {summary['spoken']} spoken / {summary['skipped']} skipped / {summary['failed']} failed"
        f" · {summary['stretched']} time-stretched (max {summary['max_tempo']:.2f}x)",
        file=sys.stderr,
    )
    print(
        f"    {summary['api_calls']} TTS calls, {summary['cache_hits']} cache hits, "
        f"{summary['seconds']:.1f}s wall clock",
        file=sys.stderr,
    )

    rows = report.segments if verbose else [s for s in report.segments if s.error or abs(s.drift) > 0.15]
    if rows:
        print(
            f"\n  {'#':>3} {'start':>7} {'slot':>6} {'tts':>6} {'tempo':>6} {'final':>6} {'drift':>7}  text",
            file=sys.stderr,
        )
        for s in rows:
            flag = "ERR " if s.error else ("SKIP " if s.skipped else "")
            print(
                f"  {s.index:>3} {s.start:>7.2f} {s.target:>6.2f} {s.tts_seconds:>6.2f} "
                f"{s.tempo:>6.2f} {s.final_seconds:>6.2f} {s.drift*1000:>+6.0f}ms  "
                f"{flag}{(s.error or s.text)[:56]}",
                file=sys.stderr,
            )
    print("", file=sys.stderr)


# -------------------------------------------------------------------------- commands


def cmd_transcribe(args) -> int:
    src = Path(args.input).expanduser()
    opts = _options(args)
    work = _work_dir(args, src)
    transcript = pipeline.transcribe_stage(src, work, opts, _progress)
    out = Path(args.output).expanduser() if args.output else work / "transcript.json"
    transcript.save(out)
    print(out)
    return 0


def cmd_synthesize(args) -> int:
    transcript = Transcript.load(Path(args.transcript).expanduser())
    work = Path(args.work).expanduser() if args.work else Path(args.transcript).expanduser().parent
    report = pipeline.synthesize_stage(transcript, work, _options(args), _progress)
    _print_summary(report, args.verbose)
    if args.output:
        Path(args.output).expanduser().write_bytes(Path(report.track_path).read_bytes())
        print(args.output)
    else:
        print(report.track_path)
    return 0


def cmd_mux(args) -> int:
    src = Path(args.input).expanduser()
    out = Path(args.output).expanduser() if args.output else _default_out(src)
    pipeline.mux_stage(src, Path(args.track).expanduser(), out, _options(args), _progress)
    print(out)
    return 0


def cmd_run(args) -> int:
    src = Path(args.input).expanduser()
    opts = _options(args)
    work = _work_dir(args, src)
    out = Path(args.output).expanduser() if args.output else _default_out(src)

    transcript = None
    if args.transcript:  # resume from an edited transcript, skipping STT
        transcript = Transcript.load(Path(args.transcript).expanduser())
        print(f"  using edited transcript: {args.transcript}", file=sys.stderr)
    else:
        transcript = pipeline.transcribe_stage(src, work, opts, _progress)

    if args.clone:
        voice = pipeline.clone_voice_from_source(src, transcript, work, opts, _progress)
        opts.voice_id = voice["id"]
        print(f"  cloned voice: {voice['id']}", file=sys.stderr)

    report = pipeline.synthesize_stage(transcript, work, opts, _progress)
    pipeline.mux_stage(src, report.track_path, out, opts, _progress)
    _print_summary(report, args.verbose)
    print(out)
    return 1 if any(s.error for s in report.segments) else 0


def cmd_voices(args) -> int:
    voices = tts.list_voices(args.provider)
    if args.json:
        print(json.dumps(voices, indent=2))
        return 0
    for voice in voices:
        mine = "*" if voice["is_owner"] else " "
        print(f"{mine} {voice['id']}  {voice['language']:<5} {voice['name'][:38]:<38} {voice['description'][:60]}")
    print(f"\n{len(voices)} voices ('*' = yours)", file=sys.stderr)
    return 0


def cmd_preview(args) -> int:
    """Hear one line in the chosen voice before committing a whole video to it."""
    import subprocess
    import tempfile

    text = args.text or (
        "Here's how this voice sounds. Your transcript will be spoken this way, pause for pause."
    )
    opts = _options(args)
    detail = opts.voice_id if opts.tts_provider == "deepgram" else f"{opts.voice_id} ({opts.tts_model})"
    print(f"  {opts.tts_provider}: {detail}", file=sys.stderr)
    wav = tts.synthesize(text, opts)
    out = Path(args.output).expanduser() if args.output else Path(tempfile.gettempdir()) / "revoice_preview.wav"
    out.write_bytes(wav)
    print(out)
    if not args.no_play:
        for player in (["afplay", str(out)], ["aplay", str(out)], ["ffplay", "-nodisp", "-autoexit", str(out)]):
            try:
                subprocess.run(player, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
    return 0


def cmd_probe(args) -> int:
    print(json.dumps(media.probe(Path(args.input).expanduser()).to_dict(), indent=2))
    return 0


# ----------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revoice",
        description="Re-voice a local video, preserving its exact pauses and total length.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, *, tts_opts: bool = True):
        p.add_argument("--work", help="working directory (default: ./work/<name>)")
        p.add_argument("-v", "--verbose", action="store_true", help="per-segment timing table")
        p.add_argument("--language", default=None, help="language code (default: en)")
        if not tts_opts:
            return
        p.add_argument(
            "--provider", dest="tts_provider", choices=["deepgram", "cartesia"], default=None,
            help="TTS provider (default: deepgram/Aura — same key as the STT stage)",
        )
        p.add_argument(
            "--voice-id", dest="voice_id", default=None,
            help="Cartesia voice UUID, or a Deepgram aura model name (aura-2-thalia-en)",
        )
        p.add_argument("--tts-model", dest="tts_model", default=None, help="Cartesia model (sonic-3.5)")
        p.add_argument("--sample-rate", dest="sample_rate", type=int, default=None)
        p.add_argument("--workers", type=int, default=None, help="parallel TTS requests")
        p.add_argument(
            "--fit-mode", dest="fit_mode", choices=["natural", "exact"], default=None,
            help="natural: speed up only when needed (default). exact: fill every slot exactly",
        )
        p.add_argument("--max-tempo", dest="max_tempo", type=float, default=None)
        p.add_argument("--min-tempo", dest="min_tempo", type=float, default=None)
        p.add_argument(
            "--room-tone", dest="room_tone", type=float, default=None,
            help="blend the source's own noise floor under the track so pauses aren't digital "
                 "silence (0 = off, default 0.9)",
        )
        p.add_argument("--fade-ms", dest="fade_ms", type=int, default=None)
        p.add_argument(
            "--no-adaptive", dest="adaptive_retry", action="store_false", default=None,
            help="never re-request a faster take; time-stretch only",
        )
        p.add_argument(
            "--keep-original-track", dest="keep_original_track", action="store_true", default=None,
            help="keep the source audio as a second track in the output",
        )
        p.add_argument(
            "--audio-codec", dest="audio_codec", default=None,
            choices=["aac", "alac", "flac", "pcm_s16le"],
            help="output audio codec; lossless ones keep the track sample-exact (default: aac)",
        )

    p_run = sub.add_parser("run", help="full pipeline: extract → STT → TTS → fit → mux")
    p_run.add_argument("input", help="local video (or audio) file")
    p_run.add_argument("-o", "--output", help="output file (default: <name>.revoiced.mp4)")
    p_run.add_argument("--transcript", help="skip STT and use this (edited) transcript.json")
    p_run.add_argument("--clone", action="store_true", help="clone the source speaker's voice first")
    p_run.add_argument(
        "--merge-gap", dest="merge_gap", type=float, default=None,
        help="fuse neighbouring segments closer than this many seconds, so one spoken "
             "sentence stays one TTS call (0 = off, default 0.18)",
    )
    add_common(p_run)
    p_run.set_defaults(func=cmd_run)

    p_tr = sub.add_parser("transcribe", help="stages 1+2 → editable transcript.json")
    p_tr.add_argument("input")
    p_tr.add_argument("-o", "--output")
    p_tr.add_argument("--stt-model", dest="stt_model", default=None)
    p_tr.add_argument("--utt-split", dest="utt_split", type=float, default=None,
                      help="gap (s) that starts a new segment, default 0.6")
    p_tr.add_argument("--merge-gap", dest="merge_gap", type=float, default=None,
                      help="then fuse neighbours closer than this, default 0.18 (0 = off)")
    p_tr.add_argument("--max-segment-chars", dest="max_segment_chars", type=int, default=None)
    p_tr.add_argument("--diarize", action="store_true", default=None, help="tag speakers")
    p_tr.add_argument("--no-filler-words", dest="filler_words", action="store_false", default=None)
    add_common(p_tr, tts_opts=False)
    p_tr.set_defaults(func=cmd_transcribe)

    p_syn = sub.add_parser("synthesize", help="stages 3+4 → full-length revoiced.wav")
    p_syn.add_argument("transcript", help="transcript.json (edit it first if you like)")
    p_syn.add_argument("-o", "--output", help="write the track here as well")
    add_common(p_syn)
    p_syn.set_defaults(func=cmd_synthesize)

    p_mux = sub.add_parser("mux", help="stage 5 → put a track back into the video")
    p_mux.add_argument("input")
    p_mux.add_argument("track")
    p_mux.add_argument("-o", "--output")
    add_common(p_mux)
    p_mux.set_defaults(func=cmd_mux)

    p_voices = sub.add_parser("voices", help="list available voices")
    p_voices.add_argument("--provider", choices=["deepgram", "cartesia"], default="deepgram")
    p_voices.add_argument("--json", action="store_true")
    p_voices.set_defaults(func=cmd_voices)

    p_prev = sub.add_parser("preview", help="speak one line so you can audition a voice")
    p_prev.add_argument("--text", help="what to say (default: a stock sample line)")
    p_prev.add_argument("-o", "--output", help="where to write the WAV")
    p_prev.add_argument("--no-play", action="store_true")
    add_common(p_prev)
    p_prev.set_defaults(func=cmd_preview)

    p_probe = sub.add_parser("probe", help="ffprobe summary of a local file")
    p_probe.add_argument("input")
    p_probe.set_defaults(func=cmd_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (media.MediaError, FileNotFoundError, RuntimeError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
