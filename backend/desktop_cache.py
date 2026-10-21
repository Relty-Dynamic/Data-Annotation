"""User-visible disposable media; account/project databases stay in .local."""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import uuid
from pathlib import Path, PureWindowsPath
from fastapi import HTTPException
from .source_cache import is_reparse
from .playback_contract import asset_kinds

LOCK = threading.RLock()
APPLICATION = 'DataMark-desktop-cache-v1'


def sha256_file(path: Path, on_progress=None) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            read += len(block)
            if on_progress is not None:on_progress(read)
    return digest.hexdigest()


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', value).strip(' .').encode('utf-8')[:100].decode('utf-8', errors='ignore')
    if not cleaned or cleaned.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
        cleaned = '_' + cleaned
    if cleaned != value:
        cleaned += '-' + hashlib.sha256(value.encode()).hexdigest()[:8]
    return cleaned


def collection_name(project: dict) -> str:
    if project.get('collection_folder_name'):
        return safe_name(project['collection_folder_name'])
    raw = project.get('source_dir')
    if not raw:
        parents = {str(Path(item['path']).parent) for item in project.get('_sources', {}).values()}
        raw = next(iter(parents)) if len(parents) == 1 else None
    if raw:
        folder = PureWindowsPath(raw) if '\\' in raw else Path(raw)
        if folder.name.lower() == 'fpv':
            folder = folder.parent
        if folder.name:
            return safe_name(folder.name)
    return 'collection-' + project.get('source_fingerprint', project['id'])[:12]


def desktop_directory() -> Path:
    if sys.platform == 'win32':
        # FOLDERID_Desktop includes OneDrive/enterprise redirection.
        import uuid as guid_module
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(guid_module.UUID('B4BFCC3A-DB2C-424C-B029-7FE99A87C641').bytes_le)
        result = ctypes.c_wchar_p()
        shell = ctypes.windll.shell32
        ole = ctypes.windll.ole32
        ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ole.CoInitializeEx.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree.restype = None
        initialized = ole.CoInitializeEx(None, 2)
        if initialized < 0 and initialized != -2147417850:  # Already initialized with another apartment model is valid.
            raise HTTPException(503, '无法定位系统桌面，请设置缓存目录。')
        shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        try:
            if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(result)) != 0 or not result.value:
                raise HTTPException(503, '无法定位系统桌面，请设置缓存目录。')
            return Path(result.value)
        finally:
            ole.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))
            if initialized >= 0:
                ole.CoUninitialize()
    return Path.home() / 'Desktop'


