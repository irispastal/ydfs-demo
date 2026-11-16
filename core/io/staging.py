"""Local staging with an incremental digest of the unchanged file prefix.

Callers serialize mutations. read_at preserves the upload cursor for concurrent
readers. Appends hash bytes already in memory; mutations invalidate the prefix
and defer remaining reads until upload. size can be queried without seeking.
"""
import hashlib
from tempfile import TemporaryFile
from threading import RLock
from core.perf import increment


class StagedFile:
    def __init__(self, directory, algorithm):
        self._digest = hashlib.new(algorithm)
        self._hashed = 0
        self._stream = TemporaryFile(dir=directory)
        self._lock = RLock()
        self.size = 0

    @property
    def hashed_bytes(self):
        return self._hashed

    @property
    def hash_algorithm(self):
        return self._digest.name

    def read(self, size=-1):
        with self._lock:
            return self._stream.read(size)

    def read_at(self, offset, size):
        # Preserve the uploader's cursor while another handle reads locally.
        with self._lock:
            position = self.tell()
            try:
                self.seek(offset)
                return self.read(size)
            finally:
                self.seek(position)

    def seek(self, offset, whence=0):
        with self._lock:
            return self._stream.seek(offset, whence)

    def tell(self):
        with self._lock:
            return self._stream.tell()

    def flush(self):
        with self._lock:
            self._stream.flush()

    def close(self):
        with self._lock:
            self._stream.close()

    def _reset(self, algorithm):
        self._digest = hashlib.new(algorithm)
        self._hashed = 0

    def write(self, data):
        with self._lock:
            offset = self.tell()
            if data and offset < self._hashed:
                self._reset(self._digest.name)
            written = self._stream.write(data)
            if written:
                self.size = max(self.size, self.tell())
            if offset == self._hashed and written:
                self._digest.update(memoryview(data)[:written])
                self._hashed += written
            return written

    def truncate(self, size=None):
        with self._lock:
            size = self._stream.truncate(size)
            self.size = size
            if size < self._hashed:
                self._reset(self._digest.name)
            return size

    def content_hash(self, algorithm):
        with self._lock:
            return self._content_hash(algorithm)

    def _content_hash(self, algorithm):
        if algorithm != self._digest.name:
            self._reset(algorithm)
        position = self.tell()
        try:
            size = self.seek(0, 2)
            self.seek(self._hashed)
            while self._hashed < size:
                chunk = self.read(min(1024 * 1024, size - self._hashed))
                if not chunk:
                    raise OSError("unexpected end of staging file")
                self._digest.update(chunk)
                self._hashed += len(chunk)
                increment("hash_read_bytes", len(chunk))
            return self._digest.hexdigest()
        finally:
            self.seek(position)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
