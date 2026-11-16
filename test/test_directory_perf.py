from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

from core.api.client import Client, CloudError
from core.perf import current, increment, stage, traced


class PagingTest(unittest.TestCase):
    def test_overlapping_pages_return_each_file_id_once(self):
        client = Client("http://127.0.0.1", "secret", "test", "test")
        first = dict(fileId="first", name="first")
        second = dict(fileId="second", name="second")
        third = dict(fileId="third", name="third")
        with patch.object(client, "post", side_effect=[
                dict(items=[first, second], nextPageCursor="next"),
                dict(items=[second], nextPageCursor="last"),
                dict(items=[third], nextPageCursor="")]):
            self.assertEqual(client.list("/"), [first, second, third])

    def test_empty_page_keeps_following_cursor_and_uses_large_pages(self):
        client = Client("http://127.0.0.1", "secret", "test", "test")
        with patch.object(client, "post", side_effect=[
                dict(items=[], nextPageCursor="next"),
                dict(items=[dict(name="file")], nextPageCursor="")]) as post:
            self.assertEqual(client.list("/"), [dict(name="file")])
        self.assertEqual(post.call_args_list[0].kwargs["pageInfo"],
                         dict(pageSize=1000, pageCursor=""))
        self.assertEqual(post.call_args_list[1].kwargs["pageInfo"]["pageCursor"], "next")

    def test_repeated_cursor_fails_instead_of_looping(self):
        client = Client("http://127.0.0.1", "secret", "test", "test")
        with patch.object(client, "post", return_value=dict(items=[], nextPageCursor="same")) as post:
            with self.assertRaisesRegex(CloudError, "repeated-cursor"):
                client.list("/")
        self.assertEqual(post.call_count, 2)

    def test_page_size_rejects_values_outside_protocol_range(self):
        for value in (0, 1001, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Client("http://127.0.0.1", "secret", "test", "test", page_size=value)


class PerfLogTest(unittest.TestCase):
    def test_nested_stages_share_one_trace_without_logging_arguments(self):
        @traced("inner")
        def inner():
            with stage("work"):
                increment("items", 3)

        @traced("outer")
        def outer(secret):
            inner()

        with self.assertLogs("ydfs.perf", level="INFO") as captured:
            outer("credential-do-not-log")
        self.assertEqual(len(captured.output), 1)
        self.assertNotIn("credential-do-not-log", captured.output[0])
        record = json.loads(captured.output[0].split("[directory-perf] ", 1)[1])
        self.assertEqual((record["operation"], record["status"], record["counts"]["items"]),
                         ("outer", "ok", 3))
        self.assertGreaterEqual(record["stages_ms"]["work"], 0)
        self.assertIsNone(current.get())

    def test_thread_traces_and_exception_cleanup_are_isolated(self):
        barrier = threading.Barrier(2)

        @traced("thread")
        def worker():
            barrier.wait(timeout=5)
            increment("items")

        @traced("failure")
        def fail():
            raise ValueError("credential-do-not-log")

        with self.assertLogs("ydfs.perf", level="INFO") as captured:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker) for _ in range(2)]
                for future in futures:
                    future.result(timeout=5)
            with self.assertRaises(ValueError):
                fail()
        records = [json.loads(line.split("[directory-perf] ", 1)[1]) for line in captured.output]
        self.assertEqual(len({row["trace"] for row in records}), 3)
        self.assertEqual([row["counts"]["items"] for row in records[:2]], [1, 1])
        self.assertEqual(records[-1]["status"], "error")
        self.assertNotIn("credential-do-not-log", "\n".join(captured.output))
        self.assertIsNone(current.get())
