"""Bounded, cancellable IMU submission with progress separate from S3 bytes."""
import time

import httpx
from fastapi import HTTPException


def submit_input(target, path, source, size, label, progress, cancel, *, clock=time.monotonic):
    started = clock()
    last = started
    sent = 0
    deadline = started + 900

    def payload():
        nonlocal last, sent
        with source.open('rb') as stream:
            while True:
                cancel()
                if clock() >= deadline:
                    raise HTTPException(504, 'IMU 提交超过 15 分钟，已停止等待；已上传视频保留，请检查连接后按原采集重试。')
                chunk = stream.read(256 * 1024)
                if not chunk:
                    if sent != size:
                        raise HTTPException(409, 'IMU 文件大小发生变化，未提交预标注。')
                    progress(f'{label}：本机已发送 {sent / 1024**2:.1f}/{size / 1024**2:.1f} MiB，等待服务端确认。')
                    return
                sent += len(chunk)
                if sent > size:
                    raise HTTPException(409, 'IMU 文件大小发生变化，未提交预标注。')
                yield chunk
                cancel()
                now = clock()
                if now - last >= 1:
                    progress(f'{label}：本机已发送 {sent / 1024**2:.1f}/{size / 1024**2:.1f} MiB；尚未确认完成。')
                    last = now

    progress(f'{label}：0.0/{size / 1024**2:.1f} MiB')
    try:
        result = target.request('PUT', path, content=payload(), headers={'Content-Length': str(size)},
                                timeout=httpx.Timeout(30, connect=15, write=15, pool=30))
    except httpx.HTTPError:
        raise HTTPException(504, 'IMU 提交连接中断或超时，服务端尚未确认完成；已上传视频保留，请检查连接后按原采集重试。') from None
    cancel()
    progress(f'{label}：服务端已确认接收。')
    return result
