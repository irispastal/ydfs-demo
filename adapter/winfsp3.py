"""The WinFsp FUSE 3.2 FFI subset used by this demo (64-bit Windows).

ABI references: WinFsp inc/fuse3/{fuse.h,fuse_common.h} and
inc/fuse/winfsp_fuse.h. fuse3_* exports are the upstream FFI entry points;
see src/dll/fuse3/fuse3_compat.c. No FUSE 2 symbols are used here.
"""
import ctypes as c
import errno
import logging
import os
import signal
import sysconfig
import winreg
from functools import wraps
from pathlib import Path
from core.perf import increment, stage, traced


class FuseOSError(OSError):
    def __init__(self, code):
        super().__init__(code, os.strerror(code))


class Operations:
    """Implement the filesystem callbacks needed by the mounted filesystem."""


class Timespec(c.Structure):
    _fields_ = [("tv_sec", c.c_int64), ("tv_nsec", c.c_int64)]


class Stat(c.Structure):
    _fields_ = [
        ("st_dev", c.c_uint32), ("st_ino", c.c_uint64),
        ("st_mode", c.c_uint32), ("st_nlink", c.c_uint16),
        ("st_uid", c.c_uint32), ("st_gid", c.c_uint32),
        ("st_rdev", c.c_uint32), ("st_size", c.c_int64),
        ("st_atim", Timespec), ("st_mtim", Timespec), ("st_ctim", Timespec),
        ("st_blksize", c.c_int32), ("st_blocks", c.c_int64), ("st_birthtim", Timespec),
    ]


class StatVfs(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in (
        "f_bsize", "f_frsize", "f_blocks", "f_bfree", "f_bavail", "f_files",
        "f_ffree", "f_favail", "f_fsid", "f_flag", "f_namemax",
    )]


class FileInfo(c.Structure):
    # WinFsp's FUSE 3.2 header has six 1-bit fields plus 27 padding bits.
    # With MSVC layout these span TWO uint32 slots, so fh is at offset 16.
    _fields_ = [("flags", c.c_int)] + [
        (name, c.c_uint, 1) for name in (
            "writepage", "direct_io", "keep_cache", "flush", "nonseekable", "flock_release",
        )
    ] + [("padding", c.c_uint, 27), ("fh", c.c_uint64),
         ("lock_owner", c.c_uint64), ("poll_events", c.c_uint32)]


P = c.c_char_p
V = c.c_void_p
I = c.c_int
U = c.c_uint32
O = c.c_int64
S = c.c_size_t
FI = c.POINTER(FileInfo)
FILLER = c.CFUNCTYPE(I, V, P, c.POINTER(Stat), O, I)


def callback_type(*args):
    return c.CFUNCTYPE(I, *args)


class OperationTable(c.Structure):
    _fields_ = [
        ("getattr", callback_type(P, c.POINTER(Stat), FI)),
        ("readlink", V), ("mknod", V), ("mkdir", callback_type(P, U)),
        ("unlink", callback_type(P)), ("rmdir", callback_type(P)),
        ("symlink", V), ("rename", callback_type(P, P, U)), ("link", V),
        ("chmod", callback_type(P, U, FI)), ("chown", callback_type(P, U, U, FI)),
        ("truncate", callback_type(P, O, FI)), ("open", callback_type(P, FI)),
        ("read", callback_type(P, V, S, O, FI)), ("write", callback_type(P, V, S, O, FI)),
        ("statfs", callback_type(P, c.POINTER(StatVfs))),
        ("flush", callback_type(P, FI)), ("release", callback_type(P, FI)),
        ("fsync", callback_type(P, I, FI)),
        ("setxattr", V), ("getxattr", V), ("listxattr", V), ("removexattr", V),
        ("opendir", V), ("readdir", callback_type(P, V, FILLER, O, FI, I)),
        ("releasedir", V), ("fsyncdir", V), ("init", V), ("destroy", V), ("access", V),
        ("create", callback_type(P, U, FI)), ("lock", V),
        ("utimens", callback_type(P, c.POINTER(Timespec), FI)),
        ("bmap", V), ("ioctl", V), ("poll", V), ("write_buf", V),
        ("read_buf", V), ("flock", V), ("fallocate", V),
    ]


def load_library():
    arch = {"win-amd64": "x64", "win-arm64": "a64"}.get(sysconfig.get_platform())
    if arch is None or c.sizeof(V) != 8:
        raise RuntimeError("The YDFS WinFsp FUSE 3 binding requires 64-bit Windows Python")
    path = os.environ.get("FUSE_LIBRARY_PATH")
    if not path:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WinFsp", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
            install_dir = winreg.QueryValueEx(key, "InstallDir")[0]
        path = str(Path(install_dir) / "bin" / f"winfsp-{arch}.dll")
    library = c.CDLL(path)
    try:
        library.fuse3_version.argtypes = []
        library.fuse3_version.restype = I
        library.fuse3_main_real.argtypes = [I, c.POINTER(P), c.POINTER(OperationTable), S, V]
        library.fuse3_main_real.restype = I
    except AttributeError as error:
        raise RuntimeError("WinFsp DLL does not provide the required FUSE 3 FFI exports") from error
    version = library.fuse3_version()
    if version != 32:
        raise RuntimeError(f"Unsupported WinFsp FUSE API {version}; this binding targets 3.2")
    return library


