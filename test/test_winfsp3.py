"""Exercise the native callback ABI, not just the Python adapter methods."""
import ctypes as c
import errno
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock


@unittest.skipUnless(os.name == "nt", "WinFsp is a Windows runtime")
class WinFsp3Test(unittest.TestCase):
    def setUp(self):
        from adapter import winfsp3
        self.binding = winfsp3

    def host(self, **callbacks):
        host = self.binding.FUSE.__new__(self.binding.FUSE)
        host.operations = SimpleNamespace(**callbacks)
        host.direct_io = True
        host.table = host.make_table()
        return host

    def test_winfsp_32_abi(self):
        b = self.binding
        self.assertEqual(b._library.fuse3_version(), 32)
        self.assertEqual((c.sizeof(b.FileInfo), b.FileInfo.fh.offset), (40, 16))
        self.assertEqual((c.sizeof(b.Stat), b.Stat.st_size.offset, b.Stat.st_birthtim.offset), (128, 40, 112))
        self.assertEqual(c.sizeof(b.StatVfs), 88)
        self.assertEqual((c.sizeof(b.OperationTable), b.OperationTable.create.offset), (320, 240))

    def test_cloud_creation_time_reaches_windows_stat(self):
        from adapter.fusefs import FuseFilesystem
        node = dict(type="folder", size=0, createdAt="2026-11-15T17:39:46.017+08:00",
                    updatedAt="2026-11-16T13:25:15.147+08:00", localCreatedAt=None,
                    localUpdatedAt=None)
        filesystem = FuseFilesystem(SimpleNamespace(lookup=lambda path: node))
        host = self.host(getattr=filesystem.getattr)
        output = self.binding.Stat()
        self.assertEqual(host.table.getattr(b"/folder", c.byref(output), None), 0)
        expected = datetime.fromisoformat(node["createdAt"]).timestamp()
        actual = output.st_birthtim.tv_sec + output.st_birthtim.tv_nsec / 1e9
        self.assertAlmostEqual(actual, expected, places=6)

    def test_creation_time_local_override_and_missing_field_fallback(self):
        from adapter.fusefs import FuseFilesystem
        cases = [
            dict(localCreatedAt="2025-01-02T03:04:05Z", createdAt="2026-01-01T00:00:00Z",
                 updatedAt="2026-11-16T00:00:00Z"),
            dict(createdAt=None, localCreatedAt=None, updatedAt="2026-11-16T00:00:00Z"),
        ]
        for times in cases:
            with self.subTest(times=times):
                node = dict(type="file", size=14, **times)
                filesystem = FuseFilesystem(SimpleNamespace(handle_attributes=lambda fh: node))
                host = self.host(getattr=filesystem.getattr)
                output, info = self.binding.Stat(), self.binding.FileInfo(fh=7)
                self.assertEqual(host.table.getattr(None, c.byref(output), c.byref(info)), 0)
                expected = times.get("localCreatedAt") or times.get("createdAt") or times["updatedAt"]
                self.assertEqual(output.st_birthtim.tv_sec,
                                 int(datetime.fromisoformat(expected.replace("Z", "+00:00")).timestamp()))
                self.assertEqual(output.st_mtim.tv_sec,
                                 int(datetime.fromisoformat(times["updatedAt"].replace("Z", "+00:00")).timestamp()))

    def test_native_callbacks_preserve_handles_and_binary_data(self):
        b = self.binding
        fh = 2**48 + 7
        data = b"a\x00b\xff"
        operations = dict(
            open=Mock(return_value=fh), read=Mock(return_value=data),
            write=Mock(return_value=len(data)), truncate=Mock(return_value=0),
            getattr=Mock(return_value={"st_mode": 0o100644, "st_size": 2**35, "st_mtime": 12.25}),
        )
        host = self.host(**operations)
        fi = b.FileInfo(flags=2)
        self.assertEqual(host.table.open("/中文".encode(), c.byref(fi)), 0)
        self.assertEqual((fi.fh, fi.direct_io), (fh, 1))
        output = c.create_string_buffer(len(data))
        self.assertEqual(host.table.read(None, output, len(data), 2**34, c.byref(fi)), len(data))
        self.assertEqual(output.raw, data)
        operations["read"].assert_called_once_with(None, len(data), 2**34, fh)
        self.assertEqual(host.table.write(None, output, len(data), 2**34, c.byref(fi)), len(data))
        operations["write"].assert_called_once_with(None, data, 2**34, fh)
        self.assertEqual(host.table.truncate(None, 2**33, c.byref(fi)), 0)
        operations["truncate"].assert_called_once_with(None, 2**33, fh)
        stat = b.Stat()
        self.assertEqual(host.table.getattr(None, c.byref(stat), c.byref(fi)), 0)
        self.assertEqual((stat.st_size, stat.st_mtim.tv_sec, stat.st_mtim.tv_nsec), (2**35, 12, 250_000_000))

    def test_rename_flags_and_errors_do_not_escape_native_callbacks(self):
        rename = Mock(return_value=0)
        host = self.host(rename=rename, unlink=Mock(side_effect=FileNotFoundError(errno.ENOENT, "missing")))
        for flags in (1, 2, 4):
            self.assertEqual(host.table.rename(b"/old", b"/new", flags), -errno.EOPNOTSUPP)
        rename.assert_not_called()
        self.assertEqual(host.table.rename(b"/old", b"/new", 0), 0)
        rename.assert_called_once_with("/old", "/new")
        self.assertEqual(host.table.unlink(b"/missing"), -errno.ENOENT)
        host.operations.unlink.side_effect = ValueError("unexpected")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(host.table.unlink(b"/bad"), -errno.EIO)

    def test_readdir_uses_five_argument_filler(self):
        b = self.binding
        host = self.host(readdir=lambda path, fh: [".", "..", "中文"])
        seen = []

        @b.FILLER
        def filler(buffer, name, stat, offset, flags):
            seen.append((buffer, name.decode(), bool(stat), offset, flags))
            return 0

        self.assertEqual(host.table.readdir(b"/", 123, filler, 0, None, 0), 0)
        self.assertEqual(seen, [(123, name, False, 0, 0) for name in (".", "..", "中文")])
