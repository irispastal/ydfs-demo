import tempfile
import unittest
from unittest.mock import Mock, patch

from core.fs import Filesystem, FsError


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.nodes = [dict(fileId=str(i), parentFileId="/", name=f"item-{i}", type="file", size=i)
                      for i in range(1000)]
        self.client = Mock()
        self.client.algorithm = "sha256"
        self.client.list.side_effect = lambda parent: [dict(node) for node in self.nodes
                                                     if node["parentFileId"] == parent]
        self.fs = Filesystem(self.client, self.temp.name, ignore_case=True)

    def test_warm_attribute_lookup_does_not_scan_directory(self):
        self.fs.list("/")
        with patch.object(self.fs.cache, "key", wraps=self.fs.cache.key) as key:
            self.assertEqual(self.fs.lookup("/ITEM-999")["size"], 999)
            self.assertLessEqual(key.call_count, 4, "cached getattr scans every sibling")
        self.client.list.assert_called_once_with("/")

    def test_exact_case_then_folded_and_missing(self):
        self.nodes[:0] = [dict(fileId="upper", parentFileId="/", name="Item-0", type="file", size=8)]
        self.assertEqual(self.fs.lookup("/item-0")["fileId"], "0")
        self.assertEqual(self.fs.lookup("/ITEM-0")["fileId"], "upper")
        with self.assertRaises(FsError) as error:
            self.fs.lookup("/desktop.ini")
        self.assertEqual(error.exception.kind, "missing")
        self.client.list.assert_called_once()

    def test_local_overlay_and_invalidation(self):
        self.fs.list("/")
        handle = self.fs.open("/item-0", True)
        self.fs.write(handle, 0, b"local")
        self.assertEqual(self.fs.lookup("/ITEM-0")["size"], 5)
        self.assertEqual(next(n for n in self.fs.list("/") if n["name"] == "item-0")["size"], 5)
        self.fs.rename("/item-0", "/renamed")
        self.nodes[0]["name"] = "renamed"  # Mock the server-side rename.
        self.assertEqual(self.fs.lookup("/RENAMED")["size"], 5)
        with self.assertRaises(FsError):
            self.fs.lookup("/item-0")
        # Avoid uploading through the mock on cleanup.
        self.fs.handle(handle).dirty = False
        self.fs.release(handle)

    def test_expiry_refreshes_both_positive_and_negative_results(self):
        with patch("core.meta.cache.time.monotonic", return_value=1):
            self.fs.lookup("/item-0")
            with self.assertRaises(FsError):
                self.fs.lookup("/new")
        self.nodes[0]["name"] = "new"
        with patch("core.meta.cache.time.monotonic", return_value=10):
            self.assertEqual(self.fs.lookup("/new")["size"], 0)
            with self.assertRaises(FsError):
                self.fs.lookup("/item-0")
        self.assertEqual(self.client.list.call_count, 2)

    def test_case_sensitive_lookup_and_not_directory(self):
        self.fs.cache.ignore_case = False
        with self.assertRaises(FsError):
            self.fs.lookup("/ITEM-0")
        with self.assertRaises(FsError) as error:
            self.fs.lookup("/item-0/child")
        self.assertEqual(error.exception.kind, "not-directory")

    def test_new_local_file_rename_and_unlink_do_not_leave_index_entries(self):
        handle = self.fs.create("/new")
        self.fs.write(handle, 0, b"abc")
        self.fs.rename("/new", "/NEW-NAME")
        self.assertEqual(self.fs.lookup("/new-name")["size"], 3)
        with self.assertRaises(FsError):
            self.fs.lookup("/new")
        self.fs.remove("/new-name")
        with self.assertRaises(FsError):
            self.fs.lookup("/NEW-NAME")
        self.assertFalse(any(n["name"] == "NEW-NAME" for n in self.fs.list("/")))
        self.assertEqual(self.fs.read(handle, 0, 3), b"abc")
        self.fs.release(handle)
        self.client.upload.assert_not_called()
        self.client.rename.assert_not_called()
        self.client.trash.assert_not_called()

    def test_failed_refresh_does_not_publish_partial_or_stale_index(self):
        from core.api.client import CloudError
        self.fs.list("/")
        self.fs.cache.invalidate("/")
        self.client.list.side_effect = CloudError("transport", "/file/list")
        with self.assertRaises(CloudError):
            self.fs.lookup("/item-0")
        self.client.list.side_effect = None
        self.client.list.return_value = []
        with self.assertRaises(FsError):
            self.fs.lookup("/item-0")
        self.assertEqual(self.fs.list("/"), [])

    def test_zero_ttl_always_refreshes(self):
        self.fs.cache.ttl = 0
        self.fs.lookup("/item-0")
        self.nodes[0]["name"] = "new"
        self.assertEqual(self.fs.lookup("/new")["size"], 0)
        self.assertEqual(self.client.list.call_count, 2)


if __name__ == "__main__":
    unittest.main()
