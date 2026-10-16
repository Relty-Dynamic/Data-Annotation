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


def compact_command(ffmpeg, source, target, duration_ms, audio_start):
    """One normal-playback encoding contract for NAS and desktop uploads."""
    filters = ('setpts=PTS-STARTPTS,scale=480:270:force_original_aspect_ratio=decrease:reset_sar=1,'
               'pad=480:270:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1')
    return [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-progress', 'pipe:1', '-nostats', '-stats_period', '0.5', '-threads', '2', '-filter_threads', '1',
            '-copyts', '-i', str(source), '-map', '0:v:0', '-map', '0:a?', '-vf', filters,
            '-c:v', 'libx264', '-preset', 'veryfast', '-threads', encoder_threads(4), '-b:v', '256k',
            '-maxrate', '320k', '-bufsize', '512k', '-g', '15', '-keyint_min', '15',
            '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough', '-c:a', 'aac', '-b:a', '32k',
            '-af', f'asetpts=PTS-({audio_start:.9f})/TB,atrim=start=0:end={duration_ms / 1000:.6f}',
            '-movflags', '+faststart', str(target)]
