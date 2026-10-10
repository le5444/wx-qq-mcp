"""Durable, bounded-memory primitives for local export workflows."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_fingerprint(path):
    path = Path(path)
    before = path.stat()
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('Input changed while fingerprinting')
    return {'sha256': sha.hexdigest(), 'bytes': after.st_size}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def output_lock(folder):
    """An OS lock releases on crash; keep the inode to prevent lock races."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.workflow.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('This output directory is already being processed') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def open_store(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA temp_store=FILE')
    db.execute('PRAGMA cache_size=-2048')
    return db


def project_jsonl(path, rows):
    """Replace a derived JSONL atomically while holding one row in memory."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    count = 0
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            for row in rows:
                stream.write((row if isinstance(row, str) else canonical(row)) + '\n')
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return count


def snapshot_sources(snapshot):
    snapshot = Path(snapshot)
    manifest = snapshot / 'coverage.json'
    if manifest.exists():
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if value.get('complete') is not True:
            raise ValueError('Snapshot export is incomplete; resume export before processing media')
        sources = list(value.get('sources', {}))
    else:
        sources = [source for source in ('wechat', 'qq') if (snapshot / (source + '.jsonl')).is_file()]
    if not sources or any(source not in ('wechat', 'qq') for source in sources):
        raise ValueError('Snapshot must contain supported source files')
    if any(not (snapshot / (source + '.jsonl')).is_file() for source in sources):
        raise FileNotFoundError('A source declared by the snapshot manifest is missing')
    return sources
