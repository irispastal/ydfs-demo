"""Check FUSE 3 callbacks through mounted Linux filesystem operations."""
import ctypes
import errno
import os
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1]) / "fuse3-semantics"
    root.mkdir()
    source, target = root / "source", root / "target"
    source.write_bytes(b"source")
    target.write_bytes(b"target")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    libc.renameat2.restype = ctypes.c_int
    for flags in (1, 2, 4):  # NOREPLACE, EXCHANGE, WHITEOUT must not become a plain rename.
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), flags)
        expected = {errno.EOPNOTSUPP, errno.EINVAL, errno.EPERM}
        if flags == 1:
            expected.add(errno.EEXIST)  # The kernel can reject NOREPLACE before dispatch.
        assert result == -1 and ctypes.get_errno() in expected, (
            result, ctypes.get_errno())
        assert source.read_bytes() == b"source" and target.read_bytes() == b"target"
    missing = root / "missing"
    result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(missing), 1)
    assert result == -1 and ctypes.get_errno() == errno.EOPNOTSUPP
    assert source.read_bytes() == b"source" and not missing.exists()

    with target.open("rb", buffering=0) as old_target:
        source.replace(target)
        assert not source.exists() and target.read_bytes() == b"source"
        assert old_target.read() == b"target"
        assert os.fstat(old_target.fileno()).st_size == 6

    with target.open("r+b", buffering=0) as stream:
        stream.truncate(3)  # FUSE 3 passes the open handle to truncate/getattr.
        assert os.fstat(stream.fileno()).st_size == 3
        stream.seek(0)
        assert stream.read() == b"sou"
        stream.flush()
        os.fsync(stream.fileno())
    assert target.read_bytes() == b"sou"

    with target.open("r+b", buffering=0) as stream:
        target.unlink()
        assert not target.exists()
        stream.seek(0)
        assert stream.read() == b"sou"
        stream.seek(0)
        stream.write(b"new")
        assert os.fstat(stream.fileno()).st_size == 3
    assert not target.exists()  # Closing an unlinked handle must not resurrect its name.
    root.rmdir()
    print("FUSE 3 checks passed: rename flags, open-target replacement, ftruncate/fstat/fsync, open unlink", flush=True)


if __name__ == "__main__":
    main()
