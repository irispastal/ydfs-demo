"""Read-only real-cloud header probe overlapped with a cached directory open.

Each repeat starts with a fresh read cache. Only list, download-URL and signed
Range GET are used; output contains hashes/counters, never file content or names.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


class Operations(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        message = record.getMessage()
        if "[directory-perf] " in message:
            row = json.loads(message.split("[directory-perf] ", 1)[1])
            if row.get("operation", "").startswith("probe_"):
                self.records.append(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--request-bytes", type=int, default=32)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    from core.api.client import Client
    from core.fs import Filesystem
    from core.perf import traced
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if urlsplit(config["base_url"]).scheme != "https" or not config.get("root_file_id"):
        parser.error("HTTPS and a dedicated root are required")
    if args.repeats < 1 or not 1 <= args.request_bytes <= 65536:
        parser.error("positive repeats and 1..65536 requested bytes are required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.output.with_suffix(".log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    operations = Operations()
    logging.getLogger("ydfs.perf").addHandler(operations)
    report = dict(measured_at=datetime.now(timezone.utc).isoformat(), read_only=True,
                  requested_bytes=args.request_bytes, samples=[])
    try:
        for _ in range(args.repeats):
            client = Client(config["base_url"], config.get("authorization") or os.environ.get("YDFS_AUTH"),
                            config.get("client_info", "ydfs-demo"), config.get("app_channel", "ydfs-demo"))
            post = client.post

            def read_only(path, **body):
                if path not in {"/file/list", "/file/getDownloadUrl"}:
                    raise RuntimeError("only cloud reads are permitted")
                return post(path, **body)

            client.post = read_only
            with tempfile.TemporaryDirectory(prefix="ydfs-cloud-probe-") as temporary:
                fs = Filesystem(client, temporary, ignore_case=os.name == "nt", meta_ttl=60,
                                root_id=config["root_file_id"])
                nodes = fs.list("/")
                node = min((n for n in nodes if n["type"] == "file" and n["name"].lower().endswith(".exe")),
                           key=lambda n: n["size"])
                target = hashlib.sha256(node["fileId"].encode()).hexdigest()[:12]
                handle = fs.open("/" + node["name"])
                entered = threading.Event()
                download_url = client.download_url

                def signaled_url(file_id):
                    entered.set()
                    return download_url(file_id)

                client.download_url = signaled_url
                operations.records.clear()

                @traced("probe_read")
                def read_header():
                    return fs.read(handle, 0, args.request_bytes)

                @traced("probe_directory")
                def directory():
                    start = time.perf_counter()
                    found = fs.list("/")
                    # Include the attribute sweep a folder view needs.
                    for item in found:
                        fs.lookup("/" + item["name"])
                    return found, (time.perf_counter() - start) * 1000

                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(read_header)
                    if not entered.wait(timeout=10):
                        raise RuntimeError("download did not start")
                    found, elapsed = directory()
                    finished_first = not future.done()
                    header = future.result(timeout=65)
                fs.release(handle)
                if len(found) != len(nodes) or len(header) != args.request_bytes:
                    raise RuntimeError("read or directory validation failed")
                read = next(row for row in operations.records if row["operation"] == "probe_read")
                listing = next(row for row in operations.records if row["operation"] == "probe_directory")
                sample = dict(target=target, file_size=node["size"], entries=len(nodes),
                              header_hash=hashlib.sha256(header).hexdigest(),
                              directory_ms=round(elapsed, 3),
                              directory_lock_wait_ms=listing["stages_ms"].get("lock_wait", 0),
                              directory_finished_before_read=finished_first,
                              read_ms=read["total_ms"],
                              downloaded_bytes=read["counts"].get("downloaded_bytes", 0),
                              operations=operations.records[:])
                report["samples"].append(sample)
                report["median"] = {key: round(statistics.median(s[key] for s in report["samples"]), 3)
                                    for key in ("directory_ms", "directory_lock_wait_ms", "read_ms", "downloaded_bytes")}
                args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(dict(repeats=args.repeats, target=target, median=report["median"])), flush=True)
    except Exception as error:
        print(json.dumps(dict(status="error", kind=type(error).__name__,
                              code=getattr(error, "code", None))), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
