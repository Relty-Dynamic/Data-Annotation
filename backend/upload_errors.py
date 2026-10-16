"""Useful upload failures without exposing signed URLs, headers or local paths."""
import errno

import httpx
from fastapi import HTTPException


def failure_detail(error, stage='本机处理'):
    if isinstance(error, HTTPException) and isinstance(error.detail, str):
        return error.detail
    if isinstance(error, httpx.TimeoutException):
        reason = '网络连接或传输超时'
    elif isinstance(error, httpx.HTTPError):
        reason = '网络连接中断'
    elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
        reason = '本机磁盘空间不足'
    elif isinstance(error, OSError):
        reason = '本机文件或设备无法读取'
    elif isinstance(error, RuntimeError) and str(error) in {
        '上传中断，可重试续传；已完成文件和分片保留。',
        '上传期间源文件长度发生变化。',
    }:
        return str(error)
    else:
        reason = '处理失败'
    return f'{stage}未完成：{reason}。已生成缓存和已上传内容保留，可按原采集信息重试。'
