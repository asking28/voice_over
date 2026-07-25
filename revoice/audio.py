"""Sample-exact PCM assembly.

Everything here works on 16-bit mono PCM held in a bytearray, which makes placement
arithmetic exact: frame N lives at byte 2N. That is the whole reason the output track can
be guaranteed to be the same length as the input, to the sample — no filter-graph rounding,
no accumulated drift across hundreds of segments.
"""

from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

SAMPLE_WIDTH = 2  # pcm_s16le


def read_wav(path: str | Path) -> tuple[bytes, int]:
    """Read a mono 16-bit WAV → (pcm bytes, sample_rate). Written by media.normalize_wav,
    so the format assumptions hold."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != SAMPLE_WIDTH or wav.getnchannels() != 1:
            raise ValueError(
                f"{path}: expected mono 16-bit PCM, got {wav.getnchannels()}ch/"
                f"{wav.getsampwidth() * 8}-bit — run it through media.normalize_wav first"
            )
        return wav.readframes(wav.getnframes()), wav.getframerate()


def write_wav(path: str | Path, pcm: bytes, sample_rate: int) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return Path(path)


def n_frames(pcm: bytes) -> int:
    return len(pcm) // SAMPLE_WIDTH


def duration_of(pcm: bytes, sample_rate: int) -> float:
    return n_frames(pcm) / sample_rate


def _as_array(pcm: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":  # file data is little-endian
        samples.byteswap()
    return samples


def _to_bytes(samples: array.array) -> bytes:
    if sys.byteorder != "little":
        samples = array.array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def apply_fades(pcm: bytes, sample_rate: int, fade_in_ms: float = 8, fade_out_ms: float = 8) -> bytes:
    """Linear fade in/out so segments dropped onto silence don't click at the seams.

    In and out are separate on purpose: a clip that butts straight against its neighbour
    wants only a click-guard (~2 ms), because a full fade there is audible as a dip in the
    middle of what the listener hears as one continuous sentence.
    """
    if not pcm or (fade_in_ms <= 0 and fade_out_ms <= 0):
        return pcm
    samples = _as_array(pcm)
    half = len(samples) // 2
    fade_in = min(int(sample_rate * fade_in_ms / 1000), half)
    fade_out = min(int(sample_rate * fade_out_ms / 1000), half)
    for i in range(fade_in):
        samples[i] = int(samples[i] * (i / fade_in))
    for i in range(fade_out):
        samples[-1 - i] = int(samples[-1 - i] * (i / fade_out))
    return _to_bytes(samples)


def mix(base: bytes, overlay: bytes, gain: float = 1.0) -> bytes:
    """Sum two buffers with clipping. `overlay` is looped/truncated to len(base)."""
    if not overlay or gain <= 0:
        return base
    a, b = _as_array(base), _as_array(overlay)
    for i in range(len(a)):
        value = a[i] + int(b[i] * gain)
        a[i] = 32767 if value > 32767 else (-32768 if value < -32768 else value)
    return _to_bytes(a)


def reversed_pcm(pcm: bytes) -> bytes:
    samples = _as_array(pcm)
    samples.reverse()
    return _to_bytes(samples)


def quietest_window(pcm: bytes, sample_rate: int, seconds: float = 0.4) -> tuple[bytes, float]:
    """Find the quietest stretch of `pcm` — the recording's own room tone.

    Returns (window, its RMS). Scans block energies rather than per-sample windows so this
    stays linear over a long file.
    """
    block = max(1, sample_rate // 100)                 # 10 ms blocks
    want = max(1, int(seconds * sample_rate) // block)  # window length, in blocks
    samples = _as_array(pcm)
    if len(samples) < want * block:
        return b"", 0.0

    energies = []
    for start in range(0, len(samples) - block + 1, block):
        total = 0
        for i in range(start, start + block):
            total += samples[i] * samples[i]
        energies.append(total / block)

    if len(energies) < want:
        return b"", 0.0
    running = sum(energies[:want])
    best, best_at = running, 0
    for i in range(want, len(energies)):
        running += energies[i] - energies[i - want]
        if running < best:
            best, best_at = running, i - want + 1

    start = best_at * block
    return slice_frames(pcm, start, start + want * block), (best / want) ** 0.5


def tile(pcm: bytes, total_frames: int) -> bytes:
    """Repeat `pcm` to `total_frames`, alternating forward/reversed copies so the loop points
    stay continuous instead of clicking every time the tile restarts."""
    if not pcm:
        return b""
    want = total_frames * SAMPLE_WIDTH
    chunks, size, flip = [], 0, False
    back = reversed_pcm(pcm)
    while size < want:
        piece = back if flip else pcm
        chunks.append(piece)
        size += len(piece)
        flip = not flip
    return b"".join(chunks)[:want]


def peak(pcm: bytes) -> int:
    """Largest absolute sample value (0..32768). Used for loudness matching."""
    if not pcm:
        return 0
    samples = _as_array(pcm)
    return max(abs(min(samples)), abs(max(samples)))


def rms(pcm: bytes) -> float:
    """Root-mean-square level (0..32768). Used to match the new speech to the original's
    loudness so the re-voiced track sits at the same level as everything around it."""
    if not pcm:
        return 0.0
    samples = _as_array(pcm)
    total = 0
    for value in samples:
        total += value * value
    return (total / len(samples)) ** 0.5


def slice_frames(pcm: bytes, start_frame: int, end_frame: int) -> bytes:
    start = max(0, start_frame) * SAMPLE_WIDTH
    end = max(0, end_frame) * SAMPLE_WIDTH
    return pcm[start:end]


def fit_frames(pcm: bytes, target_frames: int) -> bytes:
    """Hard trim or zero-pad to exactly `target_frames` frames."""
    want = max(0, target_frames) * SAMPLE_WIDTH
    if len(pcm) > want:
        return pcm[:want]
    return pcm + bytes(want - len(pcm))


def scale(pcm: bytes, gain: float) -> bytes:
    """Multiply by `gain`, clipping at full scale."""
    if abs(gain - 1.0) < 1e-3 or not pcm:
        return pcm
    samples = _as_array(pcm)
    for i, value in enumerate(samples):
        scaled = int(value * gain)
        samples[i] = 32767 if scaled > 32767 else (-32768 if scaled < -32768 else scaled)
    return _to_bytes(samples)


class Canvas:
    """A fixed-length silent track that segments get stamped onto at exact frame offsets.

    The length never changes, so the assembled track is bit-for-bit the same duration as the
    audio extracted from the source video.
    """

    def __init__(self, total_frames: int, sample_rate: int):
        self.sample_rate = sample_rate
        self.total_frames = max(0, int(total_frames))
        self.buffer = bytearray(self.total_frames * SAMPLE_WIDTH)

    @classmethod
    def for_duration(cls, seconds: float, sample_rate: int) -> "Canvas":
        return cls(int(round(seconds * sample_rate)), sample_rate)

    def place(self, at_frame: int, pcm: bytes) -> tuple[int, int]:
        """Stamp `pcm` at `at_frame`, truncating anything past the end of the canvas.

        Returns (start_frame, end_frame) of what was actually written.
        """
        start = max(0, int(at_frame))
        if start >= self.total_frames or not pcm:
            return start, start
        room = (self.total_frames - start) * SAMPLE_WIDTH
        chunk = pcm[:room]
        offset = start * SAMPLE_WIDTH
        self.buffer[offset : offset + len(chunk)] = chunk
        return start, start + len(chunk) // SAMPLE_WIDTH

    def to_wav(self, path: str | Path) -> Path:
        return write_wav(path, bytes(self.buffer), self.sample_rate)

    @property
    def duration(self) -> float:
        return self.total_frames / self.sample_rate
