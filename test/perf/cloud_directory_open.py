"""Read-only directory timings against a configured real cloud and optional mount.

Uses the same paths and cloud metadata for both implementations. Credentials and
raw paths never appear in output. --source-root selects a separate baseline copy.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import posixpath
import secrets
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time


class Traces(logging.Handler):
    def __init__(self):
        super().__init__()
        self.latest = None

    def emit(self, record):
        message = record.getMessage()
        if "[directory-perf] " in message:
            row = json.loads(message.split("[directory-perf] ", 1)[1])
            if row.get("operation", "").startswith("cloud_"):
                self.latest = row


def manifest(rows):
    return hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=False).encode()).hexdigest()


def median(samples):
    return {key: round(statistics.median(sample[key] for sample in samples), 3)
            for key, value in samples[0].items() if isinstance(value, (int, float))}


def core_scenario(config, path, args, traces):
    from adapter.fusefs import FuseFilesystem
    from core.api.client import Client
    from core.fs import Filesystem
    from core.perf import traced

    def measure(operation, action):
        wrapped = traced(operation)(action)
        start = time.perf_counter()
        result = wrapped()
        return result, round((time.perf_counter() - start) * 1000, 3), traces.latest

    client = Client(config["base_url"], config.get("authorization") or os.environ.get("YDFS_AUTH"),
                    config.get("client_info", "ydfs-demo"), config.get("app_channel", "ydfs-demo"))
    original = client.post

    def only_list(api_path, **body):
        if api_path != "/file/list":
            raise RuntimeError("cloud benchmark permits only list requests")
        return original(api_path, **body)

    client.post = only_list
    result = dict(target=hashlib.sha256(path.encode()).hexdigest()[:12],
                  depth=0 if path == "/" else len(path.strip("/").split("/")),
                  page_size=client.page_size, core=[])
    with tempfile.TemporaryDirectory(prefix="ydfs-cloud-perf-") as cache_dir:
        fs = Filesystem(client, cache_dir, ignore_case=os.name == "nt", meta_ttl=args.meta_ttl,
                        root_id=config["root_file_id"])
        adapter = FuseFilesystem(fs, read_only=True)

        def names():
            return adapter.readdir(path, None)[2:]

        def attributes(children):
            rows = []
            for name in children:
                attrs = adapter.getattr(posixpath.join(path, name))
                rows.append((name, stat.S_ISDIR(attrs["st_mode"]), attrs["st_size"]))
            return rows

        def warm():
            return attributes(names())

        for _ in range(args.repeats):
            fs.cache.entries.clear()
            children, cold_ms, cold_trace = measure("cloud_cold_list", names)
            rows, attr_ms, attr_trace = measure("cloud_child_attributes", lambda: attributes(children))
            warm_rows, warm_ms, warm_trace = measure("cloud_warm_open", warm)
            if manifest(rows) != manifest(warm_rows):
                raise RuntimeError("cloud metadata changed during benchmark")
            if warm_trace["counts"].get("http_pages", 0):
                raise RuntimeError("warm open unexpectedly refreshed metadata")
            result["entries"] = len(children)
            result["manifest"] = manifest(rows)
            result["core"].append(dict(cold_list_ms=cold_ms, child_attributes_ms=attr_ms,
                                       cold_open_ms=round(cold_ms + attr_ms, 3), warm_open_ms=warm_ms,
                                       http_pages=cold_trace["counts"].get("http_pages", 0),
                                       stages=dict(cold=cold_trace, attributes=attr_trace, warm=warm_trace)))
        result["core_median"] = median(result["core"])
    return result


def mounted_scenario(config, path, result, args):
    root = Path(args.mount.rstrip("\\") + "\\" if os.name == "nt" else args.mount)
    if os.name == "nt" and root.exists():
        raise RuntimeError("benchmark requires an unused drive letter")
    relative = path.strip("/").split("/") if path != "/" else []
    directory = root.joinpath(*relative)
    samples = []
    with tempfile.TemporaryDirectory(prefix="ydfs-cloud-mount-") as temporary:
        mount_config = dict(config, mount=args.mount, read_only=True, meta_ttl=args.meta_ttl,
                            log_level="INFO", cache_dir=temporary)
        mount_config.pop("log_file", None)
        mount_config.pop("page_size", None)
        if mount_config.get("network_drive"):
            mount_config["network_name"] = "perf-" + secrets.token_hex(6)
        config_path = Path(temporary) / "config.json"
        config_path.write_text(json.dumps(mount_config), encoding="utf-8")
        native_log = args.output.with_suffix(f".{result['target']}.mount.log")
        with native_log.open("w", encoding="utf-8") as output:
            for repeat in range(args.mount_repeats):
                process = subprocess.Popen([sys.executable, "-m", "cmd.ydfs", "--config", str(config_path)],
                                           cwd=args.source_root, stdout=output, stderr=output)
                try:
                    def ready():
                        return root.exists() if os.name == "nt" else os.path.ismount(root)

                    deadline = time.monotonic() + 20
                    while not ready():
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("cloud mount failed; inspect its timing log")
                        time.sleep(.05)

                    def enumerate_and_stat():
                        with os.scandir(directory) as children:
                            rows = [(entry.name, entry.is_dir(), entry.stat().st_size) for entry in children]
                        if manifest(rows) != result["manifest"]:
                            raise RuntimeError("mounted metadata differs from cloud metadata")

                    def timed(action):
                        start = time.perf_counter()
                        action()
                        return round((time.perf_counter() - start) * 1000, 3)

                    sample = dict(cold_open_ms=timed(enumerate_and_stat),
                                  warm_open_ms=timed(enumerate_and_stat),
                                  volume_space_ms=timed(lambda: shutil.disk_usage(root)))
                    if args.expiry_check and repeat == 0:
                        time.sleep(args.meta_ttl + .2)
                        result["expired_open_ms"] = timed(enumerate_and_stat)
                        result["after_expired_warm_ms"] = timed(enumerate_and_stat)
                    samples.append(sample)
                finally:
                    if os.name == "nt":
                        process.terminate()
                    else:
                        subprocess.run(["fusermount3", "-u", args.mount], check=True)
                    process.wait(timeout=15)
                deadline = time.monotonic() + 15
                while ready():
                    if time.monotonic() > deadline:
                        raise RuntimeError("benchmark mount did not disconnect")
                    time.sleep(.05)
    result["mounted"] = samples
    result["mounted_median"] = median(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--targets-file", type=Path, help="Private JSON array of paths within configured root")
    parser.add_argument("--path", default="/")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--meta-ttl", type=float, default=5)
    parser.add_argument("--mount", help="Unused test drive, separate from the existing mount")
    parser.add_argument("--mount-repeats", type=int, default=3)
    parser.add_argument("--expiry-check", action="store_true")
    args = parser.parse_args()
    args.source_root = args.source_root.resolve()
    args.output = args.output.resolve()
    sys.path.insert(0, str(args.source_root))
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    from urllib.parse import urlsplit
    if urlsplit(config["base_url"]).scheme != "https" or not config.get("root_file_id"):
        parser.error("real-cloud test requires HTTPS and a dedicated root ID")
    if args.repeats < 1 or args.mount_repeats < 1 or args.meta_ttl <= 0:
        parser.error("repeats and TTL must be positive")
    paths = json.loads(args.targets_file.read_text(encoding="utf-8")) if args.targets_file else [args.path]
    if not isinstance(paths, list) or not paths or any(
            not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/") for path in paths):
        parser.error("targets must be paths within the configured mount root")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.output.with_suffix(".log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    traces = Traces()
    logging.getLogger("ydfs.perf").addHandler(traces)
    report = dict(measured_at=datetime.now(timezone.utc).isoformat(), platform=sys.platform,
                  python=sys.version.split()[0], meta_ttl=args.meta_ttl, repeats=args.repeats,
                  mount_repeats=args.mount_repeats if args.mount else 0, read_only=True, scenarios=[])
    try:
        for path in paths:
            result = core_scenario(config, path, args, traces)
            if args.mount:
                mounted_scenario(config, path, result, args)
            report["scenarios"].append(result)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({key: result[key] for key in
                             ("target", "depth", "entries", "page_size", "core_median", "mounted_median",
                              "expired_open_ms", "after_expired_warm_ms") if key in result}), flush=True)
    except Exception as error:
        # Do not print arbitrary exception messages: transports may contain URLs.
        print(json.dumps(dict(status="error", kind=type(error).__name__,
                              code=getattr(error, "code", None))), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
