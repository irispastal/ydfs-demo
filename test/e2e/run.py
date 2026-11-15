"""Exercise the mount through ordinary filesystem calls."""
import argparse
import errno
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

MIB = 1024 * 1024
PAYLOAD = bytes(range(251)) * (23 * MIB // 251) + bytes(range((23 * MIB) % 251))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require_error(action, expected):
    try:
        action()
    except OSError as error:
        if error.errno in expected or (os.name == "nt" and error.winerror == 145
                                       and errno.ENOTEMPTY in expected):
            return
        raise
    raise AssertionError("operation unexpectedly succeeded")


def run(root, case, wait):
    if case == "T4":
        with (root / "T3" / "large.bin").open("rb", buffering=0) as file:
            file.seek(9 * MIB)
            start = time.perf_counter()
            data = file.read(MIB)
            elapsed = time.perf_counter() - start
        assert data == PAYLOAD[9 * MIB:10 * MIB]
        return {"first_read_seconds": elapsed}
    folder = root / case
    folder.mkdir()
    file = folder / "sample.bin"
    if case == "T1":
        (folder / "a").mkdir()
        (folder / "a" / "b").mkdir()
        assert sorted(p.name for p in folder.iterdir()) == ["a"]
        assert sorted(p.name for p in (folder / "a").iterdir()) == ["b"]
    elif case == "T2":
        data = PAYLOAD[:1024]
        file.write_bytes(data)
        assert digest(file.read_bytes()) == digest(data)
    elif case == "T3":
        file = folder / "large.bin"
        start = time.perf_counter()
        file.write_bytes(PAYLOAD)
        elapsed = time.perf_counter() - start
        assert digest(file.read_bytes()) == digest(PAYLOAD)
        return {"write_close_seconds": elapsed}
    elif case == "T5":
        file.write_bytes(b"original")
        with file.open("ab") as stream:
            stream.write(b"-appended")
        assert file.read_bytes() == b"original-appended"
    elif case == "T6":
        file.touch()
        assert file.is_file() and file.stat().st_size == 0
    elif case == "T7":
        file.write_bytes(b"move")
        changed = file.rename(folder / "renamed")
        assert not file.exists() and changed.read_bytes() == b"move"
        destination = folder / "nested"
        destination.mkdir()
        moved = changed.rename(destination / "renamed")
        assert not changed.exists() and moved.read_bytes() == b"move"
    elif case == "T8":
        file.write_bytes(b"source")
        target = folder / "target"
        target.write_bytes(b"target")
        file.replace(target)
        assert not file.exists() and target.read_bytes() == b"source"
    elif case == "T9":
        nested = folder / "nested"
        nested.mkdir()
        item = nested / "item"
        item.write_bytes(b"remove")
        require_error(nested.rmdir, {errno.ENOTEMPTY, errno.EEXIST})
        item.unlink()
        assert not item.exists()
        nested.rmdir()
        assert not nested.exists()
    elif case == "T10":
        for index in range(250):
            (folder / f"item-{index:03}").touch()
        assert sorted(item.name for item in folder.iterdir()) == [f"item-{i:03}" for i in range(250)]
    elif case == "T11":
        file.write_bytes(PAYLOAD)
        with file.open("rb", buffering=0) as stream:
            assert stream.read(1024) == PAYLOAD[:1024]
            time.sleep(wait)
            stream.seek(8 * MIB)
            assert stream.read(1024) == PAYLOAD[8 * MIB:8 * MIB + 1024]
    elif case == "T12":
        if os.name == "nt":
            file.write_bytes(b"case")
            assert (folder / "SAMPLE.BIN").read_bytes() == b"case"
        else:
            require_error(lambda: (folder / "invalid:name").touch(), {errno.EINVAL})
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mount", type=Path)
    parser.add_argument("--suite", default=f"ydfs-e2e-{uuid4().hex}")
    parser.add_argument("--case", action="append", choices=[f"T{i}" for i in range(1, 14)])
    parser.add_argument("--wait-seconds", type=float, default=905)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if not args.suite.startswith("ydfs-e2e-") or not all(
            character.isascii() and (character.isalnum() or character == "-") for character in args.suite):
        parser.error("invalid suite directory")
    root = args.mount / args.suite
    if args.cleanup:
        import shutil
        shutil.rmtree(root)
        return
    root.mkdir(exist_ok=True)
    cases = args.case or [f"T{i}" for i in range(1, 14) if i != 4]
    evidence = {"T1", "T3", "T4", "T5", "T8", "T9", "T10", "T11"}
    failed = False
    print(json.dumps({"suite": args.suite}), flush=True)
    for case in cases:
        if case == "T13":
            print(json.dumps({"case": case, "status": "manual"}), flush=True)
            continue
        try:
            metrics = run(root, case, args.wait_seconds)
            status = "needs-server-evidence" if case in evidence else "pass"
            print(json.dumps({"case": case, "status": status, **metrics}), flush=True)
        except Exception as error:
            failed = True
            print(json.dumps({"case": case, "status": "fail", "error": repr(error)}), flush=True)
    raise SystemExit(int(failed))


if __name__ == "__main__":
    main()
