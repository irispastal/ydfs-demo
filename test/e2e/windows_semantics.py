"""Check real Win32 handle, sharing and locking behavior on the mounted drive."""
import ctypes as c
import sys
from contextlib import contextmanager
from pathlib import Path

k = c.WinDLL("kernel32", use_last_error=True)
H, D, B = c.c_void_p, c.c_uint32, c.c_int
READ, WRITE, SHARE_ALL = 0x80000000, 0x40000000, 7


class Overlapped(c.Structure):
    _fields_ = [("Internal", c.c_size_t), ("InternalHigh", c.c_size_t),
                ("Offset", D), ("OffsetHigh", D), ("hEvent", H)]


def api(name, result, *args):
    function = getattr(k, name)
    function.restype, function.argtypes = result, list(args)
    return function


create = api("CreateFileW", H, c.c_wchar_p, D, D, H, D, D, H)
close = api("CloseHandle", B, H)
read = api("ReadFile", B, H, H, D, c.POINTER(D), H)
write = api("WriteFile", B, H, H, D, c.POINTER(D), H)
seek = api("SetFilePointerEx", B, H, c.c_int64, c.POINTER(c.c_int64), D)
truncate = api("SetEndOfFile", B, H)
size = api("GetFileSizeEx", B, H, c.POINTER(c.c_int64))
flush = api("FlushFileBuffers", B, H)
lock = api("LockFileEx", B, H, D, D, D, D, c.POINTER(Overlapped))
unlock = api("UnlockFileEx", B, H, D, D, D, c.POINTER(Overlapped))
replace_file = api("ReplaceFileW", B, c.c_wchar_p, c.c_wchar_p, c.c_wchar_p, D, H, H)


def check(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


@contextmanager
def opened(path, access=READ | WRITE, sharing=SHARE_ALL):
    handle = create(str(path), access, sharing, None, 3, 0, None)  # OPEN_EXISTING
    if handle == H(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        yield handle
    finally:
        check(close(handle))


def read_start(handle, length):
    check(seek(handle, 0, None, 0))
    output, count = c.create_string_buffer(length), D()
    check(read(handle, output, length, c.byref(count), None))
    return output.raw[:count.value]


def denied(action, errors):
    try:
        action()
    except OSError as error:
        assert error.winerror in errors, repr(error)
    else:
        raise AssertionError("conflicting Windows operation unexpectedly succeeded")


def main():
    root = Path(sys.argv[1]) / "windows-semantics"
    root.mkdir()
    file, target = root / "中文-🙂.bin", root / "target"
    original = b"old\x00data\xff"
    file.write_bytes(original)
    assert file.read_bytes() == original
    assert root.stat().st_birthtime > 946684800, "Directory creation time is Unix epoch"
    assert file.stat().st_birthtime > 946684800, "File creation time is Unix epoch"
    with opened(file, READ, 1):  # Only FILE_SHARE_READ; deny delete, rename and writing.
        denied(file.unlink, {32})
        denied(lambda: file.rename(target), {32})
        def conflicting_open():
            with opened(file, WRITE):
                pass
        denied(conflicting_open, {32})

    with opened(file) as first, opened(file) as second:
        overlap = Overlapped()
        check(lock(first, 3, 0, 3, 0, c.byref(overlap)))  # Exclusive, fail immediately.
        try:
            denied(lambda: read_start(second, 3), {33})
        finally:
            check(unlock(first, 0, 3, 0, c.byref(overlap)))
        assert read_start(second, len(original)) == original

    target.write_bytes(b"replacement")
    with opened(file, READ) as old:
        # Ordinary MoveFileEx replacement of an open target is denied on NTFS
        # even with FILE_SHARE_DELETE. POSIX rename is a separate API/mode.
        denied(lambda: target.replace(file), {5})
        assert file.read_bytes() == original and target.read_bytes() == b"replacement"
        assert read_start(old, len(original)) == original
    target.replace(file)
    assert file.read_bytes() == b"replacement" and not target.exists()
    target.write_bytes(b"replacement-v2")
    check(replace_file(str(file), str(target), None, 0, None, None))
    assert file.read_bytes() == b"replacement-v2" and not target.exists()

    with opened(file) as handle:
        check(seek(handle, 3, None, 0))
        check(truncate(handle))
        length = c.c_int64()
        check(size(handle, c.byref(length)))
        assert length.value == 3
        check(flush(handle))
    assert file.read_bytes() == b"rep"

    with opened(file) as handle:
        file.unlink()  # FILE_SHARE_DELETE permits deletion with this handle open.
        assert read_start(handle, 3) == b"rep"
        check(seek(handle, 0, None, 0))
        count = D()
        check(write(handle, b"new", 3, c.byref(count), None))
        assert count.value == 3
        check(flush(handle))
    assert not file.exists()
    root.rmdir()
    print("Win32 checks passed: Unicode/binary I/O, share modes, byte-range lock, replacement rules/ReplaceFileW, handle truncate/flush, open delete", flush=True)


if __name__ == "__main__":
    main()
