"""Build small playback derivatives entirely from an already-local normal video."""
from __future__ import annotations

import math
import os
from pathlib import Path

from fastapi import HTTPException

from .previews import run_ffmpeg
from .encoding import encoder_threads


def _tool(service, stopping):
    if stopping.is_set():
        raise HTTPException(503, '本机播放素材准备已暂停。')
    ffmpeg = service.tool('ffmpeg')
    if not ffmpeg:
        raise HTTPException(503, '缺少 FFmpeg，无法准备本机播放素材。')
    return ffmpeg


def render_local_fast(service, normal: Path, target: Path, duration_ms: int, update, stopping) -> None:
    ffmpeg = _tool(service, stopping)
    temporary = target.with_suffix('.partial.mp4')
    # Sample before encoding. Every output second represents 20 original seconds;
    # do not upscale the 270p normal preview or read the original again.
    filters = ('setpts=PTS-STARTPTS,fps=3/2:start_time=0:round=up,'
               'settb=1/30,setpts=N,fps=30:start_time=0:round=up,trim=end_frame='
               + str(math.ceil(duration_ms * 1.5 / 1000)))
    args = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-progress', 'pipe:1', '-nostats', '-stats_period', '0.5',
            '-threads', '2', '-filter_threads', '1', '-i', str(normal),
            '-map', '0:v:0', '-an', '-vf', filters, '-fps_mode', 'cfr',
            '-c:v', 'libx264', '-preset', 'veryfast', '-threads', encoder_threads(2),
            '-crf', '25', '-g', '30', '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', str(temporary)]
    try:
        run_ffmpeg(args, max(1, round(duration_ms / 20)), target.with_suffix('.ffmpeg.log'),
                   update, stopping, timeout_seconds=max(300, duration_ms / 1000 * 2))
        rendered = service.probe(temporary)
        if abs(rendered.get('media_start_seconds', 0)) >= .001 or abs(rendered['duration_ms'] - duration_ms / 20) > 70:
            raise HTTPException(422, '高倍速预览时间校验失败，已停止使用，避免标注错位。')
        if stopping.is_set():
            raise HTTPException(503, '本机播放素材准备已暂停。')
        os.replace(temporary, target)
        update(100)
    finally:
        temporary.unlink(missing_ok=True)


def render_local_thumbnail(service, normal: Path, target: Path, duration_ms: int, stopping) -> None:
    ffmpeg = _tool(service, stopping)
    temporary = target.with_suffix('.partial.jpg')
    args = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-progress', 'pipe:1', '-nostats', '-threads', '1', '-filter_threads', '1',
            '-i', str(normal), '-map', '0:v:0', '-an', '-frames:v', '1',
            '-vf', 'scale=320:-2', '-threads', '1', '-q:v', '4', '-update', '1', str(temporary)]
    try:
        run_ffmpeg(args, duration_ms, target.with_suffix('.thumbnail.log'), lambda _: None,
                   stopping, timeout_seconds=120)
        if not service.valid_thumbnail(temporary):
            raise HTTPException(422, '时间轴封面生成失败，请重试。')
        if stopping.is_set():
            raise HTTPException(503, '本机播放素材准备已暂停。')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
