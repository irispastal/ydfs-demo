"""Repeatable directory-open benchmark using the real HTTP client and adapter.

Run from the repository root. Fixtures exist only in the in-memory local mock.
--mount additionally measures Windows/Linux mounted enumeration and stat.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import statistics
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapter.fusefs import FuseFilesystem
from core.api.client import Client
from core.fs import Filesystem
from mock.server import server


def measure(action):
    start = time.perf_counter()
    result = action()
    return result, round((time.perf_counter() - start) * 1000, 3)


def benchmark(http, args, count):
    http.storage.files.clear()
    http.storage.events.clear()
    folder = dict(fileId="bench", parentFileId="/", name="bench", type="folder", size=0)
    http.storage.files["bench"] = folder
    for number in range(count):
        node = dict(fileId=f"f{number:06}", parentFileId="bench", name=f"item-{number:06}.txt",
                    type="file", size=number, updatedAt="2026-11-16T00:00:00Z")
        http.storage.files[node["fileId"]] = node
    samples = []
    with tempfile.TemporaryDirectory(prefix="ydfs-perf-") as cache_dir:
        options = {} if args.page_size is None else {"page_size": args.page_size}
        client = Client(http.origin, http.authorization, "perf-test", "perf-test", **options)
        fs = Filesystem(client, cache_dir, ignore_case=True, meta_ttl=60)
        adapter = FuseFilesystem(fs)

        def sweep(names):
            for name in names:
                # Exercise the fallback used by Windows for case variants too.
                attrs = adapter.getattr("/BENCH/" + name.upper())
                assert attrs["st_size"] == int(name[5:11])

        for _ in range(args.repeats):
            fs.cache.invalidate("/", "bench")
            before = len(http.storage.events)
            names, cold_ms = measure(lambda: adapter.readdir("/bench", None))
            names = [entry if isinstance(entry, str) else entry[0] for entry in names][2:]
            assert len(set(names)) == count
            cold_requests = len(http.storage.events) - before
            _, attrs_ms = measure(lambda: sweep(names))
            _, warm_ms = measure(lambda: adapter.readdir("/bench", None))
            assert len(http.storage.events) - before == cold_requests, "warm operation fetched HTTP"
            samples.append(dict(cold_list_ms=cold_ms, child_getattr_ms=attrs_ms,
                                warm_list_ms=warm_ms, cold_requests=cold_requests,
                                total_ms=round(cold_ms + attrs_ms, 3)))
        result = dict(entries=count, page_size=client.page_size, samples=samples,
                      median={key: round(statistics.median(s[key] for s in samples), 3)
                              for key in samples[0]})
        if args.mount:
            mount = Path(args.mount.rstrip("\\") + "\\" if os.name == "nt" else args.mount)
            if os.name == "nt" and mount.exists():
                raise RuntimeError("benchmark requires an unused drive letter")
            native_log = args.output.with_suffix(f".{count}.mount.log")
            mounted = []
            with native_log.open("w", encoding="utf-8") as log:
                command = [sys.executable, "-m", "cmd.ydfs", "--mount", args.mount,
                           "--base-url", http.origin, "--read-only", "--meta-ttl", "60",
                           "--log-level", args.log_level]
                if args.page_size is not None:
                    command.extend(["--page-size", str(args.page_size)])

                def ready():
                    return mount.exists() if os.name == "nt" else os.path.ismount(mount)

                def enumerate_and_stat():
                    with os.scandir(mount / "bench") as entries:
                        found = {entry.name: entry.stat().st_size for entry in entries}
                    assert len(found) == count
                    assert all(size == int(name[5:11]) for name, size in found.items())

                for _ in range(args.mount_repeats):
                    before = len(http.storage.events)
                    process = subprocess.Popen(command, env=dict(os.environ, YDFS_AUTH=http.authorization),
                                               stdout=log, stderr=log)
                    try:
                        deadline = time.monotonic() + 15
                        while not ready():
                            if process.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError(f"mount failed; see {native_log}")
                            time.sleep(.05)
                        _, cold = measure(enumerate_and_stat)
                        _, warm = measure(enumerate_and_stat)
                        mounted.append(dict(cold_ms=cold, warm_ms=warm,
                                            requests=len(http.storage.events) - before))
                    finally:
                        if os.name == "nt":
                            process.terminate()
                        else:
                            subprocess.run(["fusermount3", "-u", args.mount], check=True)
                        process.wait(timeout=15)
                    deadline = time.monotonic() + 15
                    while ready():
                        if time.monotonic() > deadline:
                            raise RuntimeError("mount did not disconnect")
                        time.sleep(.05)
            result["mounted"] = dict(samples=mounted, median={
                key: round(statistics.median(sample[key] for sample in mounted), 3)
                for key in mounted[0]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, nargs="+", default=[100, 1000, 3000])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--latency-ms", type=float, default=15)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mount", help="Unused Windows drive letter or empty Linux mount point")
    parser.add_argument("--mount-repeats", type=int, default=3)
    parser.add_argument("--page-size", type=int, help="Override page size to measure each optimization separately")
    parser.add_argument("--log-level", choices=("INFO", "DEBUG", "WARNING"), default="INFO")
    args = parser.parse_args()
    if args.repeats < 1 or args.mount_repeats < 1 or min(args.entries) < 1 or args.latency_ms < 0:
        parser.error("entries/repeats must be positive and latency must be nonnegative")
    if args.page_size is not None and not 1 <= args.page_size <= 1000:
        parser.error("page size must be between 1 and 1000")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.output.with_suffix(".log"), level=args.log_level,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    http = server(0, 900, "sha256", secrets.token_hex(16))
    original = http.storage.business

    def delayed(path, body, origin):
        if path == "/file/list":
            time.sleep(args.latency_ms / 1000)
        return original(path, body, origin)

    http.storage.business = delayed
    thread = threading.Thread(target=http.serve_forever)
    thread.start()
    try:
        results = dict(python=sys.version.split()[0], platform=sys.platform,
                       measured_at=datetime.now(timezone.utc).isoformat(), latency_ms=args.latency_ms,
                       repeats=args.repeats, mount_repeats=args.mount_repeats if args.mount else 0,
                       page_size=args.page_size, scenarios=[])
        for count in args.entries:
            result = benchmark(http, args, count)
            results["page_size"] = result["page_size"]
            results["scenarios"].append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    finally:
        http.shutdown()
        thread.join()
        http.server_close()


if __name__ == "__main__":
    main()