_library = load_library()
BACKEND = f"ctypes / WinFsp FUSE {_library.fuse3_version() // 10}.{_library.fuse3_version() % 10} (fuse3_main_real)"


def decode(path):
    return None if path is None else path.decode("utf-8", "surrogateescape")


def handle(info):
    return info.contents.fh if info else None


def guarded(method):
    @wraps(method)
    def invoke(*args):
        try:
            return method(*args) or 0
        except OSError as error:
            return -(error.errno if error.errno and error.errno > 0 else errno.EIO)
        except BaseException:
            # No Python exception may escape a native callback.
            logging.exception("WinFsp FUSE 3 callback %s failed", method.__name__)
            return -errno.EIO
    return invoke


class FUSE:
    def __init__(self, operations, mountpoint, foreground=True, **options):
        if not foreground:
            raise ValueError("The Windows binding requires foreground=True")
        self.operations = operations
        self.direct_io = bool(options.pop("direct_io", False))
        self.table = self.make_table()
        flags = []
        for name in ("debug", "nothreads"):
            if options.pop(name, False):
                flags.append("-d" if name == "debug" else "-s")
        options.setdefault("fsname", type(operations).__name__)
        values = [key if value is True else f"{key}={value}" for key, value in options.items()
                  if value is not False]
        args = ["ydfs", "-f", *flags, "-o", ",".join(values), str(mountpoint)]
        argv = (P * len(args))(*(arg.encode("utf-8") for arg in args))
        # Match foreground fusepy behavior: the default console handler exits
        # on Ctrl+C, letting WinFsp tear down the disconnected mount.
        old_handler = signal.signal(signal.SIGINT, signal.SIG_DFL)
        try:
            result = _library.fuse3_main_real(len(args), argv, c.byref(self.table),
                                             c.sizeof(self.table), None)
        finally:
            signal.signal(signal.SIGINT, old_handler)
        if result:
            raise RuntimeError(f"WinFsp fuse3_main_real failed: {result}")

    def make_table(self):
        table = OperationTable()
        for name, prototype in table._fields_:
            if prototype is V or not callable(getattr(self.operations, name, None)):
                continue
            setattr(table, name, prototype(guarded(getattr(self, "_" + name))))
        return table

    def _getattr(self, path, output, info):
        attrs = self.operations.getattr(decode(path), handle(info))
        c.memset(output, 0, c.sizeof(Stat))
        for name, value in attrs.items():
            if name in {"st_atime", "st_mtime", "st_ctime", "st_birthtime"}:
                timespec = getattr(output.contents, name.replace("time", "tim"))
                timespec.tv_sec = int(value)
                timespec.tv_nsec = int((value - int(value)) * 1_000_000_000)
            elif hasattr(output.contents, name):
                setattr(output.contents, name, value)

    def _open(self, path, info):
        info.contents.fh = self.operations.open(decode(path), info.contents.flags)
        info.contents.direct_io = self.direct_io

    def _create(self, path, mode, info):
        info.contents.fh = self.operations.create(decode(path), mode, info.contents.flags)
        info.contents.direct_io = self.direct_io

    def _read(self, path, output, size, offset, info):
        data = self.operations.read(decode(path), size, offset, handle(info))
        if len(data) > size:
            raise FuseOSError(errno.EIO)
        c.memmove(output, data, len(data))
        return len(data)

    def _write(self, path, data, size, offset, info):
        return self.operations.write(decode(path), c.string_at(data, size), offset, handle(info))

    @traced("native_readdir")
    def _readdir(self, path, buffer, filler, offset, info, flags):
        # Non-offset mode: supply the entire directory, with offset/PLUS = 0.
        entries = self.operations.readdir(decode(path), handle(info))
        with stage("native_fill"):
            for name in entries:
                increment("filled_entries")
                if filler(buffer, name.encode("utf-8", "surrogateescape"), None, 0, 0):
                    break

    def _statfs(self, path, output):
        c.memset(output, 0, c.sizeof(StatVfs))
        for name, value in self.operations.statfs(decode(path)).items():
            setattr(output.contents, name, value)

    def _truncate(self, path, size, info):
        return self.operations.truncate(decode(path), size, handle(info))

    def _rename(self, old, new, flags):
        if flags:
            raise FuseOSError(errno.EOPNOTSUPP)
        return self.operations.rename(decode(old), decode(new))

    def _mkdir(self, path, mode):
        return self.operations.mkdir(decode(path), mode)

    def _unlink(self, path):
        return self.operations.unlink(decode(path))

    def _rmdir(self, path):
        return self.operations.rmdir(decode(path))

    def _flush(self, path, info):
        return self.operations.flush(decode(path), handle(info))

    def _release(self, path, info):
        return self.operations.release(decode(path), handle(info))

    def _fsync(self, path, datasync, info):
        return self.operations.fsync(decode(path), datasync, handle(info))

    def _chmod(self, path, mode, info):
        return self.operations.chmod(decode(path), mode)

    def _chown(self, path, uid, gid, info):
        return self.operations.chown(decode(path), uid, gid)

    def _utimens(self, path, times, info):
        values = None if not times else tuple(times[i].tv_sec + times[i].tv_nsec / 1e9 for i in range(2))
        return self.operations.utimens(decode(path), values)
