"""The intermediate transcript — the editable contract between stage 2 and stage 3.

A Transcript is a list of Segments, each carrying the *exact* start/end it occupied in the
original audio. The silence between segments is never stored as data; it is implied by the
gaps, and reproduced exactly because every segment is stamped back at its own start time.

The JSON form is designed to be hand-edited (or edited in the web UI): fix a misheard word,
rewrite a sentence, mute a segment, or nudge a boundary, then re-run stage 3.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Word:
    text: str
    start: float
    end: float
    confidence: float = 0.0

    @property
    def gap_before(self) -> float:
        return 0.0


@dataclass
class Segment:
    index: int
    start: float
    end: float
    text: str
    speaker: str = ""
    confidence: float = 0.0
    skip: bool = False          # mute this slot instead of speaking it
    voice_id: str = ""          # per-segment voice override (blank = job default)
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self, *, pause_after: float = 0.0, with_words: bool = True) -> dict:
        data = {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "pause_after": round(pause_after, 3),
            "text": self.text,
            "speaker": self.speaker,
            "confidence": round(self.confidence, 3),
            "skip": self.skip,
            "voice_id": self.voice_id,
        }
        if with_words:
            data["words"] = [asdict(w) for w in self.words]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        return cls(
            index=int(data.get("index", 0)),
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            text=str(data.get("text", "")).strip(),
            speaker=str(data.get("speaker", "") or ""),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            skip=bool(data.get("skip", False)),
            voice_id=str(data.get("voice_id", "") or ""),
            words=[
                Word(
                    text=str(w.get("text", w.get("word", ""))),
                    start=float(w.get("start", 0.0)),
                    end=float(w.get("end", 0.0)),
                    confidence=float(w.get("confidence", 0.0) or 0.0),
                )
                for w in data.get("words", [])
            ],
        )


@dataclass
class Transcript:
    source: str                 # the original video/audio path
    audio_path: str             # extracted WAV used for STT
    duration: float             # exact duration of the extracted audio, in seconds
    language: str = "en"
    stt_model: str = ""
    created_at: str = ""
    segments: list[Segment] = field(default_factory=list)

    # ---------------------------------------------------------------- derived views

    def pause_after(self, i: int) -> float:
        """Silence between segment i and the next one (or the tail silence)."""
        if i < len(self.segments) - 1:
            return max(0.0, self.segments[i + 1].start - self.segments[i].end)
        return max(0.0, self.duration - self.segments[i].end)

    @property
    def lead_silence(self) -> float:
        return self.segments[0].start if self.segments else self.duration

    @property
    def speech_duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text and not s.skip)

    # ---------------------------------------------------------------- serialization

    def to_dict(self, *, with_words: bool = True) -> dict:
        return {
            "source": self.source,
            "audio_path": self.audio_path,
            "duration": round(self.duration, 3),
            "language": self.language,
            "stt_model": self.stt_model,
            "created_at": self.created_at,
            "stats": {
                "segments": len(self.segments),
                "speech_seconds": round(self.speech_duration, 2),
                "silence_seconds": round(max(0.0, self.duration - self.speech_duration), 2),
                "lead_silence": round(self.lead_silence, 3),
            },
            "segments": [
                s.to_dict(pause_after=self.pause_after(i), with_words=with_words)
                for i, s in enumerate(self.segments)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        transcript = cls(
            source=data.get("source", ""),
            audio_path=data.get("audio_path", ""),
            duration=float(data.get("duration", 0.0)),
            language=data.get("language", "en"),
            stt_model=data.get("stt_model", ""),
            created_at=data.get("created_at", ""),
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
        )
        transcript.normalize()
        return transcript

    def save(self, path: str | Path, *, with_words: bool = True) -> Path:
        Path(path).write_text(json.dumps(self.to_dict(with_words=with_words), indent=2))
        return Path(path)

    @classmethod
    def load(cls, path: str | Path) -> "Transcript":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def to_srt(self) -> str:
        def stamp(t: float) -> str:
            ms = int(round(t * 1000))
            h, ms = divmod(ms, 3_600_000)
            m, ms = divmod(ms, 60_000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        blocks = [
            f"{i}\n{stamp(seg.start)} --> {stamp(seg.end)}\n{seg.text}\n"
            for i, seg in enumerate(self.segments, 1)
        ]
        return "\n".join(blocks)

    # ---------------------------------------------------------------- housekeeping

    def merge_range(self, first: int, last: int) -> "Transcript":
        """Fuse segments [first..last] into one slot spanning first.start → last.end.

        Deepgram splits on pauses, which means a sentence delivered without a breath comes
        back as several utterances. Speaking those separately gives each one its own
        sentence-final fall and a seam where they butt together; merging them hands the whole
        thing to the model as one line, which is what it actually was.
        """
        if not (0 <= first < last < len(self.segments)):
            return self
        group = self.segments[first : last + 1]
        head = group[0]
        head.end = group[-1].end
        head.text = " ".join(s.text for s in group if s.text).strip()
        head.words = [w for s in group for w in s.words]
        head.skip = all(s.skip for s in group)
        head.confidence = min((s.confidence for s in group if s.confidence), default=head.confidence)
        del self.segments[first + 1 : last + 1]
        return self.normalize()

    def merge_close(self, max_gap: float) -> int:
        """Merge every run of adjacent segments separated by <= max_gap seconds.

        Returns the number of segments removed. Runs back-to-front so indices stay valid.
        """
        if max_gap <= 0 or len(self.segments) < 2:
            return 0
        before = len(self.segments)
        i = len(self.segments) - 1
        while i > 0:
            start = i
            while start > 0 and (self.segments[start].start - self.segments[start - 1].end) <= max_gap:
                start -= 1
            if start < i:
                self.merge_range(start, i)
            i = start - 1
        return before - len(self.segments)

    def delete(self, index: int) -> "Transcript":
        """Drop a segment entirely. Its slot simply stays silent."""
        match = next((i for i, s in enumerate(self.segments) if s.index == index), None)
        if match is not None:
            del self.segments[match]
            self.normalize()
        return self

    def normalize(self) -> "Transcript":
        """Re-sort, clamp to the media bounds, and re-index. Run after user edits — the web
        editor lets people change start/end, and stage 3 assumes an ordered, in-range list."""
        for seg in self.segments:
            seg.start = max(0.0, seg.start)
            seg.end = max(seg.start, seg.end)
            if self.duration:
                seg.start = min(seg.start, self.duration)
                seg.end = min(seg.end, self.duration)
            seg.text = (seg.text or "").strip()
        self.segments.sort(key=lambda s: (s.start, s.end))
        for i, seg in enumerate(self.segments):
            seg.index = i
        return self


# --------------------------------------------------------------- Deepgram → Transcript


def _word_text(word: dict) -> str:
    return str(word.get("punctuated_word") or word.get("word") or "").strip()


def _to_words(raw: list[dict]) -> list[Word]:
    return [
        Word(
            text=_word_text(w),
            start=float(w.get("start", 0.0)),
            end=float(w.get("end", 0.0)),
            confidence=float(w.get("confidence", 0.0) or 0.0),
        )
        for w in raw
        if _word_text(w)
    ]


def _group_by_gap(words: list[Word], gap: float) -> list[list[Word]]:
    """Fallback segmentation when Deepgram returns no utterances: start a new segment
    whenever the silence between two words exceeds `gap`."""
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if current and word.start - current[-1].end > gap:
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def _split_long(words: list[Word], max_chars: int) -> list[list[Word]]:
    """Break an over-long utterance at a sentence boundary, falling back to the widest
    internal pause — so each synthesized chunk still lines up with real silence."""
    if not words or max_chars <= 0:
        return [words] if words else []

    chunks: list[list[Word]] = []
    pending = list(words)
    while pending:
        chars, cut = 0, len(pending)
        for i, word in enumerate(pending):
            chars += len(word.text) + 1
            if chars > max_chars:
                cut = max(1, i)
                break
        if cut >= len(pending):
            chunks.append(pending)
            break

        floor = max(1, int(cut * 0.4))
        split_at = next(
            (i + 1 for i in range(cut - 1, floor - 1, -1) if pending[i].text[-1:] in ".?!"),
            None,
        )
        if split_at is None:
            gaps = [
                (pending[i + 1].start - pending[i].end, i + 1)
                for i in range(floor, cut)
                if i + 1 < len(pending)
            ]
            split_at = max(gaps)[1] if gaps else cut
        chunks.append(pending[:split_at])
        pending = pending[split_at:]
    return chunks


def from_deepgram(
    response: dict,
    *,
    source: str,
    audio_path: str,
    duration: float,
    language: str,
    stt_model: str,
    utt_split: float = 0.6,
    max_segment_chars: int = 320,
) -> Transcript:
    """Turn a Deepgram pre-recorded response into a timed, editable transcript."""
    results = response.get("results", {})
    utterances = results.get("utterances") or []

    groups: list[tuple[list[Word], str, float]] = []  # (words, speaker, confidence)
    if utterances:
        for utt in utterances:
            words = _to_words(utt.get("words", []))
            if not words:
                continue
            speaker = "" if utt.get("speaker") is None else f"speaker_{utt['speaker']}"
            confidence = float(utt.get("confidence", 0.0) or 0.0)
            for chunk in _split_long(words, max_segment_chars):
                groups.append((chunk, speaker, confidence))
    else:  # no utterances in the response — rebuild them from word timings
        channels = results.get("channels") or [{}]
        alternative = (channels[0].get("alternatives") or [{}])[0]
        words = _to_words(alternative.get("words", []))
        confidence = float(alternative.get("confidence", 0.0) or 0.0)
        for group in _group_by_gap(words, utt_split):
            for chunk in _split_long(group, max_segment_chars):
                groups.append((chunk, "", confidence))

    segments = [
        Segment(
            index=i,
            start=words[0].start,
            end=words[-1].end,
            text=" ".join(w.text for w in words),
            speaker=speaker,
            confidence=confidence,
            words=words,
        )
        for i, (words, speaker, confidence) in enumerate(groups)
    ]

    transcript = Transcript(
        source=source,
        audio_path=audio_path,
        duration=duration,
        language=language,
        stt_model=stt_model,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        segments=segments,
    )
    return transcript.normalize()
