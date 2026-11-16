from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest

from core.api.client import CloudError
from core.fs import Filesystem, FsError
from adapter.fusefs import FuseFilesystem


class BlockingUploadClient:
    algorithm = "sha256"

    def __init__(self):
        self.started = threading.Event()
        self.resume = threading.Event()
        self.nodes = {}
        self.payloads = []
        self.fail = False

    def list(self, parent):
        return [dict(node) for node in self.nodes.values() if node["parentFileId"] == parent]

    def upload(self, parent, name, stream, file_id=None):
        stream.seek(0)
        prefix = stream.read(3)
        self.started.set()
        if not self.resume.wait(5):
            raise RuntimeError("test did not release upload")
        if self.fail:
            raise CloudError("transport", "upload-part")
        data = prefix + stream.read()
        self.payloads.append(data)
        node = dict(fileId=file_id or name, parentFileId=parent, name=name,
                    type="file", size=len(data), revisionId=str(len(self.payloads)))
        self.nodes[node["fileId"]] = node
        return dict(node)

    def rename(self, file_id, name):
        self.nodes[file_id]["name"] = name

    def trash(self, file_id):
        self.nodes.pop(file_id)


class UploadConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = BlockingUploadClient()
        self.fs = Filesystem(self.client, self.temp.name, meta_ttl=60)
        self.handle = self.fs.create("/file")
        self.entry = self.fs.handle(self.handle)
        self.addCleanup(self.entry.stream.close)
        self.data = b"0123456789abcdef"
        self.fs.write(self.handle, 0, self.data)

    def test_lookup_and_listing_finish_during_flush_and_release(self):
        for operation in ("flush", "release"):
            with self.subTest(operation=operation):
                # Reuse an open second handle if the first was released.
                handle = self.handle
                if operation == "release":
                    self.fs.write(handle, len(self.data), b"x")
                self.client.started.clear()
                self.client.resume.clear()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    upload = pool.submit(getattr(self.fs, operation), handle)
                    self.assertTrue(self.client.started.wait(2))
                    try:
                        self.assertEqual(pool.submit(self.fs.lookup, "/").result(.5)["type"], "folder")
                        nodes = pool.submit(self.fs.list, "/").result(.5)
                        self.assertEqual(nodes[0]["name"], "file")
                        self.assertFalse(upload.done())
                    finally:
                        self.client.resume.set()
                    upload.result(2)

    def test_pending_new_file_has_creation_time_before_cloud_commit(self):
        attrs = FuseFilesystem.attributes(self.fs.handle_attributes(self.handle))
        self.assertGreater(attrs["st_birthtime"], 946684800)

    def test_attributes_and_local_reads_do_not_move_upload_cursor(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            upload = pool.submit(self.fs.flush, self.handle)
            self.assertTrue(self.client.started.wait(2))
            try:
                self.assertEqual(pool.submit(self.fs.handle_attributes, self.handle).result(.5)["size"],
                                 len(self.data))
                self.assertEqual(pool.submit(self.fs.read, self.handle, 7, 4).result(.5), b"789a")
            finally:
                self.client.resume.set()
            upload.result(2)
        self.assertEqual(self.client.payloads, [self.data])

    def test_same_file_write_waits_without_blocking_other_file(self):
        other = self.fs.create("/other")
        self.addCleanup(self.fs.handle(other).stream.close)
        with ThreadPoolExecutor(max_workers=3) as pool:
            upload = pool.submit(self.fs.flush, self.handle)
            self.assertTrue(self.client.started.wait(2))
            mutation = pool.submit(self.fs.write, self.handle, len(self.data), b"new")
            try:
                self.assertEqual(pool.submit(self.fs.write, other, 0, b"other").result(.5), 5)
                self.assertFalse(mutation.done())
            finally:
                self.client.resume.set()
            upload.result(2)
            mutation.result(2)
        self.assertEqual(self.client.payloads, [self.data])
        self.assertTrue(self.entry.dirty)
        self.fs.flush(self.handle)
        self.assertEqual(self.client.payloads[-1], self.data + b"new")

    def test_truncate_waits_without_holding_global_lock(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            upload = pool.submit(self.fs.flush, self.handle)
            self.assertTrue(self.client.started.wait(2))
            mutation = pool.submit(self.fs.truncate, "/file", 4, self.handle)
            try:
                self.assertEqual(pool.submit(self.fs.lookup, "/").result(.5)["type"], "folder")
                self.assertFalse(mutation.done())
            finally:
                self.client.resume.set()
            upload.result(2)
            mutation.result(2)
        self.fs.flush(self.handle)
        self.assertEqual(self.client.payloads, [self.data, self.data[:4]])

    def test_duplicate_flush_waits_and_does_not_upload_twice(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(self.fs.flush, self.handle)
            self.assertTrue(self.client.started.wait(2))
            second = pool.submit(self.fs.flush, self.handle)
            try:
                pool.submit(self.fs.lookup, "/").result(.5)
            finally:
                self.client.resume.set()
            first.result(2)
            second.result(2)
        self.assertEqual(self.client.payloads, [self.data])

    def test_rename_and_unlink_wait_for_pending_create(self):
        for operation in ("rename", "remove"):
            with self.subTest(operation=operation):
                self.entry.dirty = True
                self.client.started.clear()
                self.client.resume.clear()
                with ThreadPoolExecutor(max_workers=3) as pool:
                    upload = pool.submit(self.fs.flush, self.handle)
                    self.assertTrue(self.client.started.wait(2))
                    args = ("/file", "/renamed") if operation == "rename" else ("/renamed",)
                    mutation = pool.submit(getattr(self.fs, operation), *args)
                    try:
                        pool.submit(self.fs.lookup, "/").result(.5)
                        self.assertFalse(mutation.done())
                    finally:
                        self.client.resume.set()
                    upload.result(2)
                    mutation.result(2)
        self.assertEqual(self.fs.list("/"), [])
        self.assertEqual(self.fs.read(self.handle, 0, len(self.data)), self.data)
        self.fs.release(self.handle)
        self.assertEqual(self.fs.active, {})

    def test_failed_upload_keeps_dirty_content_and_can_retry(self):
        self.client.fail = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            upload = pool.submit(self.fs.flush, self.handle)
            self.assertTrue(self.client.started.wait(2))
            try:
                pool.submit(self.fs.lookup, "/").result(.5)
            finally:
                self.client.resume.set()
            with self.assertRaises(CloudError):
                upload.result(2)
        self.assertTrue(self.entry.dirty)
        self.client.fail = False
        self.fs.flush(self.handle)
        self.assertEqual(self.client.payloads, [self.data])
        self.fs.release(self.handle)
        with self.assertRaises(FsError):
            self.fs.handle_attributes(self.handle)
        self.assertEqual(self.fs.active, {})

    def test_handleless_truncate_does_not_keep_outer_global_lock_during_release(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            mutation = pool.submit(self.fs.truncate, "/file", 4)
            self.assertTrue(self.client.started.wait(2))
            try:
                pool.submit(self.fs.lookup, "/").result(.5)
            finally:
                self.client.resume.set()
            mutation.result(2)
        self.assertEqual(self.client.payloads, [self.data[:4]])

    def test_last_release_pins_stream_and_duplicate_flush_does_not_close_it(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            upload = pool.submit(self.fs.flush, self.handle)
            self.assertTrue(self.client.started.wait(2))
            close = pool.submit(self.fs.release, self.handle)
            try:
                pool.submit(self.fs.lookup, "/").result(.5)
                self.assertFalse(close.done())
            finally:
                self.client.resume.set()
            upload.result(2)
            close.result(2)
        self.assertEqual(self.client.payloads, [self.data])
        self.assertEqual(self.fs.active, {})

    def test_other_handle_write_after_release_keeps_new_content_dirty(self):
        second = self.fs.open("/file", True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            close = pool.submit(self.fs.release, self.handle)
            self.assertTrue(self.client.started.wait(2))
            mutation = pool.submit(self.fs.write, second, len(self.data), b"new")
            try:
                pool.submit(self.fs.lookup, "/").result(.5)
                self.assertFalse(mutation.done())
            finally:
                self.client.resume.set()
            close.result(2)
            mutation.result(2)
        self.assertTrue(self.entry.dirty)
        self.fs.release(second)
        self.assertEqual(self.client.payloads, [self.data, self.data + b"new"])
        self.assertEqual(self.fs.active, {})

    def test_rename_over_pending_target_waits_then_trashes_committed_target(self):
        other = self.fs.create("/other")
        self.addCleanup(self.fs.handle(other).stream.close)
        self.fs.write(other, 0, b"replacement")
        with ThreadPoolExecutor(max_workers=3) as pool:
            upload = pool.submit(self.fs.flush, self.handle)
            self.assertTrue(self.client.started.wait(2))
            rename = pool.submit(self.fs.rename, "/other", "/file")
            try:
                pool.submit(self.fs.lookup, "/").result(.5)
                self.assertFalse(rename.done())
            finally:
                self.client.resume.set()
            upload.result(2)
            rename.result(2)
        self.assertEqual(self.client.nodes, {})
        self.assertTrue(self.entry.unlinked)
        self.fs.release(other)
        self.assertEqual(self.client.payloads[-1], b"replacement")
