#!/usr/bin/env python3
"""P1.10 — command latency histogram in INFO + slowlog threshold."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26910"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"


def start() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call([str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-slowlog-{PORT}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(PORT), "--evict", "noop", "--maxmemory", "200000"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(80):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    kill_tcp_port(PORT)


def info_map(sock: socket.socket) -> dict[str, str]:
    info = redis_call(sock, "INFO")
    assert isinstance(info, str)
    out = {}
    for line in info.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k] = v
    return out


def main() -> int:
    proc = start()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "CONFIG", "GET", "slowlog-log-slower-than") == [
                "slowlog-log-slower-than",
                "10000",
            ]
            # Make threshold very low so normal cmds count as slow
            assert redis_call(sock, "CONFIG", "SET", "slowlog-log-slower-than", "0") == "OK"
            for i in range(50):
                redis_call(sock, "SET", f"k{i}", "v" * 8)
                redis_call(sock, "GET", f"k{i}")
            m = info_map(sock)
            for k in (
                "cmd_lt_1ms",
                "cmd_lt_10ms",
                "cmd_lt_100ms",
                "cmd_ge_100ms",
                "cmd_avg_us",
                "slowlog_count",
                "slowlog_log_slower_than",
            ):
                assert k in m, f"missing {k} in {sorted(m)}"
            samples = (
                int(m["cmd_lt_1ms"])
                + int(m["cmd_lt_10ms"])
                + int(m["cmd_lt_100ms"])
                + int(m["cmd_ge_100ms"])
            )
            assert samples >= 100, samples
            assert int(m["slowlog_count"]) >= 50, m["slowlog_count"]
            assert m["slowlog_log_slower_than"] == "0"
            # Raise threshold again — still readable
            assert (
                redis_call(sock, "CONFIG", "SET", "slowlog-log-slower-than", "50000")
                == "OK"
            )
            assert redis_call(sock, "CONFIG", "GET", "slowlog-log-slower-than") == [
                "slowlog-log-slower-than",
                "50000",
            ]
        print("PASS latency histogram + slowlog threshold")
        print("test_prod_slowlog: ALL PASSED")
        return 0
    finally:
        stop(proc)


if __name__ == "__main__":
    raise SystemExit(main())
