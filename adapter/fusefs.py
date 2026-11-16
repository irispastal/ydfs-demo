import errno
import logging
import os
import re
import stat
from datetime import datetime
from functools import wraps

from adapter.binding import FuseOSError, Operations

from core.api.client import CloudError
from core.fs import FsError
from core.perf import stage, traced

ERRORS = {
    "missing": errno.ENOENT, "exists": errno.EEXIST, "not-empty": errno.ENOTEMPTY,
    "invalid": errno.EINVAL, "name-too-long": errno.ENAMETOOLONG,
    "not-directory": errno.ENOTDIR, "is-directory": errno.EISDIR, "bad-handle": errno.EBADF,
    "04010300": errno.ENOENT, "04000010": errno.ENOENT, "04010330": errno.EEXIST,
    "04000011": errno.EEXIST, "04000016": errno.ENOTEMPTY, "04000012": errno.ENOSPC,
    "04000005": errno.EACCES, "04000006": errno.EACCES, "04010105": errno.EPERM,
    "04010106": errno.EPERM,
}


def callback(method):
    @wraps(method)
    def invoke(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except FsError as error:
            code = ERRORS.get(error.kind, errno.EIO)
        except CloudError as error:
            code = ERRORS.get(error.code, errno.EIO)
            if code == errno.EIO:
                logging.error("%s", error)
        except OSError as error:
            code = error.errno or errno.EIO
        raise FuseOSError(code)
    return invoke


def validate(path):
    name = path.rsplit("/", 1)[-1]
    if not name or re.search(r'[\\/:*?"<>|\x00]', name):
        raise FsError("invalid")
    if len(name.encode()) > 255:
        raise FsError("name-too-long")


class FuseFilesystem(Operations):
    def __init__(self, fs, read_only=False):
        self.fs = fs
        self.read_only = read_only

    @callback
    @traced("getattr", quiet=True)
    def getattr(self, path, fh=None):
        node = self.fs.lookup(path) if not fh else self.fs.handle_attributes(fh)
        with stage("attributes"):
            return self.attributes(node)

    @staticmethod
    def attributes(node):
        directory = node["type"] == "folder"
        modified = node.get("localUpdatedAt") or node.get("updatedAt")
        mtime = datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp() if modified else 0
        created = node.get("localCreatedAt") or node.get("createdAt") or modified
        birthtime = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else mtime
        mode = stat.S_IFDIR if directory else stat.S_IFREG
        permissions = 0o755 if directory or os.name == "nt" else 0o644
        return dict(st_mode=mode | permissions,
                    st_nlink=2 if directory else 1, st_size=0 if directory else node["size"], st_mtime=mtime,
                    st_atime=mtime, st_ctime=mtime, st_birthtime=birthtime)

    @callback
    @traced("readdir")
    def readdir(self, path, fh):
        nodes = self.fs.list(path)
        with stage("names"):
            return [".", "..", *(node["name"] for node in nodes)]

    @callback
    def open(self, path, flags):
        writable = (flags & 3) != os.O_RDONLY
        if writable and self.read_only:
            raise FuseOSError(errno.EROFS)
        return self.fs.open(path, writable)

    @callback
    def create(self, path, mode, fi=None):
        validate(path)
        return self.fs.create(path)

    @callback
    def read(self, path, size, offset, fh):
        return self.fs.read(fh, offset, size)

    @callback
    def write(self, path, data, offset, fh):
        return self.fs.write(fh, offset, data)

    @callback
    def truncate(self, path, length, fh=None):
        self.fs.truncate(path, length, fh or None)
        return 0

    @callback
    def flush(self, path, fh):
        self.fs.flush(fh)
        return 0

    def fsync(self, path, datasync, fh):
        return self.flush(path, fh)

    @callback
    def release(self, path, fh):
        self.fs.release(fh)
        return 0

    @callback
    def mkdir(self, path, mode):
        validate(path)
        self.fs.mkdir(path)
        return 0

    @callback
    def rmdir(self, path):
        validate(path)
        self.fs.remove(path, directory=True)
        return 0

    @callback
    def unlink(self, path):
        validate(path)
        self.fs.remove(path)
        return 0

    @callback
    def rename(self, old, new):
        validate(new)
        self.fs.rename(old, new)
        return 0

    def statfs(self, path):
        return self.fs.statfs()

    def chmod(self, path, mode):
        return 0

    def chown(self, path, uid, gid):
        return 0

    def utimens(self, path, times=None):
        return 0
