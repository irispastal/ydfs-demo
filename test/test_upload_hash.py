import hashlib
import io
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.api.client import Client, CloudError, MIB
from core.fs import Filesystem
from core.io.staging import StagedFile
from mock.server import server


class UploadHashTest(unittest.TestCase):
    def test_sequential_copy_does_not_reread_before_create(self):
        client = Client("http://unused", "test", "test", "test")
        with tempfile.TemporaryDirectory() as directory:
            fs = Filesystem(client, directory)
            with patch.object(client, "list", return_value=[]):
                handle = fs.create("/large.bin")
            entry = fs.handle(handle)
            self.addCleanup(entry.stream.close)
            chunk = b"x" * MIB
            expected = hashlib.sha256()
            for offset in range(0, 32 * MIB, len(chunk)):
                fs.write(handle, offset, chunk)
                expected.update(chunk)
            created = dict(fileId="uploaded", fileName="large.bin", rapidUpload=True)
            with patch.object(entry.stream, "read", wraps=entry.stream.read) as read:
                with patch.object(client, "post", return_value=created) as post:
                    fs.flush(handle)
                calls = read.call_count
            self.assertEqual(post.call_args.kwargs["contentHash"], expected.hexdigest())
            self.assertEqual(post.call_args.kwargs["size"], 32 * MIB)
            self.assertEqual(calls, 0, "sequential copy rereads the whole file before /file/create")

    def test_retry_reuses_digest_and_append_hashes_only_new_bytes(self):
        client = Client("http://unused", "test", "test", "test")
        with tempfile.TemporaryDirectory() as directory, StagedFile(directory, "sha256") as stream:
            stream.write(b"abc")
            stream.seek(0)
            stream.write(b"z")  # Invalidate the original digest before the first attempt.
            with patch.object(client, "post", side_effect=CloudError("transport", "/file/create")):
                with self.assertRaises(CloudError):
                    client.upload("/", "file", stream)
            stream.seek(0, 2)
            stream.write(b"def")
            with patch.object(stream, "read", wraps=stream.read) as read:
                with patch.object(client, "post", return_value=dict(
                        fileId="uploaded", fileName="file", rapidUpload=True)) as post:
                    client.upload("/", "file", stream)
                read.assert_not_called()
            self.assertEqual(post.call_args.kwargs["contentHash"], hashlib.sha256(b"zbcdef").hexdigest())

    def test_preallocation_then_sequential_copy_does_not_reread(self):
        with tempfile.TemporaryDirectory() as directory, StagedFile(directory, "sha256") as stream:
            stream.truncate(8)
            stream.write(b"abcd")
            stream.seek(0, 2)  # Attribute queries may move the shared stream position.
            stream.seek(4)
            stream.write(b"efgh")
            with patch.object(stream, "read", wraps=stream.read) as read:
                self.assertEqual(stream.content_hash("sha256"), hashlib.sha256(b"abcdefgh").hexdigest())
                read.assert_not_called()

    def test_mutations_match_full_hash_for_both_algorithms(self):
        for algorithm in ("sha256", "sha1"):
            with self.subTest(algorithm=algorithm), tempfile.TemporaryDirectory() as directory:
                with StagedFile(directory, algorithm) as stream:
                    expected = io.BytesIO()
                    rng = random.Random(32)
                    # Includes empty files, sparse/overlapping/out-of-order writes,
                    # shrink/grow/zero truncation and digest queries between writes.
                    for index in range(240):
                        offset = rng.randrange(128)
                        stream.seek(offset)
                        expected.seek(offset)
                        if index % 5 == 0:
                            # BytesIO.truncate does not extend; model filesystem zeros.
                            size = rng.randrange(128)
                            old_size = len(expected.getvalue())
                            expected.truncate(size)
                            if size > old_size:
                                expected.seek(old_size)
                                expected.write(b"\0" * (size - old_size))
                                expected.seek(offset)
                            stream.truncate(size)
                        else:
                            data = rng.randbytes(rng.randrange(24))
                            self.assertEqual(stream.write(data), expected.write(data))
                        if index % 3 == 0:
                            self.assertEqual(stream.content_hash(algorithm), hashlib.new(
                                algorithm, expected.getvalue()).hexdigest())
                            self.assertEqual(stream.tell(), offset if index % 5 == 0
                                             else expected.tell())
                    stream.truncate(0)
                    self.assertEqual(stream.content_hash(algorithm), hashlib.new(algorithm).hexdigest())
                    stream.seek(0)
                    stream.write(b"new")
                    other = "sha1" if algorithm == "sha256" else "sha256"
                    self.assertEqual(stream.content_hash(other), hashlib.new(other, b"new").hexdigest())

    def test_staged_multipart_and_reopened_append_pass_server_hash_validation(self):
        auth = "test-upload-hash"
        http = server(0, 900, "sha256", auth)
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            client = Client(http.origin, auth, "test", "test", part_size=5 * MIB)
            with tempfile.TemporaryDirectory() as directory:
                fs = Filesystem(client, directory)
                handle = fs.create("/file")
                self.addCleanup(fs.handle(handle).stream.close)
                data = b"x" * (5 * MIB) + b"tail"
                fs.write(handle, 0, data)
                fs.flush(handle)
                file_id = fs.handle(handle).node["fileId"]
                self.assertEqual(http.storage.objects[file_id], data)
                fs.release(handle)
                handle = fs.open("/file", True)
                fs.write(handle, len(data), b"append")  # Stage and hash remote bytes too.
                self.addCleanup(fs.handle(handle).stream.close)
                fs.write(handle, 1, b"Z")
                fs.truncate("/file", len(data) + 3, handle)
                fs.release(handle)
                expected = b"xZ" + data[2:] + b"app"
                self.assertEqual(http.storage.objects[file_id], expected)
                self.assertEqual(http.storage.files[file_id]["contentHash"],
                                 hashlib.sha256(expected).hexdigest())
        finally:
            http.shutdown()
            thread.join()
            http.server_close()


if __name__ == "__main__":
    unittest.main()
