from concurrent.futures import ThreadPoolExecutor, TimeoutError
import tempfile
import threading
import time
import unittest

from core.api.client import CloudError, MIB
from core.fs import Filesystem, FsError
from core.io.reader import Reader


class MemoryClient:
    algorithm = "sha256"

    def __init__(self):
        self.data = bytes(range(251)) * (3 * MIB // 251 + 1)
        self.node = dict(fileId="file", parentFileId="/", name="probe.exe", type="file",
                         size=len(self.data), revisionId="v1")
        self.started = threading.Event()
        self.resume = threading.Event()
        self.block_first = False
        self.fail_first = False
        self.deleted = False
        self.requests = []
        self.mutex = threading.Lock()

    def list(self, parent):
        return [] if self.deleted else [dict(self.node)]

    def download_url(self, file_id):
        if self.deleted:
            raise CloudError("04010300", "download-url")
        return "opaque-ticket", time.time() + 900

    def read_range(self, url, start, end, size):
        with self.mutex:
            first = not self.requests
            self.requests.append((start, end))
            data = self.data[start:end + 1]
        if first and self.block_first:
            self.started.set()
            if not self.resume.wait(timeout=5):
                raise RuntimeError("test did not unblock the transfer")
            if self.fail_first:
                raise CloudError("403", "range")
        return data

    def trash(self, file_id):
        self.deleted = True

    def rename(self, file_id, name):
        self.node["name"] = name


class ReadConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = MemoryClient()
        self.fs = Filesystem(self.client, self.temp.name, meta_ttl=60)
        self.handle = self.fs.open("/probe.exe")

    def test_directory_lookup_finishes_while_remote_read_is_blocked(self):
        self.client.block_first = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(self.fs.read, self.handle, 0, 32)
            self.assertTrue(self.client.started.wait(timeout=2))
            listing = pool.submit(self.fs.list, "/")
            try:
                try:
                    nodes = listing.result(timeout=.5)
                except TimeoutError:
                    self.fail("cached directory waits for unrelated remote file read")
                self.assertEqual(nodes[0]["name"], "probe.exe")
                self.assertFalse(read.done())
            finally:
                self.client.resume.set()
            self.assertEqual(read.result(timeout=2), self.client.data[:32])
        self.fs.release(self.handle)

    def test_small_probe_does_not_download_a_full_multimegabyte_block(self):
        self.assertEqual(self.fs.read(self.handle, 0, 32), self.client.data[:32])
        downloaded = sum(end - start + 1 for start, end in self.client.requests)
        self.assertLessEqual(downloaded, 64 * 1024, "32-byte probe downloads MiB of data")
        self.fs.release(self.handle)

    def test_release_during_remote_read_pins_the_entry_until_completion(self):
        self.client.block_first = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(self.fs.read, self.handle, 0, 32)
            self.assertTrue(self.client.started.wait(timeout=2))
            release = pool.submit(self.fs.release, self.handle)
            try:
                release.result(timeout=.5)
                with self.assertRaises(FsError):
                    self.fs.handle_attributes(self.handle)
                self.assertFalse(read.done())
            finally:
                self.client.resume.set()
            self.assertEqual(read.result(timeout=2), self.client.data[:32])
        self.assertEqual(self.fs.active, {})

    def test_truncate_then_release_during_read_uses_current_local_contents(self):
        self.client.block_first = True
        writable = self.fs.open("/probe.exe", True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(self.fs.read, self.handle, 0, 32)
            self.assertTrue(self.client.started.wait(timeout=2))
            mutation = pool.submit(self.fs.truncate, "/probe.exe", 0, writable)
            try:
                mutation.result(timeout=.5)
                self.fs.handle(writable).dirty = False  # No upload needed for this read race.
                self.fs.release(writable)
                self.fs.release(self.handle)
            finally:
                self.client.resume.set()
            self.assertEqual(read.result(timeout=2), b"")
        self.assertEqual(self.fs.active, {})

    def test_unlink_during_read_preserves_open_file_when_remote_url_fails(self):
        self.client.block_first = self.client.fail_first = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(self.fs.read, self.handle, 0, 32)
            self.assertTrue(self.client.started.wait(timeout=2))
            remove = pool.submit(self.fs.remove, "/probe.exe")
            try:
                remove.result(timeout=.5)
                self.assertEqual(self.fs.list("/"), [])
                self.fs.release(self.handle)
            finally:
                self.client.resume.set()
            self.assertEqual(read.result(timeout=2), self.client.data[:32])
        self.assertEqual(self.fs.active, {})

    def test_rename_during_read_keeps_the_open_entry_and_new_path(self):
        self.client.block_first = True
        with ThreadPoolExecutor(max_workers=2) as pool:
            read = pool.submit(self.fs.read, self.handle, 0, 32)
            self.assertTrue(self.client.started.wait(timeout=2))
            rename = pool.submit(self.fs.rename, "/probe.exe", "/renamed.exe")
            try:
                rename.result(timeout=.5)
                self.assertEqual(self.fs.lookup("/renamed.exe")["fileId"], "file")
            finally:
                self.client.resume.set()
            self.assertEqual(read.result(timeout=2), self.client.data[:32])
        self.fs.release(self.handle)
        self.assertEqual(self.fs.active, {})

    def test_read_failure_releases_operation_pin(self):
        def fail(*args):
            raise CloudError("transport", "range")
        self.client.read_range = fail
        with self.assertRaises(CloudError):
            self.fs.read(self.handle, 0, 32)
        self.fs.release(self.handle)
        self.assertEqual(self.fs.active, {})


class ReaderTest(unittest.TestCase):
    def test_sequential_small_reads_promote_to_large_blocks(self):
        client = MemoryClient()
        reader = Reader(client, 4 * MIB)
        actual = b"".join(reader.read(client.node, offset, 4096)
                          for offset in range(0, MIB, 4096))
        self.assertEqual(actual, client.data[:MIB])
        self.assertLessEqual(len(client.requests), 4, "small-read optimization breaks sequential throughput")

    def test_small_read_reuses_existing_large_block(self):
        client = MemoryClient()
        reader = Reader(client, 4 * MIB)
        reader.read(client.node, MIB, MIB)
        before = len(client.requests)
        self.assertEqual(reader.read(client.node, 0, 32), client.data[:32])
        self.assertEqual(len(client.requests), before)

    def test_boundary_and_eof_are_correct_for_small_and_large_reads(self):
        client = MemoryClient()
        reader = Reader(client, 4 * MIB)
        for offset, size in ((65530, 32), (len(client.data) - 7, 64),
                             (123456, 100000), (len(client.data), 8), (0, 0)):
            with self.subTest(offset=offset, size=size):
                self.assertEqual(reader.read(client.node, offset, size), client.data[offset:offset + size])

    def test_custom_block_size_not_divisible_by_probe_size(self):
        client = MemoryClient()
        reader = Reader(client, 100000)
        reader.read(client.node, 0, 200000)
        # The 64 KiB probe window straddles a 100000-byte cached block.
        self.assertEqual(reader.read(client.node, 99000, 4096), client.data[99000:103096])

    def test_cache_evicts_within_byte_budget(self):
        client = MemoryClient()
        reader = Reader(client, 1000)
        for offset in range(0, 50000, 2000):
            self.assertEqual(reader.read(client.node, offset, 1), client.data[offset:offset + 1])
        self.assertLessEqual(reader.cache_bytes, 16000)
        self.assertEqual(reader.cache_bytes, sum(map(len, reader.blocks.values())))

    def test_concurrent_same_block_fetches_share_one_request(self):
        client = MemoryClient()
        client.block_first = True
        reader = Reader(client, 4 * MIB)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(reader.read, client.node, 0, 32)
            self.assertTrue(client.started.wait(timeout=2))
            second = pool.submit(reader.read, client.node, 16, 32)
            client.resume.set()
            self.assertEqual(first.result(timeout=2), client.data[:32])
            self.assertEqual(second.result(timeout=2), client.data[16:48])
        self.assertEqual(len(client.requests), 1)

    def test_invalidation_during_fetch_does_not_republish_old_cache_data(self):
        client = MemoryClient()
        client.block_first = True
        reader = Reader(client, 4 * MIB)
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(reader.read, client.node, 0, 32)
            self.assertTrue(client.started.wait(timeout=2))
            reader.invalidate("file")
            client.data = b"z" * len(client.data)
            client.resume.set()
            self.assertNotEqual(old.result(timeout=2), client.data[:32])
        self.assertEqual(reader.read(client.node, 0, 32), b"z" * 32)
        self.assertEqual(len(client.requests), 2)
