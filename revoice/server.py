"""FastAPI backend.

Input is always a **local file path** — nothing is uploaded. The browser sends a path, the
server reads it in place, and every artifact stays on disk next to the job.

    uvicorn revoice.server:app --port 8010     (or ./run.sh)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import _http, clip, media, tts, waveform
from .config import ROOT, Options, env
from .jobs import STORE
from .timeline import Transcript

WEB_DIR = ROOT / "web"
# The picker is confined to this subtree. It's a localhost tool, but there's no reason to
# expose the whole filesystem to a browser tab.
BROWSE_ROOT = Path(os.environ.get("REVOICE_BROWSE_ROOT", str(Path.home()))).expanduser().resolve()

app = FastAPI(title="revoice", version="0.1.0")


def _safe(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved != BROWSE_ROOT and BROWSE_ROOT not in resolved.parents:
        raise HTTPException(403, f"path outside {BROWSE_ROOT}")
    return resolved


# ------------------------------------------------------------------------------ meta


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "deepgram_key": bool(env("DEEPGRAM_API_KEY")),
        "cartesia_key": bool(env("CARTESIA_API_KEY")),
        "browse_root": str(BROWSE_ROOT),
        "defaults": Options().resolved().to_dict(),
    }


@app.get("/api/voices")
def voices(provider: str = Query("deepgram")) -> dict:
    try:
        return {"provider": provider, "voices": tts.list_voices(provider)}
    except Exception as exc:
        return JSONResponse({"voices": [], "error": str(exc)}, status_code=200)


SAMPLE_LINE = "Here's how this voice sounds. Your transcript will be spoken this way, pause for pause."


@app.post("/api/preview")
def preview_voice(payload: dict = Body(default={})):
    """One short line in the chosen voice — hear it before spending a run on it.

    Doubles as a preflight check: if the key is wrong or the plan is out of credits, you
    find out here for the price of one sentence instead of one call per segment.
    """
    opts = Options.from_dict(payload.get("options") or {}).resolved()
    text = str(payload.get("text") or "").strip()

    job_id, index = payload.get("job_id"), payload.get("segment")
    if not text and job_id and index is not None:  # preview a real line from the transcript
        job = STORE.get(str(job_id))
        if job and job.transcript_path.exists():
            segments = Transcript.load(job.transcript_path).segments
            match = next((s for s in segments if s.index == int(index)), None)
            if match:
                text = match.text
    text = (text or SAMPLE_LINE)[:220]  # a preview should cost one sentence, not a paragraph

    try:
        wav = tts.synthesize(text, opts)
    except _http.ApiError as exc:
        raise HTTPException(502, exc.brief())
    except Exception as exc:
        raise HTTPException(502, str(exc))
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"X-Preview-Text": text[:120], "Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------- file browsing


@app.get("/api/browse")
def browse(path: str = Query("", description="directory to list; defaults to $HOME")) -> dict:
    target = _safe(path or BROWSE_ROOT)
    if target.is_file():
        target = target.parent
    if not target.is_dir():
        raise HTTPException(404, f"not a directory: {target}")

    dirs, files = [], []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry), "is_dir": True})
                elif entry.suffix.lower() in media.VIDEO_SUFFIXES | media.AUDIO_SUFFIXES:
                    files.append({
                        "name": entry.name,
                        "path": str(entry),
                        "is_dir": False,
                        "size": entry.stat().st_size,
                        "kind": "video" if entry.suffix.lower() in media.VIDEO_SUFFIXES else "audio",
                    })
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"permission denied: {target}")

    parent = str(target.parent) if target != BROWSE_ROOT else ""
    shortcuts = [
        {"name": name, "path": str(Path.home() / name)}
        for name in ("Desktop", "Downloads", "Movies", "Documents")
        if (Path.home() / name).is_dir()
    ]
    return {"dir": str(target), "parent": parent, "entries": dirs + files, "shortcuts": shortcuts}


# --------------------------------------------------------------- audio presence / cutting


@app.post("/api/analyze")
def analyze(payload: dict = Body(...)) -> dict:
    """Waveform envelope + where speech and silence actually are, for the Cut tab.

    The extracted audio is cached per (path, mtime, size), so re-opening a file is instant but
    an edited file re-analyses.
    """
    target = _safe(str(payload.get("path") or ""))
    if not target.is_file():
        raise HTTPException(404, f"no such file: {target}")
    try:
        result = waveform.analyze(
            target,
            buckets=int(payload.get("buckets") or 1600),
            noise_db=float(payload.get("noise_db") or -35.0),
            min_silence=float(payload.get("min_silence") or 0.4),
        )
    except media.MediaError as exc:
        raise HTTPException(400, str(exc))
    return result.to_dict()


@app.post("/api/envelope")
def envelope(payload: dict = Body(...)) -> dict:
    """Peak envelope for a time window — what the Cut tab fetches as you zoom in."""
    target = _safe(str(payload.get("path") or ""))
    if not target.is_file():
        raise HTTPException(404, f"no such file: {target}")
    try:
        wav, _ = waveform.ensure_audio(target)
        return waveform.envelope(
            wav,
            buckets=int(payload.get("buckets") or 1600),
            start=float(payload.get("start") or 0.0),
            end=float(payload.get("end") or 0.0),
        )
    except media.MediaError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/silences")
def silences(payload: dict = Body(...)) -> dict:
    """Re-detect silence at a different noise floor / minimum length, without re-extracting."""
    target = _safe(str(payload.get("path") or ""))
    if not target.is_file():
        raise HTTPException(404, f"no such file: {target}")
    wav, duration = waveform.ensure_audio(target)
    quiet = waveform.silences(
        wav,
        noise_db=float(payload.get("noise_db") or -35.0),
        min_duration=float(payload.get("min_silence") or 0.4),
    )
    quiet = [(a, min(b, duration) if b != float("inf") else duration) for a, b in quiet]
    return {
        "silence": [[round(a, 3), round(b, 3)] for a, b in quiet],
        "speech": [list(s) for s in waveform.invert(quiet, duration)],
        "duration": round(duration, 3),
    }


@app.post("/api/clip/plan")
def clip_plan(payload: dict = Body(...)) -> dict:
    """What a cut list would produce — durations only, nothing written."""
    target = _safe(str(payload.get("path") or ""))
    if not target.is_file():
        raise HTTPException(404, f"no such file: {target}")
    duration = float(payload.get("duration") or 0.0) or media.probe(target).duration
    cuts = [(float(a), float(b)) for a, b in (payload.get("cuts") or [])]
    return clip.plan(cuts, duration)


@app.post("/api/clip")
def clip_run(payload: dict = Body(...)) -> dict:
    """Cut the marked ranges out and join the remainder. Runs as a background job."""
    target = _safe(str(payload.get("path") or ""))
    if not target.is_file():
        raise HTTPException(404, f"no such file: {target}")
    cuts = payload.get("cuts") or []
    if not cuts:
        raise HTTPException(400, "no cuts marked")

    output = str(payload.get("output_path") or "").strip()
    try:
        job = STORE.create_clip(
            str(target), cuts, output, mode=str(payload.get("mode") or "precise")
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    STORE.start_clip(job)
    return job.to_dict()


@app.get("/api/probe")
def probe(path: str = Query(...)) -> dict:
    target = _safe(path)
    if not target.is_file():
        raise HTTPException(404, f"no such file: {target}")
    try:
        return media.probe(target).to_dict()
    except media.MediaError as exc:
        raise HTTPException(400, str(exc))


# ------------------------------------------------------------------------------ jobs


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": STORE.list()}


@app.post("/api/jobs")
def create_job(payload: dict = Body(...)) -> dict:
    input_path = str(payload.get("input_path") or "").strip()
    if not input_path:
        raise HTTPException(400, "input_path is required")
    src = _safe(input_path)
    if not src.is_file():
        raise HTTPException(404, f"no such file: {src}")

    output_path = str(payload.get("output_path") or "").strip()
    try:
        job = STORE.create(str(src), payload.get("options") or {}, output_path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))

    STORE.start(
        job,
        full=bool(payload.get("full", False)),
        clone=bool(payload.get("clone", False)),
    )
    return job.to_dict()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, log: bool = True) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job.to_dict(with_log=log)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    if not STORE.delete(job_id):
        raise HTTPException(404, "no such job")
    return {"deleted": job_id}


# ------------------------------------------------------- the editable transcript


@app.get("/api/jobs/{job_id}/transcript")
def get_transcript(job_id: str, words: bool = False) -> dict:
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if not job.transcript_path.exists():
        raise HTTPException(404, "transcript not ready yet")
    return Transcript.load(job.transcript_path).to_dict(with_words=words)


@app.put("/api/jobs/{job_id}/transcript")
def put_transcript(job_id: str, payload: dict = Body(...)) -> dict:
    """Save the edited transcript. Word-level timings are preserved from the stored copy —
    the editor only ever touches text / bounds / skip / voice."""
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if not job.transcript_path.exists():
        raise HTTPException(404, "transcript not ready yet")

    stored = Transcript.load(job.transcript_path)
    # Keyed by span, not index: merging and deleting renumber everything, so an index-keyed
    # lookup would staple the wrong words onto the wrong segment.
    words_by_span = {(round(s.start, 3), round(s.end, 3)): s.words for s in stored.segments}

    edited = Transcript.from_dict({**stored.to_dict(), "segments": payload.get("segments", [])})
    for segment in edited.segments:
        if not segment.words:
            segment.words = words_by_span.get((round(segment.start, 3), round(segment.end, 3)), [])
    edited.duration = stored.duration          # canvas length is not user-editable
    edited.audio_path = stored.audio_path
    edited.normalize().save(job.transcript_path)
    (job.dir / "transcript.srt").write_text(edited.to_srt())
    return edited.to_dict(with_words=False)


@app.post("/api/jobs/{job_id}/smooth")
def smooth(job_id: str, payload: dict = Body(default={})) -> dict:
    """Hand the transcript to the smoothing agent (OpenAI Agents SDK).

    Runs in the background like any other stage; the editor polls and reloads the transcript
    when it lands, so the proposed edits arrive as reviewable rows rather than a fait accompli.
    """
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if not job.transcript_path.exists():
        raise HTTPException(400, "transcribe first")
    if not env("OPENAI_API_KEY"):
        raise HTTPException(400, "OPENAI_API_KEY is not set in .env")
    if payload.get("segments") is not None:  # keep the user's pending edits
        put_transcript(job_id, {"segments": payload["segments"]})
    try:
        STORE.smooth(job, str(payload.get("model") or ""))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return job.to_dict(with_log=False)


@app.post("/api/jobs/{job_id}/synthesize")
def synthesize(job_id: str, payload: dict = Body(default={})) -> dict:
    """Run stages 3–5 against the transcript on disk."""
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if not job.transcript_path.exists():
        raise HTTPException(400, "transcribe first")
    if payload.get("segments") is not None:  # save-then-run in one call
        put_transcript(job_id, {"segments": payload["segments"]})
    try:
        STORE.resynthesize(job, payload.get("options"))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return job.to_dict(with_log=False)


# ------------------------------------------------------------------------- artifacts

_MEDIA_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".wav": "audio/wav",
                ".json": "application/json", ".srt": "text/plain", ".mkv": "video/x-matroska",
                ".webm": "video/webm", ".m4v": "video/mp4"}


@app.get("/api/media")
def media_file(path: str = Query(...)):
    """Stream a local media file for preview (Cut tab). Confined to BROWSE_ROOT like everything
    else that takes a path from the browser."""
    target = _safe(path)
    if not target.is_file():
        raise HTTPException(404, f"no such file: {target}")
    return FileResponse(
        target, media_type=_MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    )


@app.get("/api/jobs/{job_id}/file/{kind}")
def get_file(job_id: str, kind: str, download: bool = False):
    job = STORE.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    paths = {
        "source": Path(job.input_path),
        "output": Path(job.output_path),
        "track": job.track_path,
        "transcript": job.transcript_path,
        "srt": job.dir / "transcript.srt",
        "report": job.report_path,
        "source_audio": job.dir / "source_16k.wav",
    }
    path = paths.get(kind)
    if path is None:
        raise HTTPException(404, f"unknown artifact: {kind}")
    if not path.exists():
        raise HTTPException(404, f"{kind} not available yet")
    return FileResponse(
        path,
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        filename=path.name if download else None,
    )


# -------------------------------------------------------------------------- frontend

app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
