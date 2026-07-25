"""In-process job runner for the web backend.

Jobs are plain threads writing into a Job record; the HTTP layer only ever reads that record.
Each job owns a directory under jobs/ holding the extracted audio, the editable transcript,
the TTS cache, the assembled track and the final video — so a job survives a server restart
and can be resumed from its transcript without re-hitting Deepgram.
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import media, pipeline
from .config import JOBS_DIR, Options
from .timeline import Transcript

STAGES = ["extract", "transcribe", "synthesize", "mux"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    input_path: str
    output_path: str
    options: dict
    status: str = "queued"          # queued | running | needs_review | completed | failed
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    error: str = ""
    log: list[dict] = field(default_factory=list)
    media_info: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    smooth_summary: dict = field(default_factory=dict)
    smooth_edits: list = field(default_factory=list)
    voice_note: str = ""            # e.g. "cloned voice <id>"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def dir(self) -> Path:
        return JOBS_DIR / self.id

    @property
    def transcript_path(self) -> Path:
        return self.dir / "transcript.json"

    @property
    def track_path(self) -> Path:
        return self.dir / "revoiced.wav"

    @property
    def report_path(self) -> Path:
        return self.dir / "report.json"

    def artifacts(self) -> dict:
        return {
            "transcript": self.transcript_path.exists(),
            "srt": (self.dir / "transcript.srt").exists(),
            "track": self.track_path.exists(),
            "output": Path(self.output_path).exists() if self.output_path else False,
            "report": self.report_path.exists(),
        }

    def to_dict(self, *, with_log: bool = True) -> dict:
        data = asdict(self)
        data["artifacts"] = self.artifacts()
        data["stages"] = STAGES
        if not with_log:
            data.pop("log", None)
        return data


class JobStore:
    """Thread-safe registry. One lock guards the dict and every job mutation."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    # ------------------------------------------------------------------ persistence

    def _load_existing(self) -> None:
        for path in sorted(JOBS_DIR.glob("*/job.json")):
            try:
                data = json.loads(path.read_text())
                job = Job(
                    **{k: v for k, v in data.items() if k in Job.__dataclass_fields__}
                )
                if job.status in ("queued", "running"):  # interrupted by a restart
                    job.status = "failed"
                    job.error = job.error or "server restarted while this job was running"
                self._jobs[job.id] = job
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue

    def _persist(self, job: Job) -> None:
        job.dir.mkdir(parents=True, exist_ok=True)
        (job.dir / "job.json").write_text(json.dumps(job.to_dict(), indent=2))

    # ----------------------------------------------------------------------- access

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict(with_log=False) for j in jobs]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        import shutil

        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    # ---------------------------------------------------------------------- mutation

    def _update(self, job: Job, *, persist: bool = False, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = _now()
        if persist:
            self._persist(job)

    def _progress_cb(self, job: Job):
        def progress(stage: str, fraction: float, message: str) -> None:
            new_stage = stage != job.stage
            with self._lock:
                job.stage, job.progress, job.message = stage, float(fraction), message
                job.updated_at = _now()
                job.log.append({"t": _now(), "stage": stage, "message": message})
                del job.log[:-400]  # keep the log bounded on long videos
            if new_stage:
                self._persist(job)

        return progress

    # ------------------------------------------------------------------------ create

    def create(self, input_path: str, options: dict, output_path: str = "") -> Job:
        src = Path(input_path).expanduser()
        if not src.exists() or not src.is_file():
            raise FileNotFoundError(f"no such file: {src}")

        job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        job = Job(
            id=job_id,
            input_path=str(src),
            output_path=str(output_path or src.with_name(f"{src.stem}.revoiced{src.suffix}")),
            options=Options.from_dict(options).resolved().to_dict(),
        )
        job.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
        self._persist(job)
        return job

    # ------------------------------------------------------------------------- runs

    def start(self, job: Job, *, full: bool, clone: bool = False) -> None:
        """Stage 1+2, then optionally straight through 3–5."""
        self._spawn(job, self._run_transcribe, full=full, clone=clone)

    def smooth(self, job: Job, model: str = "") -> None:
        """Run the OpenAI-Agents smoothing pass over the transcript on disk."""
        self._spawn(job, self._run_smooth, model=model)

    def _run_smooth(self, job: Job, model: str = "") -> None:
        # Imported here so the SDK stays an optional dependency of the pipeline.
        try:
            from . import agent
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from None

        progress = self._progress_cb(job)
        transcript = Transcript.load(job.transcript_path)
        before = len(transcript.segments)

        result = agent.smooth(transcript, model=model, progress=progress)
        result.transcript.save(job.transcript_path)
        (job.dir / "transcript.srt").write_text(result.transcript.to_srt())
        (job.dir / "smooth_edits.json").write_text(
            json.dumps({"summary": result.summary(), "edits": result.edits}, indent=2)
        )

        summary = result.summary()
        self._update(
            job,
            status="needs_review",
            stage="smooth",
            progress=1.0,
            smooth_summary=summary,
            smooth_edits=result.edits,
            message=(
                f"smoothed: {before} → {summary['segments_after']} segments "
                f"({summary['merges']} merged, {summary['rewrites']} rewritten, "
                f"{summary['deletes']} deleted)"
            ),
            persist=True,
        )

    def resynthesize(self, job: Job, options: dict | None = None) -> None:
        """Stages 3–5 against the transcript currently on disk (possibly edited)."""
        if options:
            merged = {**job.options, **options}
            self._update(job, options=Options.from_dict(merged).resolved().to_dict())
        self._spawn(job, self._run_synthesize)

    def _spawn(self, job: Job, target, **kwargs) -> None:
        if job.status == "running":
            raise RuntimeError("job is already running")
        self._update(job, status="running", error="", stage="", progress=0.0, message="starting", persist=True)
        threading.Thread(target=self._guard, args=(job, target), kwargs=kwargs, daemon=True).start()

    def _guard(self, job: Job, target, **kwargs) -> None:
        try:
            target(job, **kwargs)
        except pipeline.FatalApiError as exc:
            # already a clean, human-facing message (bad key / no credits)
            self._update(job, status="failed", error=str(exc), message=str(exc), persist=True)
        except Exception as exc:  # surface the failure instead of leaving the job spinning
            detail = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            self._update(job, status="failed", error=detail, message=detail, persist=True)

    # -------------------------------------------------------------------- the stages

    def _run_transcribe(self, job: Job, *, full: bool, clone: bool = False) -> None:
        opts = Options.from_dict(job.options).resolved()
        progress = self._progress_cb(job)

        info = media.probe(job.input_path)
        self._update(job, media_info=info.to_dict())

        transcript = pipeline.transcribe_stage(job.input_path, job.dir, opts, progress)

        if clone:
            voice = pipeline.clone_voice_from_source(job.input_path, transcript, job.dir, opts, progress)
            opts.voice_id = voice["id"]
            self._update(
                job,
                options=opts.to_dict(),
                voice_note=f"cloned from source → {voice['id']}",
            )

        if not full:
            self._update(
                job, status="needs_review", stage="transcribe", progress=1.0,
                message=f"{len(transcript.segments)} segments ready to review",
                persist=True,
            )
            return
        self._synthesize_and_mux(job, opts, progress, transcript)

    def _run_synthesize(self, job: Job) -> None:
        opts = Options.from_dict(job.options).resolved()
        progress = self._progress_cb(job)
        transcript = Transcript.load(job.transcript_path)
        self._synthesize_and_mux(job, opts, progress, transcript)

    def _synthesize_and_mux(self, job: Job, opts: Options, progress, transcript: Transcript) -> None:
        report = pipeline.synthesize_stage(transcript, job.dir, opts, progress)
        pipeline.mux_stage(job.input_path, report.track_path, job.output_path, opts, progress)

        summary = report.summary()
        failed = summary.get("failed", 0)
        self._update(
            job,
            status="completed",
            stage="mux",
            progress=1.0,
            summary=summary,
            message=(
                f"done — {summary['duration']:.2f}s output, "
                f"{summary['length_delta']*1000:+.0f} ms vs source"
                + (f", {failed} segment(s) failed" if failed else "")
            ),
            persist=True,
        )


STORE = JobStore()
