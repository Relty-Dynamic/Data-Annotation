"""In-process digest evidence for uploader-owned, finalized staging files only."""
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from .desktop_cache import sha256_file
from .source_cache import is_reparse


def identity(path):
    if is_reparse(path):
        raise HTTPException(409, '上传暂存文件不能包含链接。')
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True)
class UploadArtifact:
    path: Path
    stamp: tuple
    sha256: str

    @classmethod
    def read(cls, path):
        path = Path(path).absolute()
        before = identity(path)
        value = cls(path, before, sha256_file(path))
        value.digest_for(path)
        return value

    def digest_for(self, path):
        if Path(path).absolute() != self.path or identity(self.path) != self.stamp:
            raise HTTPException(409, '压缩成品在处理期间发生变化，请重新准备。')
        return self.sha256
