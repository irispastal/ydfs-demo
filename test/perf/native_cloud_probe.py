"""Exercise actual Win32 directory queries during cloud-backed header reads.

No GUI automation. Each sample gets a fresh read-only WinFsp network mount.
The child only adds boundary signals and captures existing performance records;
the filesystem implementation comes unchanged from --source-root.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

PREFIX = "[native-probe] "


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def manifest(rows):
    return hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=False).encode()).hexdigest()


def source_hashes(root):
    files = ("core/fs.py", "core/io/reader.py", "core/meta/cache.py", "core/api/client.py",
             "core/perf.py", "adapter/fusefs.py", "adapter/winfsp3.py", "cmd/ydfs/__main__.py")
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}


def child(args):
    import logging
    sys.path.insert(0, str(args.source_root.resolve()))
    from core.api.client import Client
    from core.fs import Filesystem
    from core.perf import current, traced

    output_lock = threading.Lock()
    metrics_lock = threading.Lock()
    lock_metrics = {}
    phase = "setup"

    def emit(event, **fields):
        trace = current.get()
        row = dict(event=event, phase=phase, mono_ns=time.perf_counter_ns(), **fields)
        if trace is not None:
            row.update(pid=trace["pid"], trace=trace["trace"])
        with output_lock:
            print(PREFIX + json.dumps(row, separators=(",", ":")), flush=True)

    class Capture(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if "[directory-perf] " in message:
                emit("perf", data=json.loads(message.split("[directory-perf] ", 1)[1]))

    logging.getLogger("ydfs.perf").addHandler(Capture())
    original_init = Filesystem.__init__
    original_post = Client.post
    original_url = Client.download_url
    original_read = Filesystem.read
    original_list = Filesystem.list

    class MeteredLock:
        """Same RLock semantics; collect quiet getattr waits without DEBUG I/O."""
        def __init__(self, lock):
            self.lock = lock

        def acquire(self, *values, **options):
            trace = current.get()
            active_phase = phase
            begin = time.perf_counter_ns()
            acquired = self.lock.acquire(*values, **options)
            elapsed = (time.perf_counter_ns() - begin) / 1e6
            if acquired and trace is not None and active_phase == "measure":
                key = trace["pid"], trace["trace"]
                with metrics_lock:
                    item = lock_metrics.setdefault(key, dict(pid=key[0], trace=key[1],
                        operation=trace["operation"], start_ns=begin, wait_ms=0, acquisitions=0))
                    item["wait_ms"] += elapsed
                    item["acquisitions"] += 1
            return acquired

        def release(self):
            self.lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exception):
            self.release()

    def initialize(self, *values, **options):
        original_init(self, *values, **options)
        self.lock = MeteredLock(self.lock)

    def read_only(self, path, **body):
        if path not in {"/file/list", "/file/getDownloadUrl", "/getPersonalDiskInfo"}:
            raise RuntimeError("native benchmark permits only cloud reads")
        return original_post(self, path, **body)

    def download_url(self, file_id):
        emit("download_begin", target=digest(file_id))
        return original_url(self, file_id)

    @traced("read", quiet=True)
    def read(self, handle, offset, size):
        emit("read_begin")
        try:
            return original_read(self, handle, offset, size)
        finally:
            emit("read_end")

    def listing(self, path):
        emit("directory_begin")
        return original_list(self, path)

    Client.post, Client.download_url = read_only, download_url
    Filesystem.__init__ = initialize
    Filesystem.read, Filesystem.list = read, listing

    def commands():
        nonlocal phase
        for line in sys.stdin:
            if line.strip() == "measure":
                phase = "measure"
                emit("phase_ready")
            elif line.strip() == "dump":
                with metrics_lock:
                    snapshot = [dict(item, wait_ms=round(item["wait_ms"], 6)) for item in lock_metrics.values()]
                emit("lock_metrics", data=snapshot)

    threading.Thread(target=commands, daemon=True).start()
    from cmd.ydfs.__main__ import main
    sys.argv = [sys.argv[0], "--config", str(args.config)]
    main()


class Signals:
    def __init__(self, stream, log_path):
        self.rows = []
        self.condition = threading.Condition()
        self.thread = threading.Thread(target=self.consume, args=(stream, log_path), daemon=True)
        self.thread.start()

    def consume(self, stream, path):
        with path.open("w", encoding="utf-8") as output:
            for line in stream:
                output.write(line)
                output.flush()
                if line.startswith(PREFIX):
                    row = json.loads(line[len(PREFIX):])
                    with self.condition:
                        self.rows.append(row)
                        self.condition.notify_all()

    def wait(self, predicate, timeout=15):
        with self.condition:
            if not self.condition.wait_for(lambda: any(predicate(row) for row in self.rows), timeout):
                raise RuntimeError("native probe boundary was not observed")


def sample(config, nodes, selected, args, workload, repeat):
    root = Path(args.mount.rstrip("\\") + "\\")
    if root.exists():
        raise RuntimeError("benchmark requires an unused drive letter")
    expected = manifest((n["name"], n["type"] == "folder", 0 if n["type"] == "folder" else n["size"])
                        for n in nodes)
    log_base = args.output.with_suffix(f".{workload}.{repeat}")
    with tempfile.TemporaryDirectory(prefix="ydfs-native-probe-") as temporary:
        mount_config = dict(config, mount=args.mount, read_only=True, meta_ttl=5,
                            log_level="INFO", cache_dir=temporary, network_drive=True,
                            network_name="native-probe-" + secrets.token_hex(6))
        mount_config.pop("log_file", None)
        cfg = Path(temporary) / "config.json"
        cfg.write_text(json.dumps(mount_config), encoding="utf-8")
        with Path(str(log_base) + ".stderr.log").open("w", encoding="utf-8") as error_log:
            process = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve()), "--child", "--config", str(cfg),
                 "--source-root", str(args.source_root)], cwd=args.source_root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log, text=True, bufsize=1)
            signals = Signals(process.stdout, Path(str(log_base) + ".stdout.log"))
            pool = ThreadPoolExecutor(max_workers=len(selected))
            try:
                deadline = time.monotonic() + 20
                while not root.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("native probe mount failed")
                    time.sleep(.05)

                def directory(reads=()):
                    start = time.perf_counter_ns()
                    with os.scandir(root) as entries:
                        entries = list(entries)
                    # DirEntry.stat on Windows uses the FindFirst/Next result.
                    rows = [(entry.name, entry.is_dir(), entry.stat().st_size) for entry in entries]
                    enumerated = time.perf_counter_ns()
                    before_reads = bool(reads) and all(not future.done() for future in reads)
                    # Separately measure fresh path queries on those same files.
                    refreshed, timings = [], []
                    by_name = {node["name"]: node for node in nodes}
                    probe_names = {node["name"] for node in selected}
                    for entry in entries:
                        query_start = time.perf_counter_ns()
                        attrs = os.stat(root / entry.name)
                        timings.append(dict(target=digest(by_name[entry.name]["fileId"]),
                                            probed_file=entry.name in probe_names,
                                            elapsed_ms=round((time.perf_counter_ns() - query_start) / 1e6, 3)))
                        refreshed.append((entry.name, stat.S_ISDIR(attrs.st_mode), attrs.st_size))
                    end = time.perf_counter_ns()
                    if manifest(rows) != expected or manifest(refreshed) != expected:
                        raise RuntimeError("native directory metadata differs from cloud metadata")
                    return dict(start=start, end=end, elapsed_ms=round((end - start) / 1e6, 3),
                                enumerate_ms=round((enumerated - start) / 1e6, 3),
                                refresh_ms=round((end - enumerated) / 1e6, 3),
                                enumerated_before_reads=before_reads, attributes=timings)

                cold = directory()
                warm = directory()
                process.stdin.write("measure\n")
                process.stdin.flush()
                signals.wait(lambda row: row["event"] == "phase_ready")
                barrier = threading.Barrier(len(selected) + 1)

                def header(node):
                    barrier.wait(timeout=10)
                    start = time.perf_counter_ns()
                    with open(root / node["name"], "rb", buffering=0) as stream:
                        data = stream.read(args.request_bytes)
                    if len(data) != args.request_bytes:
                        raise RuntimeError("short native header read")
                    return dict(target=digest(node["fileId"]), header_hash=hashlib.sha256(data).hexdigest(),
                                elapsed_ms=round((time.perf_counter_ns() - start) / 1e6, 3))

                futures = [pool.submit(header, node) for node in selected]
                barrier.wait(timeout=10)
                targets = {digest(node["fileId"]) for node in selected}
                signals.wait(lambda row: row["event"] == "download_begin" and row["phase"] == "measure"
                             and row["target"] in targets)
                query = directory(futures)
                begin, end, elapsed = query["start"], query["end"], query["elapsed_ms"]
                finished_before_reads = all(not future.done() for future in futures)
                headers = [future.result(timeout=60) for future in futures]
                signals.wait(lambda row: row["event"] == "perf" and row["phase"] == "measure"
                             and row["data"].get("operation") == "read")
                process.stdin.write("dump\n")
                process.stdin.flush()
                signals.wait(lambda row: row["event"] == "lock_metrics")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=15)
                pool.shutdown(wait=True, cancel_futures=True)
                signals.thread.join(timeout=5)
                process.stdin.close()
                process.stdout.close()
        deadline = time.monotonic() + 15
        while root.exists():
            if time.monotonic() > deadline:
                raise RuntimeError("native probe mount did not disconnect")
            time.sleep(.05)
        measured = [row for row in signals.rows if row["phase"] == "measure"]
        operations = [row["data"] for row in measured if row["event"] == "perf"
                      and row["data"].get("event") == "operation"]
        read_keys = {(row["pid"], row["trace"]) for row in measured if row["event"] == "download_begin"}
        reads = [op for op in operations if (op["pid"], op["trace"]) in read_keys and op["operation"] == "read"]
        directory_keys = {(row["pid"], row["trace"]) for row in measured if row["event"] == "directory_begin"
                          and begin <= row["mono_ns"] <= end}
        listings = [op for op in operations if (op["pid"], op["trace"]) in directory_keys]
        if not listings or not reads:
            raise RuntimeError("native read/directory callbacks were not captured")
        if any(op["counts"].get("http_pages", 0) for op in listings):
            raise RuntimeError("concurrent directory unexpectedly refreshed metadata")
        starts = {(row["pid"], row["trace"]): row["mono_ns"] for row in measured if row["event"] == "read_begin"}
        ends = {(row["pid"], row["trace"]): row["mono_ns"] for row in measured if row["event"] == "read_end"}
        overlap = sum(starts.get(key, begin + 1) <= begin < ends.get(key, 0) for key in read_keys)
        if not overlap:
            raise RuntimeError("native read did not overlap directory query")
        metrics = next(row["data"] for row in measured if row["event"] == "lock_metrics")
        query_metrics = [item for item in metrics if item["operation"] in {"getattr", "native_readdir"}
                         and begin <= item["start_ns"] <= end]
        metadata_metrics = [item for item in metrics if item["operation"] in {"getattr", "native_readdir"}]
        return dict(cold_ms=cold["elapsed_ms"], warm_ms=warm["elapsed_ms"], concurrent_directory_ms=elapsed,
                    cold_enumerate_ms=cold["enumerate_ms"], warm_enumerate_ms=warm["enumerate_ms"],
                    concurrent_enumerate_ms=query["enumerate_ms"], forced_attribute_refresh_ms=query["refresh_ms"],
                    max_probed_file_refresh_ms=max(t["elapsed_ms"] for t in query["attributes"] if t["probed_file"]),
                    max_other_file_refresh_ms=max(t["elapsed_ms"] for t in query["attributes"] if not t["probed_file"]),
                    enumeration_finished_before_all_reads=query["enumerated_before_reads"],
                    directory_lock_wait_ms=round(sum(item["wait_ms"] for item in query_metrics), 3),
                    max_metadata_lock_wait_ms=round(max((item["wait_ms"] for item in metadata_metrics), default=0), 3),
                    downloaded_bytes=sum(op["counts"].get("downloaded_bytes", 0) for op in reads),
                    fuse_requested_bytes=sum(op["counts"].get("requested_bytes", 0) for op in reads),
                    directory_finished_before_all_reads=finished_before_reads, overlapping_reads=overlap,
                    headers=headers, read_operations=reads, directory_operations=listings,
                    metadata_lock_metrics=query_metrics, attribute_refreshes=query["attributes"], manifest=expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mount", default="X:")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--workloads", nargs="+", choices=("single", "burst"), default=["single", "burst"])
    parser.add_argument("--request-bytes", type=int, default=32)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("this benchmark exercises Win32 and WinFsp")
    args.source_root = args.source_root.resolve()
    if args.child:
        child(args)
        return
    if args.output is None or args.repeats < 1 or not 1 <= args.request_bytes <= 65536:
        parser.error("output, positive repeats and 1..65536 requested bytes are required")
    if len(args.mount) != 2 or args.mount[0].upper() not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" or args.mount[1] != ":":
        parser.error("mount must be an unused Windows drive letter")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if urlsplit(config["base_url"]).scheme != "https" or not config.get("root_file_id"):
        parser.error("HTTPS and a dedicated root are required")
    sys.path.insert(0, str(args.source_root))
    from core.api.client import Client
    client = Client(config["base_url"], config.get("authorization") or os.environ.get("YDFS_AUTH"),
                    config.get("client_info", "ydfs-demo"), config.get("app_channel", "ydfs-demo"))
    nodes = client.list(config["root_file_id"])
    if any(n["name"] in {".", ".."} or any(c in n["name"] for c in "\\/:") for n in nodes):
        parser.error("cloud names must stay within the test mount")
    candidates = sorted((n for n in nodes if n["type"] == "file" and n["name"].lower().endswith(".exe")),
                        key=lambda n: (n["size"], n["fileId"]))
    required = 3 if "burst" in args.workloads else 1
    if len(candidates) < required:
        parser.error("dedicated root has too few existing EXEs for the requested workload")
    report = dict(measured_at=datetime.now(timezone.utc).isoformat(), read_only=True, interface="Win32/WinFsp",
                  meta_ttl=5, request_bytes=args.request_bytes, entries=len(nodes),
                  source_fingerprint_method="sha256_checked_before_and_after_each_sample",
                  source_sha256=source_hashes(args.source_root), workloads={})
    try:
        for workload in args.workloads:
            selected = candidates[:1 if workload == "single" else 3]
            samples = []
            for repeat in range(args.repeats):
                if source_hashes(args.source_root) != report["source_sha256"]:
                    raise RuntimeError("source changed during benchmark")
                samples.append(sample(config, nodes, selected, args, workload, repeat))
                if source_hashes(args.source_root) != report["source_sha256"]:
                    raise RuntimeError("source changed during benchmark")
            keys = ("cold_ms", "warm_ms", "concurrent_directory_ms", "directory_lock_wait_ms",
                    "max_metadata_lock_wait_ms", "downloaded_bytes", "fuse_requested_bytes",
                    "cold_enumerate_ms", "warm_enumerate_ms", "concurrent_enumerate_ms", "forced_attribute_refresh_ms",
                    "max_probed_file_refresh_ms", "max_other_file_refresh_ms")
            medians = {key: round(statistics.median(s[key] for s in samples), 3) for key in keys}
            report["workloads"][workload] = dict(samples=samples, median=medians)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(dict(workload=workload, repeats=args.repeats, median=medians)), flush=True)
    except Exception as error:
        print(json.dumps(dict(status="error", kind=type(error).__name__, code=getattr(error, "code", None))), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
