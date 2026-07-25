"""SKILL — the transcript-smoothing agent, on the OpenAI Agents SDK.

Composes the tools from agent_tools.py into a single agent that walks the transcript in windows
and proposes structural fixes: fuse sentences the transcriber split, clean up stutters and false
starts, drop duplicated segments. It is deliberately narrow — it edits how the text will *read
aloud*, not what it says.

Nothing is written to disk here. The agent returns a smoothed Transcript plus the list of edits
it made, and the caller (the web editor, or `revoice smooth`) decides whether to keep them.

Needs `pip install openai-agents` + OPENAI_API_KEY in .env.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .config import env, require_key
from .timeline import Transcript

try:
    from agents import Agent, Runner, set_tracing_disabled
except ImportError as exc:  # pragma: no cover
    raise SystemExit("the smoothing agent needs the OpenAI Agents SDK:  pip install openai-agents") from exc

from .agent_tools import ALL_TOOLS, SmoothContext

# The SDK uploads run traces to OpenAI by default; opt back in with OPENAI_AGENTS_DISABLE_TRACING=0.
if env("OPENAI_AGENTS_DISABLE_TRACING", "1") != "0":
    set_tracing_disabled(True)

DEFAULT_MODEL = env("REVOICE_AGENT_MODEL", "gpt-4.1-mini")
WINDOW = 14  # segments per turn — small enough that the model reads every line properly

INSTRUCTIONS = """\
You prepare a speech transcript to be re-spoken by a text-to-speech voice, over the original
video, in the original timing. Your job is to make it READ ALOUD cleanly. It is not to improve,
shorten, summarise or rewrite what the speaker said.

The transcript came from an automatic transcriber, so it has three recurring defects:

1. SPLIT SENTENCES — the transcriber starts a new segment at every pause, so one sentence
   spoken without a breath arrives as two or three segments. You can see this when
   `pause_after` is 0.00 or close to it. Each segment is spoken separately, so the voice puts a
   falling, sentence-final intonation in the middle of the sentence. Fix with merge_segments.
   This is your most useful tool and it changes no words at all — prefer it.

2. STUTTERS AND FALSE STARTS — "all this this data", "you can bring go inside". A human ear
   skips these; a synthetic voice reads them out. Fix with rewrite_segment.

3. DUPLICATED SEGMENTS — the transcriber occasionally emits the same words twice in a row as
   two segments. Remove the repeat with delete_segment.

Also fix, via rewrite_segment: missing sentence punctuation, wrong capitalisation of product or
company names, and words the transcriber plainly misheard where the surrounding sentence makes
the intended word unambiguous.

Hard limits — these matter more than any improvement you could make:
- Never add information, never remove information, never rephrase for style.
- Never change a number, name, dosage, unit or date.
- Keep rewritten text about the same length as the original. Each segment must be spoken inside
  its own `slot_seconds`; text that grows gets compressed and sounds rushed.
- If a segment reads fine, leave it alone. Doing nothing is a good outcome.

Work through the window you are given with get_segments, then make your calls. When you have
handled the window, reply with one short line saying what you changed and stop.
"""


@dataclass
class SmoothResult:
    transcript: Transcript
    edits: list[dict] = field(default_factory=list)
    model: str = ""
    windows: int = 0

    def summary(self) -> dict:
        counts = {"merge": 0, "rewrite": 0, "delete": 0}
        for edit in self.edits:
            counts[edit["op"]] = counts.get(edit["op"], 0) + 1
        return {
            "merges": counts["merge"],
            "rewrites": counts["rewrite"],
            "deletes": counts["delete"],
            "total": len(self.edits),
            "segments_after": len(self.transcript.segments),
            "model": self.model,
            "windows": self.windows,
        }


def build_agent(model: str = "") -> Agent:
    return Agent(
        name="Transcript Smoother",
        instructions=INSTRUCTIONS,
        tools=ALL_TOOLS,
        model=model or DEFAULT_MODEL,
    )


def _merge_ranges(edits: list[dict]) -> list[tuple[int, int]]:
    """Collapse the proposed merge ranges into non-overlapping ones."""
    ranges = sorted((e["first"], e["last"]) for e in edits if e["op"] == "merge")
    out: list[tuple[int, int]] = []
    for first, last in ranges:
        if out and first <= out[-1][1]:
            out[-1] = (out[-1][0], max(last, out[-1][1]))
        else:
            out.append((first, last))
    return out


def apply_edits(transcript: Transcript, edits: list[dict]) -> Transcript:
    """Apply recorded edits to a transcript, in one pass, against the ORIGINAL indices.

    Order matters: rewrite the text first, then drop deletions, then fuse the merge ranges — so a
    merged segment picks up the rewritten text and skips anything deleted inside its range.
    """
    by_index = {s.index: s for s in transcript.segments}

    for edit in edits:
        if edit["op"] == "rewrite" and edit["index"] in by_index:
            by_index[edit["index"]].text = edit["text"]

    deleted = {e["index"] for e in edits if e["op"] == "delete"}
    ranges = _merge_ranges(edits)
    ordered = sorted(transcript.segments, key=lambda s: s.index)

    out = []
    consumed: set[int] = set()
    for seg in ordered:
        if seg.index in consumed or seg.index in deleted:
            continue
        span = next((r for r in ranges if r[0] <= seg.index <= r[1]), None)
        if not span:
            out.append(seg)
            continue
        group = [s for s in ordered if span[0] <= s.index <= span[1] and s.index not in deleted]
        consumed.update(s.index for s in ordered if span[0] <= s.index <= span[1])
        if not group:
            continue
        head = group[0]
        head.end = max(s.end for s in group)
        head.text = " ".join(s.text for s in group if s.text).strip()
        head.words = [w for s in group for w in s.words]
        head.skip = all(s.skip for s in group)
        out.append(head)

    transcript.segments = out
    return transcript.normalize()


def smooth(
    transcript: Transcript,
    *,
    model: str = "",
    window: int = WINDOW,
    progress: Callable[[str, float, str], None] | None = None,
) -> SmoothResult:
    """Walk the transcript in windows, letting the agent propose edits, then apply them.

    The transcript passed in is not modified; the result carries a smoothed copy.
    """
    require_key("OPENAI_API_KEY")
    working = Transcript.from_dict(transcript.to_dict())  # deep copy via round-trip
    agent = build_agent(model)
    context = SmoothContext(transcript=working)

    total = len(working.segments)
    windows = max(1, -(-total // window))  # ceiling division
    for number, start in enumerate(range(0, total, window), start=1):
        if progress:
            progress(
                "smooth",
                (number - 1) / windows,
                f"reviewing segments {start}–{min(start + window, total) - 1} of {total}",
            )
        before = len(context.edits)
        Runner.run_sync(
            agent,
            f"Review segments {start} through {min(start + window, total) - 1}. "
            f"Call get_segments(start={start}, count={window}) first.",
            context=context,
            max_turns=window + 8,
        )
        if progress:
            found = len(context.edits) - before
            progress("smooth", number / windows, f"window {number}/{windows}: {found} edits")

    edits = list(context.edits)
    result = SmoothResult(
        transcript=apply_edits(working, edits),
        edits=edits,
        model=agent.model if isinstance(agent.model, str) else str(agent.model),
        windows=windows,
    )
    if progress:
        progress("smooth", 1.0, json.dumps(result.summary()))
    return result
