import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

env = dict(os.environ, YDFS_AUTH=secrets.token_hex(16))
parser = argparse.ArgumentParser(description="Run the local mock suite and check protocol evidence.")
parser.add_argument("mount")
drive = parser.parse_args().mount
root = Path(drive.rstrip("\\") + "\\" if os.name == "nt" else drive)
if os.name == "nt" and root.exists():
    parser.error("the drive letter is already in use")
processes = []
suite = "ydfs-e2e-" + secrets.token_hex(6)

def wait_until(test):
    for _ in range(100):
        try:
            if test():
                return
        except OSError:
            pass
        time.sleep(.1)
    raise RuntimeError("Timed out")

def state():
    with urlopen(url + "/_state", timeout=2) as response:
        return json.load(response)

def stop(process):
    if os.name == "nt" or process is mock:
        process.terminate()
    else:
        subprocess.run(["fusermount", "-u", drive], check=True)

def mount():
    process = subprocess.Popen([sys.executable, "-m", "cmd.ydfs", "--mount", drive,
                                "--base-url", url], env=env)
    processes.append(process)
    wait_until(lambda: os.path.ismount(root) if os.name != "nt" else root.is_dir())
    return process

def e2e(*args):
    return subprocess.run([sys.executable, "test/e2e/run.py", str(root), "--suite", suite, *args],
                          check=False).returncode

mock = None
try:
    mock = subprocess.Popen([sys.executable, "-m", "mock.server", "--port", "0", "--ttl", "3"],
                            env=env, stdout=subprocess.PIPE, text=True)
    processes.append(mock)
    url = mock.stdout.readline().strip()
    state()
    first = mount()
    if os.name == "nt":
        executable = root / "whoami.exe"
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe", executable)
        result = subprocess.run([str(executable)], capture_output=True, check=True, timeout=15)
        assert result.stdout.strip()
        executable.unlink()
        print("Windows executable launched from the mounted drive", flush=True)
    first_code = e2e("--wait-seconds", "5")
    before = state()
    stop(first)
    first.wait(timeout=10)
    wait_until(lambda: not (root.exists() if os.name == "nt" else os.path.ismount(root)))
    mount()
    second_code = e2e("--case", "T4")
    after = state()
    folder = next(f for f in after["files"] if f["name"] == suite)
    cases = {f["name"]: f["fileId"] for f in after["files"] if f["parentFileId"] == folder["fileId"]}
    by_case = {case: [f for f in after["files"] if f["parentFileId"] == file_id]
               for case, file_id in cases.items()}
    events = after["events"]

    def commits(case):
        file_id = by_case[case][0]["fileId"]
        return [e for e in events if e["operation"] == "complete" and e["fileId"] == file_id]

    def evidence():
        t1 = by_case["T1"][0]
        assert t1["name"] == "a"
        assert any(f["name"] == "b" and f["parentFileId"] == t1["fileId"] for f in after["files"])
        assert commits("T3")[0]["parts"] == [10 * 1048576, 10 * 1048576, 3 * 1048576]
        ranges = [e for e in events[len(before["events"]):] if e["operation"] == "range"]
        assert [(e["start"], e["end"]) for e in ranges] == [(8 * 1048576, 12 * 1048576 - 1)]
        revisions = commits("T5")
        assert len(revisions) == 2 and revisions[0]["revisionId"] != revisions[1]["revisionId"]
        assert any(f["name"] == "target" and f["parentFileId"] == cases["T8"] for f in after["trash"])
        nested = next(f for f in after["trash"] if f["name"] == "nested" and f["parentFileId"] == cases["T9"])
        assert any(f["name"] == "item" and f["parentFileId"] == nested["fileId"] for f in after["trash"])
        pages = {e["cursor"] for e in events if e["operation"] == "list" and e["parentFileId"] == cases["T10"]}
        assert {"0", "100", "200"} <= pages
        file_id = by_case["T11"][0]["fileId"]
        sequence = [e["operation"] for e in events if e.get("fileId") == file_id]
        assert sequence == ["complete", "download-url", "range", "download-url", "range"], sequence
        print("Server checks passed for T1 T3 T4 T5 T8 T9 T10 T11", flush=True)

    if not first_code and not second_code:
        evidence()
    cleanup_code = e2e("--cleanup")
    raise SystemExit(first_code or second_code or cleanup_code)
finally:
    for process in reversed(processes):
        if process.poll() is None:
            stop(process)
            process.wait(timeout=10)
