import errno
import stat

from adapter.binding import FuseOSError, Operations


class FixedFile(Operations):
    path = "/hello.txt"
    content = b"YDFS smoke mount\n"

    def getattr(self, path, fh=None):
        if path == "/":
            return {"st_mode": stat.S_IFDIR | 0o555, "st_nlink": 2}
        self._require_file(path)
        return {
            "st_mode": stat.S_IFREG | 0o444,
            "st_nlink": 1,
            "st_size": len(self.content),
        }

    def readdir(self, path, fh):
        if path != "/":
            self._require_file(path)
            raise FuseOSError(errno.ENOTDIR)
        return [".", "..", self.path[1:]]

    def open(self, path, flags):
        self._require_file(path)
        return 0

    def read(self, path, size, offset, fh):
        self._require_file(path)
        return self.content[offset:offset + size]

    def _require_file(self, path):
        if path != self.path:
            raise FuseOSError(errno.ENOENT)
