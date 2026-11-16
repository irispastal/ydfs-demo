import posixpath
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from threading import Condition, RLock
from time import monotonic

from core.api.client import CloudError, MIB
from core.io.reader import Reader
from core.io.staging import StagedFile
from core.meta.cache import DirectoryCache
from core.perf import increment, stage, timed_stage, traced


class FsError(Exception):
    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


@contextmanager
def state_lock(lock):
    with stage("lock_wait"):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with state_lock(self.lock):
            return method(self, *args, **kwargs)
    return call


@dataclass
class Entry:
    node: dict
    stream: object = None
    dirty: bool = False
    unlinked: bool = False
    refs: int = 0
    readers: int = 0
    uploading: bool = False

    def attributes(self):
        if self.stream is None:
            return self.node
        return {**self.node, "size": self.stream.size}


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
        self.upload_changed = Condition(self.lock)
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
        with stage("local_overlay"):
            local = {self.cache.key(entry.node["name"]): entry.attributes()
                     for entry in self.active.values()
                     if not entry.unlinked and entry.stream is not None
                     and entry.node["parentFileId"] == parent_id}
        remote = self.cache.list(parent_id)
        with stage("merge_children"):
            if not local:
                return list(remote)
            nodes = [node for node in remote if self.cache.key(node["name"]) not in local]
            return nodes + list(local.values())

    def child(self, parent, name):
        if parent["type"] != "folder":
            raise FsError("not-directory")
        with stage("local_overlay"):
            key = self.cache.key(name)
            local = None
            for entry in self.active.values():
                if (not entry.unlinked and entry.stream is not None
                        and entry.node["parentFileId"] == parent["fileId"]
                        and self.cache.key(entry.node["name"]) == key):
                    local = entry
            if local is not None:
                return local.attributes()
        return self.cache.lookup(parent["fileId"], name)

    @traced("lookup", quiet=True)
    @locked
    def lookup(self, path):
        with stage("lookup"):
            node = self.root
            for name in path.strip("/").split("/") if path != "/" else ():
                increment("path_components")
                exact = self.child(node, name)
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

    @traced("list")
    @locked
    def list(self, path):
        with stage("list_children"):
            nodes = self.children(self.lookup(path))
        increment("entries", len(nodes))
        return nodes

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
        created = datetime.now(timezone.utc).isoformat()
        node = dict(parentFileId=parent, name=name, type="file", size=0, fileId=None,
                    createdAt=created, updatedAt=created)
        entry = Entry(node, StagedFile(self.cache_dir, self.client.algorithm), dirty=True)
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

    @traced("open", quiet=True)
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

    def ready_handle(self, handle, write=False):
        """Called under the state lock; waiting releases it for other files."""
        entry = self.handle(handle, write)
        while entry.uploading:
            with stage("upload_wait"):
                self.upload_changed.wait()
            entry = self.handle(handle, write)
        return entry

    def wait_uploads(self, *nodes):
        for entry in self.active.values():
            if entry.uploading and any(node is not None and (
                    (node["fileId"] and entry.node["fileId"] == node["fileId"]) or
                    (not node["fileId"] and entry.node["parentFileId"] == node["parentFileId"]
                     and entry.node["name"] == node["name"])) for node in nodes):
                with stage("upload_wait"):
                    self.upload_changed.wait()
                # Namespace bindings may have changed while the lock was released.
                return True
        return False

    @locked
    def handle_attributes(self, handle):
        return self.handle(handle).attributes()

    def stage(self, entry, empty=False):
        if entry.stream is not None:
            return
        stream = StagedFile(self.cache_dir, self.client.algorithm)
        try:
            if not empty:
                size = entry.node["size"]
                for offset in range(0, size, self.reader.block_size):
                    stream.write(self.reader.read(entry.node, offset, self.reader.block_size))
        except BaseException:
            stream.close()
            raise
        entry.stream = stream

    @traced("read", quiet=True)
    def read(self, handle, offset, size):
        with state_lock(self.lock):
            entry = self.handle(handle)
            # release() may remove the handle while the transfer is in flight.
            # Keep the entry and any subsequently created staging stream alive.
            entry.readers += 1
            node = dict(entry.node)
        increment("requested_bytes", size)
        try:
            with state_lock(self.lock):
                remote = entry.stream is None
            if not remote:
                data = self.local_read(entry, offset, size)
            if remote:
                try:
                    with stage("read_blocks"):
                        data = self.reader.read(node, offset, size)
                except (CloudError, OSError):
                    # Unlink may have staged the open file and revoked its URL.
                    with state_lock(self.lock):
                        if entry.stream is None:
                            raise
                    data = self.local_read(entry, offset, size)
                else:
                    with state_lock(self.lock):
                        # Writes, truncate and unlink remain serialized by the
                        # state lock. Prefer their current local contents.
                        local = entry.stream is not None
                    if local:
                        data = self.local_read(entry, offset, size)
            increment("returned_bytes", len(data))
            return data
        finally:
            with state_lock(self.lock):
                entry.readers -= 1
                self.retire(entry)

    def local_read(self, entry, offset, size):
        with stage("local_read"):
            return entry.stream.read_at(offset, size)

    def retire(self, entry):
        # Called with the state lock held; active entries also protect unlink's
        # staging pass when the last handle has closed during a remote read.
        if not entry.refs and not entry.readers and not entry.uploading:
            self.active.pop(id(entry), None)
            if entry.stream is not None:
                entry.stream.close()

    @locked
    def write(self, handle, offset, data):
        entry = self.ready_handle(handle, write=True)
        self.stage(entry)
        entry.stream.seek(offset)
        written = entry.stream.write(data)
        entry.dirty = True
        return written

    def truncate(self, path, size, handle=None):
        if handle is None:
            handle = self.open(path, True)
            try:
                self.truncate(path, size, handle)
            finally:
                self.release(handle)
            return
        with state_lock(self.lock):
            entry = self.ready_handle(handle, write=True)
            self.stage(entry, empty=size == 0)
            entry.stream.truncate(size)
            entry.dirty = True

    @traced("flush", quiet=True, prefix="upload-perf")
    def flush(self, handle):
        self.commit(handle)

    @traced("release", quiet=True, prefix="upload-perf")
    def release(self, handle):
        self.commit(handle, release=True)

    def close_handle(self, handle, entry):
        del self.handles[handle]
        entry.refs -= 1
        self.retire(entry)

    def commit(self, handle, release=False):
        with state_lock(self.lock):
            entry = self.ready_handle(handle)
            if entry.unlinked or not entry.dirty:
                if release:
                    self.close_handle(handle, entry)
                return
            parent, name, file_id = (entry.node[key] for key in ("parentFileId", "name", "fileId"))
            entry.uploading = True
        # The entry stays alive and immutable until publication. Same-file
        # mutations wait on the condition, without holding the state lock.
        try:
            with timed_stage("upload_local_flush"):
                entry.stream.flush()
            try:
                committed = self.client.upload(parent, name, entry.stream, file_id)
            except CloudError as error:
                if file_id or error.code != "04010330":
                    raise
                with timed_stage("upload_conflict_lookup"):
                    match = next((item for item in self.client.list(parent)
                                  if item["name"].casefold() == name.casefold()
                                  and item["type"] == "file"), None)
                if match is None:
                    raise
                committed = self.client.upload(parent, match["name"], entry.stream, match["fileId"])
            with state_lock(self.lock):
                entry.node = committed
                entry.dirty = False
                self.reader.invalidate(committed["fileId"])
                self.cache.invalidate(parent)
        finally:
            with state_lock(self.lock):
                if release:
                    self.close_handle(handle, entry)
                entry.uploading = False
                self.upload_changed.notify_all()
                self.retire(entry)

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
        while self.wait_uploads(node):
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
        existing = self.optional(target)
        while self.wait_uploads(node, existing):
            node = self.lookup(source)
            existing = self.optional(target)
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
            if entry:
                self.retire(entry)

    @traced("statfs", quiet=True)
    def statfs(self):
        with self.disk_space_lock:
            if self.disk_space and monotonic() >= self.disk_space_refresh:
                try:
                    with stage("disk_space_request"):
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