class DesktopCache:
    def __init__(self, service):
        self.service = service
        self.enabled = os.getenv('DATAMARK_DESKTOP_CACHE', '') == '1' and not os.getenv('DATAMARK_ORIGIN', '').strip()
        self.settings_file = service.local / 'cache-settings.json'
        self.override = os.getenv('DATAMARK_DESKTOP_CACHE_ROOT', '').strip()
        if self.settings_file.is_file():
            self.override = json.loads(self.settings_file.read_text(encoding='utf-8'))['root']

    @property
    def root(self) -> Path:
        return Path(self.override).expanduser() if self.override else desktop_directory() / 'dataMarkProject'

    def settings(self) -> dict:
        return {'mode': 'native' if self.enabled else 'browser', 'root': str(self.root) if self.enabled else None}

    def configure(self, root: str) -> dict:
        if not self.enabled:
            raise HTTPException(404, '此服务由浏览器选择本机缓存目录。')
        path = Path(root).expanduser()
        if not path.is_absolute() or is_reparse(path):
            raise HTTPException(422, '请填写可读写的完整本机目录，不使用外部链接。')
        path.mkdir(parents=True, exist_ok=True)
        with LOCK:
            self.write_json(self.settings_file, {'root': str(path)})
            self.override = str(path)
        return self.settings()

    @staticmethod
    def write_json(path: Path, value: dict):
        if is_reparse(path):
            raise HTTPException(409, '缓存文件含外部链接。')
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.partial')
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def directory(self, project: dict) -> Path:
        root = self.root
        if is_reparse(root):
            raise HTTPException(409, '缓存根目录含外部链接。')
        root.mkdir(parents=True, exist_ok=True)
        directory = root / collection_name(project)
        if is_reparse(directory) or directory.resolve().parent != root.resolve():
            raise HTTPException(409, '采集缓存目录超出允许范围。')
        directory.mkdir(exist_ok=True)
        return directory

    def ref_name(self, project_id: str) -> str:
        owner = str(self.service.root.resolve()) + ':' + project_id
        return '.datamark-ref.' + hashlib.sha256(owner.encode()).hexdigest() + '.json'

    def retain_upload(self, prefix: str, video_name: str, source: Path, *, kind='normal', artifact=None) -> Path:
        """Keep the uploader's verified compressed video independently of staging."""
        project = {'id': hashlib.sha256(('upload:' + prefix).encode()).hexdigest()[:32],
                   'collection_folder_name': prefix.rsplit('/', 1)[-1]}
        directory = self.directory(project)
        digest = artifact.digest_for(source) if artifact is not None else sha256_file(source)
        if not re.fullmatch(r'normal|fast|thumbnail|storyboard-\d+', kind):
            raise HTTPException(422, '上传缓存类型无效。')
        filename = safe_name(Path(video_name).stem) + '.' + digest + '.' + kind + ('.mp4' if kind in ('normal', 'fast') else '.jpg')
        target = directory / filename
        with LOCK:
            if is_reparse(target):
                raise HTTPException(409, '上传缓存目标含外部链接，已保留原文件。')
            if target.exists() and sha256_file(target) != digest:
                raise HTTPException(409, '上传缓存同名文件校验不一致，已保留原文件。')
            owned = not target.exists()
            record = {'file_name': filename, 'size': source.stat().st_size, 'sha256': digest}
            if owned:
                record['owned'] = True
            self.reference(project, [record])
            if owned:
                temporary = directory / ('.' + uuid.uuid4().hex + '.partial')
                try:
                    self.service.sessions._check_space(directory, source.stat().st_size)
                    shutil.copyfile(source, temporary)
                    if sha256_file(temporary) != digest:
                        raise HTTPException(422, '上传缓存复制校验失败。')
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        return target

    def publish_entry(self, project: dict, video: dict, entry: dict, source: Path, target: Path) -> dict:
        result = dict(entry)
        stem = safe_name(Path(video['name']).stem)
        for kind, records in [(kind, [entry[kind]]) for kind in asset_kinds(entry['profile'])] + [('sheets', entry['sheets'])]:
            fresh = []
            for index, record in enumerate(records):
                path = self.service.sessions._valid_record(source, record, jpeg=kind in {'thumbnail','sheets'})
                if path is None:
                    raise HTTPException(422, '迁移的播放缓存无效，原文件已保留。')
                digest = record.get('sha256') or sha256_file(path)
                label = 'storyboard-' + f'{index:03d}' if kind == 'sheets' else kind
                filename = stem + '.' + digest + '.' + label + ('.mp4' if kind in {'normal','fast'} else '.jpg')
                destination = target / filename
                known = False
                for ref in target.glob('.datamark-ref.*.json'):
                    if is_reparse(ref): raise HTTPException(409, '缓存引用含外部链接。')
                    value = json.loads(ref.read_text(encoding='utf-8'))
                    if value.get('application') == APPLICATION:
                        known = known or any(item['file_name'] == filename and item['sha256'] == digest and item.get('owned', False) for item in value['files'])
                self.reference(project, [{'file_name': filename, 'size': record['size'], 'sha256': digest, 'owned': known or not destination.exists()}])
                with LOCK:
                    if is_reparse(destination):
                        raise HTTPException(409, '目标缓存文件含外部链接。')
                    if destination.exists() and sha256_file(destination) != digest:
                        if not known:
                            raise HTTPException(409, '同名目标文件不是已登记的缓存，已保留双方文件。')
                        os.replace(destination, target / ('.damaged.' + uuid.uuid4().hex + '.' + filename))
                    if destination.exists():
                        pass
                    elif source == target:
                        os.replace(path, destination)
                    else:
                        # Atomic publication prevents an interrupted migration from becoming playable.
                        temporary = target / ('.' + filename + '.' + uuid.uuid4().hex + '.partial')
                        try:
                            self.service.sessions._check_space(target, record['size'])
                            shutil.copyfile(path, temporary)
                            if sha256_file(temporary) != digest:
                                raise HTTPException(422, '桌面缓存复制校验失败，原缓存已保留。')
                            os.replace(temporary, destination)
                        finally:
                            temporary.unlink(missing_ok=True)
                if source == target and path != destination:
                    path.unlink(missing_ok=True)
                fresh.append({**self.service.sessions._file_record(destination, target), 'sha256': digest})
            result[kind] = fresh if kind == 'sheets' else fresh[0]
        self.service.sessions._write_json(target / (entry['key'] + '.assets.json'), result)
        return result

    def reference(self, project: dict, records: list[dict]):
        directory = self.directory(project)
        ref_path = directory / self.ref_name(project['id'])
        with LOCK:
            old = []
            if ref_path.exists():
                if is_reparse(ref_path):
                    raise HTTPException(409, '缓存引用含外部链接。')
                value = json.loads(ref_path.read_text(encoding='utf-8'))
                if value.get('application') != APPLICATION:
                    raise HTTPException(409, '缓存引用归属无法核验。')
                old = value['files']
            merged = {item['file_name']: item for item in old}
            for item in records:
                prior = merged.get(item['file_name'], {})
                merged[item['file_name']] = {**item, 'owned': item.get('owned', prior.get('owned', False))}
            ref = {'application': APPLICATION, 'collection_name': collection_name(project), 'files': list(merged.values())}
            self.write_json(ref_path, ref)
            index_directory = self.service.storage.directory(project['id'])
            index_directory.mkdir(parents=True, exist_ok=True)
            self.write_json(index_directory / 'desktop-cache.json', {'directory': str(directory), 'ref': self.ref_name(project['id'])})

    def register(self, project: dict, manifest: dict, records: list[dict]):
        self.reference(project, records)
        self.write_json(self.directory(project) / ('manifest.' + manifest['version'] + '.json'),
                        {'application': APPLICATION, 'collection_name': collection_name(project),
                         'profile': manifest['profile'], 'files': records})

    def clear(self, project: dict, documents: list[dict]):
        with LOCK:
            self._clear(project, documents)

    def _clear(self, project: dict, documents: list[dict]):
        index = self.service.storage.directory(project['id']) / 'desktop-cache.json'
        if not index.exists():
            return
        pointer = json.loads(index.read_text(encoding='utf-8'))
        directory = Path(pointer['directory'])
        if is_reparse(directory) or pointer['ref'] != self.ref_name(project['id']):
            raise HTTPException(409, '桌面缓存索引无效，已停止清理。')
        ref_path = directory / pointer['ref']
        if is_reparse(ref_path):
            raise HTTPException(409, '缓存引用含外部链接。')
        if not ref_path.exists():
            return
        own = json.loads(ref_path.read_text(encoding='utf-8'))
        if own.get('application') != APPLICATION:
            raise HTTPException(409, '缓存归属无效，已停止清理。')
        other = set()
        for ref in directory.glob('.datamark-ref.*.json'):
            if ref == ref_path: continue
            if is_reparse(ref): raise HTTPException(409, '缓存引用含外部链接。')
            value = json.loads(ref.read_text(encoding='utf-8'))
            if value.get('application') != APPLICATION: raise HTTPException(409, '缓存引用无法核验，已停止清理。')
            other.update(item['file_name'].casefold() for item in value['files'])
        originals = {os.path.abspath(item['path']) for document in documents for item in document.get('_sources', {}).values()}
        for record in own['files']:
            name = record['file_name']
            if Path(name).name != name or not (re.fullmatch(r'.+\.[0-9a-f]{64}\.(?:normal|fast|thumbnail|storyboard-\d+)\.(?:mp4|jpg)', name) or re.fullmatch(r'[0-9a-f]{32}\.(?:compact-v1\.mp4|fast20\.mp4|jpg|sheet-\d+\.jpg)', name)):
                raise HTTPException(409, '缓存文件清单无效，已停止清理。')
            path = directory / name
            if os.path.abspath(path) in originals or is_reparse(path):
                raise HTTPException(409, '缓存文件正被用作原视频或含外部链接，已停止清理。')
        for record in own['files']:
            path = directory / record['file_name']
            if record.get('owned', False) and record['file_name'].casefold() not in other and path.is_file() and sha256_file(path) == record['sha256']:
                path.unlink()
        ref_path.unlink()
        index.unlink()
