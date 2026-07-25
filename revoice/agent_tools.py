"""TOOLS — the OpenAI Agents SDK `@function_tool`s the smoothing agent acts through.

Each tool is the only way the agent can touch the transcript. Nothing mutates while the agent
is running: the tools *record* edits against the original segment indices, and agent.py applies
them in one deterministic pass afterwards. That keeps indices stable across a whole run — an
agent looking at segment 41 in one window would otherwise find it renumbered by a merge it
proposed in the previous one.

Needs `pip install openai-agents`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from agents import RunContextWrapper, function_tool
except ImportError as exc:  # pragma: no cover
    raise SystemExit("the smoothing agent needs the OpenAI Agents SDK:  pip install openai-agents") from exc

from .timeline import Transcript


@dataclass
class SmoothContext:
    """Per-run context injected into every tool. The agent sees segments through get_segments()
    and proposes changes through the other three; it never holds the Transcript itself."""

    transcript: Transcript
    edits: list[dict] = field(default_factory=list)

    def segment(self, index: int):
        return next((s for s in self.transcript.segments if s.index == index), None)

    def record(self, edit: dict) -> dict:
        self.edits.append(edit)
        return {"recorded": True, **edit}


def _view(transcript: Transcript, seg) -> dict:
    """What the agent sees for one segment: the text plus the timing that constrains it."""
    return {
        "index": seg.index,
        "text": seg.text,
        "slot_seconds": round(seg.duration, 2),
        "pause_after": round(transcript.pause_after(seg.index), 2),
    }


@function_tool
def get_segments(ctx: RunContextWrapper[SmoothContext], start: int, count: int = 12) -> dict:
    """Read a window of transcript segments, in order, starting at segment `start`.

    Each segment carries `slot_seconds` (how long the original speaker took to say it) and
    `pause_after` (the silence before the next one). A `pause_after` at or near 0.00 means the
    speaker did NOT pause — those two segments are one continuous sentence that the transcriber
    split, and they are the prime candidates for merge_segments.
    """
    transcript = ctx.context.transcript
    window = [s for s in transcript.segments if start <= s.index < start + max(1, count)]
    return {
        "segments": [_view(transcript, s) for s in window],
        "total_segments": len(transcript.segments),
    }


@function_tool
def merge_segments(ctx: RunContextWrapper[SmoothContext], first: int, last: int, reason: str) -> dict:
    """Fuse segments `first`..`last` into one, spanning first.start to last.end.

    Use this when consecutive segments are one sentence split across a transcriber boundary
    (`pause_after` at or near 0). Merging changes no words at all — it only stops the voice from
    landing a sentence-final fall in the middle of a sentence. Prefer it over rewriting.
    """
    context = ctx.context
    if last <= first:
        return {"recorded": False, "error": "last must be greater than first"}
    missing = [i for i in range(first, last + 1) if context.segment(i) is None]
    if missing:
        return {"recorded": False, "error": f"no such segments: {missing}"}
    return context.record({"op": "merge", "first": first, "last": last, "reason": reason})


@function_tool
def rewrite_segment(ctx: RunContextWrapper[SmoothContext], index: int, text: str, reason: str) -> dict:
    """Replace the text of segment `index` with `text`.

    Only for transcription artifacts that a voice would read out loud as a mistake: a stuttered
    repeat ("all this this data"), a false start, a missing or wrong sentence break, wrong
    capitalisation of a name, or a word the transcriber clearly misheard where the surrounding
    text makes the intended word unambiguous.

    Never rephrase for style, never add or drop information, never change numbers, names,
    dosages or units, and keep the new text about as long as the old — it has to be spoken
    inside the same `slot_seconds`.
    """
    context = ctx.context
    seg = context.segment(index)
    if seg is None:
        return {"recorded": False, "error": f"no segment {index}"}
    if not text.strip():
        return {"recorded": False, "error": "text is empty — use delete_segment instead"}
    if text.strip() == seg.text.strip():
        return {"recorded": False, "error": "identical to the current text — nothing to do"}
    return context.record(
        {"op": "rewrite", "index": index, "text": text.strip(), "before": seg.text, "reason": reason}
    )


@function_tool
def delete_segment(ctx: RunContextWrapper[SmoothContext], index: int, reason: str) -> dict:
    """Drop segment `index` entirely; its slot stays silent.

    Only for segments the transcriber emitted twice — where the same words already appear in a
    neighbouring segment. Never delete a segment just because it is short, filler, or seems
    unimportant: that would remove something the speaker actually said.
    """
    context = ctx.context
    if context.segment(index) is None:
        return {"recorded": False, "error": f"no segment {index}"}
    return context.record({"op": "delete", "index": index, "reason": reason})


ALL_TOOLS = [get_segments, merge_segments, rewrite_segment, delete_segment]
