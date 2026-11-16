import io
import json
import tempfile
import unittest
from unittest.mock import patch

from core.api.client import Client, CloudError, MIB
from core.io.staging import StagedFile


class UploadLoggingTest(unittest.TestCase):
    def test_retry_stage_timings_progress_and_hash_reuse_without_secrets(self):
        data = b"x" * (5 * MIB) + b"tail"
        client = Client("http://unused", "auth-do-not-log", "test", "test", part_size=5 * MIB)
        calls = {"put": 0, "complete": 0}

        def ticket(number):
            return dict(partNumber=number, uploadUrl="http://unused/private?token=signed-do-not-log")

        def post(path, **body):
            if path == "/file/create":
                return dict(fileId="file-id-do-not-log", uploadId="upload-id-do-not-log",
                            partInfos=[ticket(1), ticket(2)])
            if path == "/file/getUploadUrl":
                return dict(partInfos=[ticket(part["partNumber"]) for part in body["partInfos"]])
            self.assertEqual(path, "/file/complete")
            calls["complete"] += 1
            if calls["complete"] == 1:
                raise CloudError("04010322", path)
            return dict(fileId="done", size=len(data))

        def transfer(request, operation):
            self.assertEqual(operation, "upload-part")
            calls["put"] += 1
            if calls["put"] == 1:
                raise CloudError("403", operation)
            return 200, {}, b""

        with tempfile.TemporaryDirectory() as directory, StagedFile(directory, "sha256") as stream:
            stream.write(data)
            with self.assertLogs("ydfs.perf", level="INFO") as captured:
                with patch.object(client, "post", side_effect=post), patch.object(
                        client, "transfer", side_effect=transfer), patch("core.api.client.time.sleep") as sleep:
                    client.upload("parent-do-not-log", "private-name-do-not-log", stream)
            sleep.assert_called_once_with(1)
        output = "\n".join(captured.output)
        for secret in ("auth-do-not-log", "signed-do-not-log", "file-id-do-not-log",
                       "upload-id-do-not-log", "parent-do-not-log", "private-name-do-not-log"):
            self.assertNotIn(secret, output)
        records = [json.loads(line.split("[upload-perf] ", 1)[1]) for line in captured.output]
        self.assertEqual(len({row["trace"] for row in records}), 1)
        summary = records[-1]
        self.assertEqual((summary["operation"], summary["status"]), ("upload", "ok"))
        for name in ("upload_hash", "upload_create", "upload_read_part", "upload_put_part",
                     "upload_urls", "upload_complete_wait", "upload_complete"):
            self.assertIn(name, summary["stages_ms"])
            self.assertTrue(any(row["event"] == name and row["phase"] == "start" for row in records))
            self.assertTrue(any(row["event"] == name and row["phase"] == "end" for row in records))
        self.assertEqual(summary["counts"]["hash_reused_bytes"], len(data))
        self.assertEqual(summary["counts"].get("hash_read_bytes", 0), 0)
        self.assertEqual(summary["counts"]["upload_put_attempts"], 3)
        self.assertEqual(summary["counts"]["upload_complete_attempts"], 2)
        self.assertEqual(summary["counts"]["uploaded_bytes"], len(data))
        failures = [row for row in records if row.get("status") == "error"]
        self.assertEqual([row["code"] for row in failures], ["403", "04010322"])
        progress = [row for row in records if row["event"] == "upload_progress"]
        self.assertEqual(progress[-1]["completed_parts"], 2)
        self.assertEqual(progress[-1]["uploaded_bytes"], len(data))

    def test_hash_read_bytes_measure_fallback_and_invalidated_prefix(self):
        client = Client("http://unused", "secret", "test", "test")
        rapid = dict(fileId="done", fileName="private", rapidUpload=True)
        with tempfile.TemporaryDirectory() as directory, StagedFile(directory, "sha256") as staged:
            staged.write(b"abcdef")
            staged.seek(0)
            staged.write(b"Z")
            for stream, reused in ((io.BytesIO(b"Zbcdef"), 0), (staged, 1)):
                with self.subTest(reused=reused), self.assertLogs("ydfs.perf", level="INFO") as captured:
                    with patch.object(client, "post", return_value=rapid):
                        client.upload("parent", "private", stream)
                records = [json.loads(line.split("[upload-perf] ", 1)[1]) for line in captured.output]
                self.assertEqual(records[-1]["counts"]["hash_reused_bytes"], reused)
                self.assertEqual(records[-1]["counts"]["hash_read_bytes"], 6 - reused)

    def test_failure_is_logged_without_exception_message(self):
        client = Client("http://unused", "secret", "test", "test")
        with self.assertLogs("ydfs.perf", level="INFO") as captured:
            with patch.object(client, "post", side_effect=OSError("signed-secret-url")):
                with self.assertRaises(OSError):
                    client.upload("parent", "private", io.BytesIO(b"abc"))
        self.assertNotIn("signed-secret-url", "\n".join(captured.output))
        records = [json.loads(line.split("[upload-perf] ", 1)[1]) for line in captured.output]
        self.assertEqual(records[-1]["status"], "error")
        self.assertEqual(records[-2]["event"], "upload_create")
        self.assertEqual(records[-2]["status"], "error")
