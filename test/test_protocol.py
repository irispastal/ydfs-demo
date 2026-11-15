import io
import os
import threading
import unittest
from uuid import uuid4
from unittest.mock import patch

from core.api.client import Client, CloudError, MIB
from mock.server import server


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.auth = os.environ.get("YDFS_AUTH") or uuid4().hex
        self.http = server(0, 900, "sha256", self.auth)
        self.thread = threading.Thread(target=self.http.serve_forever)
        self.thread.start()
        self.client = Client(self.http.origin, self.auth, "test", "test", page_size=1)

    def tearDown(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()

    def test_lifecycle(self):
        client = self.client
        parent = client.mkdir("/", "first")["fileId"]
        target = client.mkdir("/", "second")["fileId"]
        self.assertEqual(len(client.list("/")), 2)
        with self.assertRaisesRegex(CloudError, "04010330"):
            client.mkdir("/", "FIRST")
        data = b"a" * (10 * MIB) + b"b" * (2 * MIB)
        node = client.upload(parent, "data", io.BytesIO(data))
        file_id = node["fileId"]
        url, _ = client.download_url(file_id)
        self.assertEqual(client.read_range(url, 10 * MIB - 1, 10 * MIB, len(data)), b"ab")
        client.rename(file_id, "changed")
        client.move(file_id, target)
        self.assertEqual(client.list(parent), [])
        self.assertEqual(client.list(target)[0]["name"], "changed")
        client.trash(file_id)
        self.assertEqual(client.list(target), [])
        self.assertIn(file_id, self.http.storage.trash)

    def test_validation(self):
        client = self.client
        part = dict(partNumber=1, partSize=1)
        args = dict(parentFileId="/", name="a", type="file", size=1, partInfos=[part],
                    contentHash="0" * 64, contentHashAlgorithm="sha256")
        with self.assertRaises(CloudError):
            client.post("/file/create", **{**args, "partInfos": [part] * 101})
        created = client.post("/file/create", **args)
        key = dict(fileId=created["fileId"], uploadId=created["uploadId"])
        with self.assertRaisesRegex(CloudError, "04010322"):
            client.post("/file/complete", **key, contentHash="0" * 64,
                        contentHashAlgorithm="sha256")
        self.http.storage.ttl = -1
        addresses = client.post("/file/getUploadUrl", **key, partInfos=[part])
        from urllib.request import Request
        with self.assertRaisesRegex(CloudError, "403"):
            client.transfer(Request(addresses["partInfos"][0]["uploadUrl"], b"x", method="PUT"),
                            "upload-part")

    def test_rapid_upload_commits_without_parts(self):
        created = dict(fileId="file-id", fileName="cached.bin", rapidUpload=True,
                       exist=False, uploadId=None, partInfos=None)
        with patch.object(self.client, "post", return_value=created) as post:
            node = self.client.upload("folder-id", "cached.bin", io.BytesIO(b"cached"))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args, ("/file/create",))
        self.assertEqual((node["fileId"], node["parentFileId"], node["name"], node["size"]),
                         ("file-id", "folder-id", "cached.bin", 6))
