"""Compare upload preparation with full-file scanning and incremental hashing.

Run from any directory. Uses the real Filesystem/Client path and local temporary
files; only /file/create is replaced with a rapid-upload response. No network or
credentials are used. Total time includes writing and digest computation.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.api.client import Client, MIB
from core.fs import Filesystem


def sample(directory, size, chunk, incremental, expected):
    client = Client("http://unused", "test", "test", "test")
    fs = Filesystem(client, directory)
    # The baseline restores the original, untracked TemporaryFile staging.
    factory = (patch("core.fs.StagedFile", side_effect=lambda directory, algorithm:
                     tempfile.TemporaryFile(dir=directory)) if not incremental
               else nullcontext())
    with factory, patch.object(client, "list", return_value=[]):
        handle = fs.create("/large.bin")
    stream = fs.handle(handle).stream
    try:
        start = time.perf_counter()
        for offset in range(0, size, len(chunk)):
            fs.write(handle, offset, chunk[:min(len(chunk), size - offset)])
        written = time.perf_counter()
        created_at = None

        def create(path, **body):
            nonlocal created_at
            created_at = time.perf_counter()
            assert path == "/file/create"
            assert body["size"] == size and body["contentHash"] == expected
            return dict(fileId="uploaded", fileName="large.bin", rapidUpload=True)

        with patch.object(client, "post", side_effect=create):
            with patch.object(stream, "read", wraps=stream.read) as read:
                fs.flush(handle)
            reads = read.call_count
        return dict(write_ms=round((written - start) * 1000, 3),
                    preparation_ms=round((created_at - written) * 1000, 3),
                    total_to_create_ms=round((created_at - start) * 1000, 3),
                    preparation_reads=reads)
    finally:
        stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=256)
    parser.add_argument("--chunk-kib", type=int, default=1024)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if min(args.size_mib, args.chunk_kib, args.runs) < 1:
        parser.error("size, chunk and runs must be positive")
    size = args.size_mib * MIB
    chunk = b"x" * (args.chunk_kib * 1024)
    digest = hashlib.sha256()
    for offset in range(0, size, len(chunk)):
        digest.update(chunk[:min(len(chunk), size - offset)])
    samples = {"scan": [], "incremental": []}
    with tempfile.TemporaryDirectory(prefix="ydfs-upload-perf-") as directory:
        for _ in range(args.runs):
            for name in samples:
                samples[name].append(sample(directory, size, chunk, name == "incremental",
                                            digest.hexdigest()))
    medians = {name: {field: round(statistics.median(row[field] for row in rows), 3)
                      for field in rows[0]} for name, rows in samples.items()}
    print(json.dumps(dict(size_mib=args.size_mib, chunk_kib=args.chunk_kib, runs=args.runs,
                          medians=medians, samples=samples), indent=2))


if __name__ == "__main__":
    main()
