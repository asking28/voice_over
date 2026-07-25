"""revoice — re-voice a local video while preserving its exact timing.

Pipeline (see pipeline.py):
    1. extract audio from the video            (ffmpeg)
    2. transcribe with word-level timings       (Deepgram) → editable transcript.json
    3. synthesize each segment                  (Cartesia)
    4. fit each segment to its original slot    (ffmpeg atempo) and lay it on a
       sample-exact silent canvas so every pause is preserved
    5. mux the new track back into the video    (ffmpeg, video stream copied)
"""

__version__ = "0.1.0"
