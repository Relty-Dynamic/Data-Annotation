"""Bounded per-encoder tuning; desktop defaults stay unchanged."""
import os


def encoder_threads(default: int) -> str:
    try:
        value = int(os.environ.get('DATAMARK_FFMPEG_ENCODER_THREADS', default))
    except (TypeError, ValueError):
        value = default
    return str(value if 1 <= value <= 8 else default)
