"""Bounded compression tuning; desktop defaults stay unchanged."""
import os


def encoder_threads(default: int) -> str:
    try:
        value = int(os.environ.get('DATAMARK_FFMPEG_ENCODER_THREADS', default))
    except (TypeError, ValueError):
        value = default
    return str(value if 1 <= value <= 8 else default)


def preparation_parallelism() -> int:
    try:
        value = int(os.environ.get('DATAMARK_PREPARATION_PARALLELISM', '2'))
    except (TypeError, ValueError):
        return 2
    return value if 1 <= value <= 4 else 2
