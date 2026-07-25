# revoice

Re-voice a **local** video: pull the audio out, transcribe it with exact word timings, speak
it again with a synthetic voice, fit every line back into the exact slot it came from, and
mux it back into the original video.

The point is **timing fidelity**. The output is the same length as the input, every pause sits
where it always sat, and the video stream is copied untouched — so lip-adjacent cues, slide
changes and screen-recording actions still line up.

- **STT** — Deepgram `nova-3` (word-level timings, utterances, filler words)
- **TTS** — Deepgram `aura-2` *(default)* or Cartesia `sonic-3.5` (which can also clone the
  source speaker). Aura uses the same key as the STT stage, so one key covers the pipeline.
- **Media** — ffmpeg (demux, pitch-preserving time-stretch, mux)
- **Input** — a local file path. Nothing is uploaded to the web app; the server reads the path.

---

## Setup

```bash
git clone https://github.com/asking28/voice_over.git
cd voice_over

brew install ffmpeg          # macOS — or: sudo apt install ffmpeg
cp .env.example .env         # then put your keys in it
```

`.env` needs `DEEPGRAM_API_KEY` ([console.deepgram.com](https://console.deepgram.com)) — it
covers both transcription and the default Aura voices. `CARTESIA_API_KEY`
([play.cartesia.ai](https://play.cartesia.ai)) is only needed if you switch the TTS provider
to Cartesia or use `--clone`.

**Requirements:** Python 3.11+, `ffmpeg` and `ffprobe` on `PATH`. `requirements.txt` covers
the web app only (`fastapi`, `uvicorn`); the pipeline and CLI are stdlib-only, so
`python3 -m revoice.cli` works with nothing installed. `./run.sh` builds the venv for you, or:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

The pipeline itself is **stdlib-only** (urllib + wave + subprocess), so the CLI runs on a bare
`python3`. Only the web layer needs dependencies, installed automatically by `run.sh`.

---

## Web app

```bash
./run.sh
```

→ http://127.0.0.1:8010

1. **Local file** — type or browse to a path. *Inspect* shows duration, resolution and codecs.
2. **Voice & timing** — pick a provider and voice, hit **▶** to hear a sentence in it before
   committing, choose a fit mode, and open *advanced* for the rest. (*Clone* is Cartesia-only
   and greys out under Aura.)
3. **Run** — *Transcribe & review* stops after stage 2 so you can edit; *Run everything* goes
   straight through. A live rail shows which stage is running.
4. **Transcript** — the editable intermediate (see below). Edit, then *Synthesize & mux*.
5. **Result** — timing tiles, the original and re-voiced videos side by side, a per-segment
   drift table, and download links.

Jobs live in `jobs/<id>/` and survive a restart. The file browser is confined to `$HOME`
(override with `REVOICE_BROWSE_ROOT`).

---

## CLI

```bash
python3 -m revoice.cli run talk.mp4                      # everything → talk.revoiced.mp4
python3 -m revoice.cli run talk.mp4 -o dubbed.mp4 -v     # + per-segment timing table
python3 -m revoice.cli run talk.mp4 --clone              # clone the source speaker first

# or stage by stage, editing in between
python3 -m revoice.cli transcribe talk.mp4               # → work/talk/transcript.json
$EDITOR work/talk/transcript.json
python3 -m revoice.cli synthesize work/talk/transcript.json
python3 -m revoice.cli mux talk.mp4 work/talk/revoiced.wav -o dubbed.mp4

# and, once you have an edited transcript, the whole thing minus the STT call
python3 -m revoice.cli run talk.mp4 --transcript work/talk/transcript.json

python3 -m revoice.cli preview                           # hear the default voice
python3 -m revoice.cli preview --text "how does this sound?" --voice-id aura-2-zeus-en
python3 -m revoice.cli voices                            # Aura catalogue
python3 -m revoice.cli voices --provider cartesia        # your Cartesia library
python3 -m revoice.cli probe talk.mp4
```

`preview` doubles as a preflight check: a bad key or an exhausted plan surfaces there for the
cost of one sentence, instead of one failed call per segment.

Audio-only inputs work too — stage 5 just writes an audio file instead of a video.

---

## The editable transcript

Stage 2 writes `transcript.json`. It is the contract between transcription and synthesis, and
it is meant to be edited — in the web table, or in any text editor:

```json
{
  "duration": 35.0,
  "segments": [
    {
      "index": 0,
      "start": 0.0,
      "end": 1.36,
      "duration": 1.36,
      "pause_after": 0.24,
      "text": "In the Epic EMR system,",
      "skip": false,
      "voice_id": "",
      "words": [ { "text": "In", "start": 0.08, "end": 0.24 } ]
    }
  ]
}
```

| field | what it does |
|---|---|
| `text` | what gets spoken. Fix a misheard word, rewrite the line, or translate it. |
| `start` / `end` | the slot. `end - start` is the duration the synthesized line is fitted into, and the gaps between slots are the pauses. |
| `skip` | leave this slot silent. |
| `voice_id` | per-segment voice override — e.g. give each speaker their own voice after `--diarize`. |
| `words` | Deepgram's word timings, kept for reference; the editor preserves them. |

Only segments whose **text actually changed** are re-synthesized — everything else comes from
the on-disk TTS cache, so a re-run after a two-word fix takes well under a second and costs
nothing. `transcript.srt` is written alongside it.

---

## Making the seams smooth

Three things make a re-voiced track sound stitched together, and the pipeline addresses each:

**1. Sentences split across segments.** Deepgram starts a new utterance at every pause, so one
sentence spoken without a breath comes back as two or three. Synthesized separately, each gets
its own sentence-final fall and they butt together audibly. After transcription, neighbours
closer than `--merge-gap` (0.18 s) are **fused back into one segment**, so one spoken sentence
is one TTS call. On a 6-minute demo this collapsed 86 segments into 52 — which also halved the
time-stretching (37 → 19 stretched) and cut max drift from 40 ms to 0 ms.

**2. Digital silence in the pauses.** An assembled canvas is mathematically silent between
clips, so the room drops dead and snaps back — the most artificial part of a naive re-voice.
The pipeline lifts the **quietest half-second out of your own recording** and tiles it under
the whole track (alternating forward/reversed copies so the loop points don't click), at the
level it measured. Pauses now sit at the recording's own noise floor instead of zero. Turn it
off with `--room-tone 0`; it self-disables if the source has no quiet passage to sample.

**3. Fades in the wrong places.** A clip that butts straight against its neighbour used to get
a full fade-out plus fade-in — an audible dip inside what the listener hears as one sentence.
Fades are now applied at placement time, where the surrounding gaps are known: a full
`--fade-ms` against silence, a 2 ms click-guard against a neighbour.

## Cut & trim — the second tab

A separate tab for taking sections *out* of a video and closing the gap, so it continues from
the next kept moment. It shares nothing with the re-voicing flow except the file browser.

- **Audio presence.** The waveform envelope is computed from the extracted 16 kHz audio, and
  silence is found with ffmpeg's `silencedetect` — so the quiet stretches are shaded behind the
  waveform and you can see at a glance where there's actually anything to keep.
- **Marking.** Drag across the waveform to select, click to seek. *Mark long silences* marks
  every silence over 1 s in one go, leaving 0.15 s of padding either side so words aren't
  clipped. Overlapping marks merge. Undo is a stack.
- **Cutting.** `precise` (default) does one ffmpeg pass with `select`/`aselect` plus
  `setpts`/`asetpts` — the re-timing is what actually closes the gap, and cut points land
  exactly where you put them at the cost of re-encoding the video. `fast` stream-copies each
  kept range and concatenates, which skips the re-encode but snaps every cut to the nearest
  preceding keyframe.
- **Use this in Re-voice →** hands the trimmed file straight to the other tab.

The extracted audio is cached per (path, mtime, size) under `jobs/_analysis/`, so re-opening a
file is instant while an edited file re-analyses. The original is never modified — cuts are
written to a new file (`<name>.cut.mp4` by default).

```
revoice/waveform.py   envelope + silence detection  (analyze → peaks, speech, silence)
revoice/clip.py       keep_ranges / plan / cut      (precise and fast strategies)
web/cut.js            the tab: canvas, drag-select, cut list, export
```

## ✨ Auto-smooth — the agent pass

`--merge-gap` fuses split sentences by a pure timing rule, which catches most of them but is
blind to what the words say. The **Auto-smooth** button hands the transcript to an agent built
on the [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/) that reads it and
proposes three kinds of change:

- **merge** consecutive segments that are one sentence the transcriber split (its main job, and
  it changes no words at all);
- **rewrite** a segment to remove a stutter or false start — `"all this this data"` → `"all this
  data"` — or to fix punctuation and the capitalisation of a name;
- **delete** a segment the transcriber emitted twice.

It is told not to rephrase, not to add or drop information, not to touch numbers or names, and
to keep rewritten text the same length — each line still has to be spoken inside its own slot.

```
revoice/agent_tools.py   the @function_tools: get_segments, merge_segments,
                         rewrite_segment, delete_segment — plus the SmoothContext
                         injected into each (mirrors the tools.py/skill.py split)
revoice/agent.py         the Agent + Runner, the instructions, and apply_edits()
```

Nothing mutates while the agent runs: the tools *record* edits against the original segment
indices and `apply_edits()` applies them in one deterministic pass afterwards, so indices stay
stable across the whole run. The transcript is walked in windows of 14 segments.

The result lands in the editor as a change list plus rewritten rows — **review it before you
synthesize.** The edits are the model's proposals, not verified facts.

```bash
python3 -m revoice.cli smooth work/talk/transcript.json --dry-run   # show the edits
python3 -m revoice.cli smooth work/talk/transcript.json             # apply in place
```

Needs `OPENAI_API_KEY` in `.env`; model via `--model` or `REVOICE_AGENT_MODEL`
(default `gpt-4.1-mini`). On the 6-minute demo it made 25 merges and 3 rewrites across
7 windows, taking 86 segments to 52.

## Editing structure, not just text

Beyond rewriting a line or ticking **skip**, the transcript panel lets you change the segment
structure itself:

- **⇧ merge** folds a segment into the one above — one slot, one TTS call, no seam. Use it
  wherever the **pause** column reads `0.00s`.
- **✕ delete** drops a segment; its slot simply stays silent. Handy for the duplicate lines
  STT sometimes emits.
- **Auto-merge** does the whole pass at once, using the same rule as the pipeline.

All of it is just the JSON — merging is one segment with a wider `start`..`end`, deleting is a
missing entry. Edit the file by hand and you get the same result.

## How the timing is preserved

1. The canvas length comes from the **frame count of the extracted PCM**, not from container
   metadata, and never changes. Every clip is stamped onto it at a byte offset (`frame N` →
   `byte 2N`), so there is no filter-graph rounding and no drift accumulating across segments.
2. Each segment is synthesized independently and compared against its slot:
   - too long → ask for a genuinely faster take where the provider supports it (Cartesia
     `speed: fast`; Aura has no speed control and goes straight to stretching), then
     pitch-preserving `atempo` up to `--max-tempo` (default 1.6×);
   - too short → in `natural` mode it just finishes early and the slot's remaining time stays
     silent, which is what a person sounds like. `exact` mode stretches it to fill the slot.
3. Clips are placed at their original start time. A clip only pushes the next one later if it
   would physically overlap, and the report tells you when that happened.
4. The video stream is **copied**, never re-encoded.

Measured on a 6-minute 1080p screen recording — 86 segments, Deepgram Aura, 31 s wall clock:

```
source 357.564s → output 357.564s (delta -0.0000s)
drift: max 40 ms, mean 2.7 ms
86 spoken / 0 skipped / 0 failed · 37 time-stretched (max 1.60x)
```

(A 35 s clip through Cartesia: 0 ms length delta, 9 ms max drift.)

**One caveat, stated honestly:** AAC codes 1024 samples per frame, so an AAC track rounds up
to the next frame boundary — up to ~23 ms of trailing silence past the assembled length (the
video stream and every segment position are unaffected). Choose `--audio-codec alac` (or
`flac` / `pcm_s16le`) when the audio track has to match the source sample count exactly;
that path is verified sample-for-sample.

---

## Options

| flag / field | default | meaning |
|---|---|---|
| `--provider` | `deepgram` | `deepgram` (Aura, 41 voices, same key as STT) or `cartesia` (Sonic, your library + cloning + a speed hint) |
| `--voice-id` | `aura-2-thalia-en` | which voice speaks — an Aura model name, or a Cartesia voice UUID |
| `--clone` | off | clone the source speaker from the densest ~15 s of their speech |
| `--fit-mode` | `natural` | `natural` speeds up only when needed; `exact` fills every slot |
| `--max-tempo` | `1.6` | hard cap on speed-up before a segment is allowed to run long |
| `--min-tempo` | `0.75` | hard cap on slow-down (`exact` mode only) |
| `--utt-split` | `0.6` | a gap this long (s) starts a new segment |
| `--merge-gap` | `0.18` | ...then fuse neighbours closer than this back together (`0` = off) |
| `--room-tone` | `0.9` | blend the source's own noise floor under the track (`0` = off) |
| `--max-segment-chars` | `320` | longer utterances are split at sentence boundaries |
| `--workers` | `6` | parallel Cartesia requests |
| `--no-adaptive` | off | never re-request a faster take; time-stretch only |
| `--audio-codec` | `aac` | `alac`/`flac`/`pcm_s16le` for a sample-exact track |
| `--keep-original-track` | off | keep the source audio as a second track |
| `--diarize` | off | tag segments with a speaker id |
| `--no-filler-words` | off | drop "um"/"uh" (their time then becomes silence) |

---

## Layout

```
revoice/
├── run.sh                  # start the web app on :8010
├── revoice/
│   ├── config.py           # env + every tunable knob
│   ├── media.py            # ffmpeg/ffprobe: demux, probe, atempo, mux
│   ├── audio.py            # sample-exact PCM canvas, fades, loudness
│   ├── stt.py              # Deepgram transcription
│   ├── tts.py              # Deepgram Aura + Cartesia Sonic behind one interface
│   ├── timeline.py         # the editable transcript model + Deepgram → segments
│   ├── pipeline.py         # the five stages
│   ├── jobs.py             # background job runner for the web app
│   ├── server.py           # FastAPI backend
│   └── cli.py              # command line
└── web/                    # single-page frontend (vanilla JS)
```

## Notes

- Voice cloning is opt-in on purpose. Only clone a voice you have the right to clone.
- Each run costs one Deepgram transcription plus **one TTS call per segment** (a couple more
  when the Cartesia adaptive retry fires). The transcript panel shows the segment count and
  character total before you commit, and the cache makes edit-and-rerun effectively free.
- A run that hits a bad key or an exhausted plan **fails loudly and stops**, rather than
  muxing a mostly-silent video that looks like a success. The first such error aborts the
  remaining segments instead of burning a doomed call on each one.
- `atempo` is transparent to roughly ±30%; past that speech starts to sound processed, which
  is why `--max-tempo` exists and why the report flags every stretched segment.
