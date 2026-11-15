import posixpath
import logging
from dataclasses import dataclass
from functools import wraps
from tempfile import TemporaryFile
from threading import RLock
from time import monotonic

from core.api.client import CloudError, MIB
from core.io.reader import Reader
from core.meta.cache import DirectoryCache


class FsError(Exception):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


@dataclass
class Entry:
    node: dict
    stream: object = None
    dirty: bool = False
    unlinked: bool = False
    refs: int = 0

    def attributes(self):
        if self.stream is None:
            return self.node
        return {**self.node, "size": self.stream.seek(0, 2)}


@dataclass
class Handle:
    entry: Entry
    writable: bool


class Filesystem:
    def __init__(self, client, cache_dir, ignore_case=False, meta_ttl=5,
                 block_size=4 * MIB, root_id="/", disk_space=None):
        self.client = client
        self.cache_dir = cache_dir
        self.root = {"fileId": root_id, "type": "folder", "size": 0}
        self.cache = DirectoryCache(client, meta_ttl, ignore_case)
        self.reader = Reader(client, block_size)
        self.lock = RLock()
        self.active = {}
        self.handles = {}
        self.next_handle = 1
        self.disk_space = disk_space
        self.disk_space_lock = RLock()
        self.disk_space_bytes = (1024 * MIB, 1024 * MIB)
        self.disk_space_refresh = 0

    def children(self, parent):
        if parent["type"] != "folder":
            raise FsError("not-directory")
        parent_id = parent["fileId"]
        local = {self.cache.key(entry.node["name"]): entry.attributes()
                 for entry in self.active.values()
                 if not entry.unlinked and entry.stream is not None
                 and entry.node["parentFileId"] == parent_id}
        nodes = [node for node in self.cache.list(parent_id)
                 if self.cache.key(node["name"]) not in local]
        return nodes + list(local.values())

    @locked
    def lookup(self, path):
        node = self.root
        for name in path.strip("/").split("/") if path != "/" else ():
            nodes = self.children(node)
            exact = next((item for item in nodes if item["name"] == name), None)
            if exact is None and self.cache.ignore_case:
                exact = next((item for item in nodes if item["name"].casefold() == name.casefold()), None)
            if exact is None:
                raise FsError("missing")
            node = exact
        return node

    def optional(self, path):
        try:
            return self.lookup(path)
        except FsError as error:
            if error.kind != "missing":
                raise
            return None

    def destination(self, path):
        parent, name = posixpath.split(path)
        if not name:
            raise FsError("invalid")
        node = self.lookup(parent)
        if node["type"] != "folder":
            raise FsError("not-directory")
        return node["fileId"], name

    @locked
    def list(self, path):
        return self.children(self.lookup(path))

    @locked
    def mkdir(self, path):
        parent, name = self.destination(path)
        self.client.mkdir(parent, name)
        self.cache.invalidate(parent)

    def register(self, entry, writable):
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = Handle(entry, writable)
        entry.refs += 1
        return handle

    @locked
    def create(self, path):
        parent, name = self.destination(path)
        if self.optional(path) is not None:
            raise FsError("exists")
        node = dict(parentFileId=parent, name=name, type="file", size=0, fileId=None)
        entry = Entry(node, TemporaryFile(dir=self.cache_dir), dirty=True)
        self.active[id(entry)] = entry
        return self.register(entry, True)

    def entry(self, node):
        file_id = node["fileId"]
        for entry in self.active.values():
            if entry.unlinked:
                continue
            if file_id and entry.node["fileId"] == file_id:
                return entry
            if not file_id and entry.node["parentFileId"] == node["parentFileId"] and entry.node["name"] == node["name"]:
                return entry
        entry = Entry(node)
        self.active[id(entry)] = entry
        return entry

    @locked
    def open(self, path, writable=False):
        node = self.lookup(path)
        if node["type"] != "file":
            raise FsError("is-directory")
        return self.register(self.entry(node), writable)

    def handle(self, handle, write=False):
        item = self.handles.get(handle)
        if item is None or (write and not item.writable):
            raise FsError("bad-handle")
        return item.entry

    @locked
    def handle_attributes(self, handle):
        return self.handle(handle).attributes()

    def stage(self, entry, empty=False):
        if entry.stream is not None:
            return
        stream = TemporaryFile(dir=self.cache_dir)
        try:
            if not empty:
                size = entry.node["size"]
                for offset in range(0, size, self.reader.block_size):
                    stream.write(self.reader.read(entry.node, offset, self.reader.block_size))
        except BaseException:
            stream.close()
            raise
        entry.stream = stream

    @locked
    def read(self, handle, offset, size):
        entry = self.handle(handle)
        if entry.stream is None:
            return self.reader.read(entry.node, offset, size)
        entry.stream.seek(offset)
        return entry.stream.read(size)

    @locked
    def write(self, handle, offset, data):
        entry = self.handle(handle, write=True)
        self.stage(entry)
        entry.stream.seek(offset)
        written = entry.stream.write(data)
        entry.dirty = True
        return written

    @locked
    def truncate(self, path, size, handle=None):
        if handle is None:
            handle = self.open(path, True)
            try:
                self.truncate(path, size, handle)
            finally:
                self.release(handle)
            return
        entry = self.handle(handle, write=True)
        self.stage(entry, empty=size == 0)
        entry.stream.truncate(size)
        entry.dirty = True

    @locked
    def flush(self, handle):
        entry = self.handle(handle)
        if entry.unlinked or not entry.dirty:
            return
        node = entry.node
        parent, name, file_id = (node[key] for key in ("parentFileId", "name", "fileId"))
        entry.stream.flush()
        try:
            committed = self.client.upload(parent, name, entry.stream, file_id)
        except CloudError as error:
            if file_id or error.code != "04010330":
                raise
            match = next((item for item in self.client.list(parent)
                          if item["name"].casefold() == name.casefold()
                          and item["type"] == "file"), None)
            if match is None:
                raise
            committed = self.client.upload(parent, match["name"], entry.stream, match["fileId"])
        entry.node = committed
        entry.dirty = False
        self.reader.invalidate(committed["fileId"])
        self.cache.invalidate(parent)

    @locked
    def release(self, handle):
        entry = self.handle(handle)
        try:
            self.flush(handle)
        finally:
            del self.handles[handle]
            entry.refs -= 1
            if not entry.refs:
                self.active.pop(id(entry))
                if entry.stream is not None:
                    entry.stream.close()

    def discard(self, node):
        file_id = node["fileId"]
        if file_id:
            # Preserve open-file reads after unlink without relying on trashed URLs.
            for entry in self.active.values():
                if entry.node["fileId"] == file_id and not entry.unlinked:
                    self.stage(entry)
            self.client.trash(file_id)
        for entry in self.active.values():
            if (file_id and entry.node["fileId"] == file_id) or (
                    not file_id and entry.node["fileId"] is None
                    and entry.node["parentFileId"] == node["parentFileId"]
                    and entry.node["name"] == node["name"]):
                entry.unlinked = True
        self.cache.invalidate(node["parentFileId"])

    @locked
    def remove(self, path, directory=False):
        node = self.lookup(path)
        if node is self.root:
            raise FsError("invalid")
        if directory and node["type"] != "folder":
            raise FsError("not-directory")
        if not directory and node["type"] == "folder":
            raise FsError("is-directory")
        if directory and self.children(node):
            raise FsError("not-empty")
        self.discard(node)

    @locked
    def rename(self, source, target):
        node = self.lookup(source)
        if node is self.root:
            raise FsError("invalid")
        parent, name = self.destination(target)
        old_parent = node["parentFileId"]
        old_name = node["name"]
        file_id = node["fileId"]
        if old_parent == parent and old_name == name:
            return
        if file_id and name != old_name and len(name.encode()) > 200:
            raise FsError("name-too-long")
        if node["type"] == "folder" and target.startswith(source.rstrip("/") + "/"):
            raise FsError("invalid")
        existing = self.optional(target)
        if existing is not None and not (file_id and existing["fileId"] == file_id):
            if existing["type"] != node["type"]:
                raise FsError("is-directory" if existing["type"] == "folder" else "not-directory")
            if existing["type"] == "folder" and self.children(existing):
                raise FsError("not-empty")
            if (existing["fileId"] or existing["name"] != old_name
                    or existing["parentFileId"] != old_parent):
                self.discard(existing)
        entry = self.entry(node) if node["type"] == "file" else None
        try:
            if file_id and old_parent != parent:
                self.client.move(file_id, parent)
                if entry:
                    entry.node = {**entry.node, "parentFileId": parent}
            if file_id and old_name != name:
                self.client.rename(file_id, name)
            if entry:
                entry.node = {**entry.node, "parentFileId": parent, "name": name}
        finally:
            self.cache.invalidate(old_parent, parent)
            if entry and not entry.refs:
                self.active.pop(id(entry), None)

    def statfs(self):
        with self.disk_space_lock:
            if self.disk_space and monotonic() >= self.disk_space_refresh:
                try:
                    self.disk_space_bytes = self.disk_space()
                except CloudError as error:
                    logging.warning("Could not refresh cloud disk space: %s", error)
                    self.disk_space_refresh = monotonic() + 10
                else:
                    self.disk_space_refresh = monotonic() + 60
            total, free = self.disk_space_bytes
        return dict(f_bsize=4096, f_frsize=4096, f_blocks=total // 4096,
                    f_bfree=free // 4096, f_bavail=free // 4096, f_files=1000000,
                    f_ffree=1000000, f_namemax=255)
