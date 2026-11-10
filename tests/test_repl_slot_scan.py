#!/usr/bin/env python3
"""Regression: repl_propagate must not walk connection slots when no replica exists.

INFO repl_slot_scans counts slots examined inside the propagate loop. With zero
replicas, SETs leave it at 0. If the empty scan returns, the counter climbs by
AR_MAX_CONN per write. A live replica must still receive the write and the
counter must move.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

BIN = ROOT / "native/build/aura_redis_server"
PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26971"))
PORT_R = PORT + 1


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in str(raw).splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def start(port: int, tag: str) -> tuple[subprocess.Popen, Path]:
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-repl-scan-{tag}-{port}.log")
    proc = subprocess.Popen(
        [str(BIN), "--port", str(port), "--bind", "127.0.0.1", "--evict", "lru"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(80):
        if "listening on" in log.read_text(errors="replace"):
            return proc, log
        if proc.poll() is not None:
            raise RuntimeError(log.read_text(errors="replace"))
        time.sleep(0.05)
    raise TimeoutError(log.read_text(errors="replace"))


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_no_replica_skips_scan() -> None:
    proc, _ = start(PORT, "master")
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
            before = int(info_map(redis_call(s, "INFO")).get("repl_slot_scans", "-1"))
            for i in range(32):
                redis_call(s, "SET", f"k{i}", "v" * 32)
                assert redis_call(s, "GET", f"k{i}") == "v" * 32
            after = int(info_map(redis_call(s, "INFO")).get("repl_slot_scans", "-1"))
            slaves = int(info_map(redis_call(s, "INFO")).get("connected_slaves", "-1"))
            redis_call(s, "QUIT")
        assert before == 0, before
        assert after == 0, after
        assert slaves == 0, slaves
        print("PASS repl_slot_scans stays 0 with no replica")
    finally:
        stop(proc)


def test_replica_still_receives() -> None:
    master, _ = start(PORT, "m2")
    replica, _ = start(PORT_R, "r2")
    try:
        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as r:
            redis_call(r, "REPLICAOF", "127.0.0.1", str(PORT))
        time.sleep(0.3)
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as m:
            redis_call(m, "SET", "replkey", "from-master")
            info = info_map(redis_call(m, "INFO"))
            redis_call(m, "QUIT")
        assert int(info.get("connected_slaves", "0")) >= 1, info
        assert int(info.get("repl_slot_scans", "0")) > 0, info
        deadline = time.time() + 3
        got = None
        while time.time() < deadline:
            with socket.create_connection(("127.0.0.1", PORT_R), timeout=2) as r:
                got = redis_call(r, "GET", "replkey")
                redis_call(r, "QUIT")
            if got == "from-master":
                break
            time.sleep(0.05)
        assert got == "from-master", got
        print(
            f"PASS replica received write scans={info.get('repl_slot_scans')} "
            f"slaves={info.get('connected_slaves')}"
        )
    finally:
        stop(replica)
        stop(master)


if __name__ == "__main__":
    test_no_replica_skips_scan()
    test_replica_still_receives()
    print("PASS repl slot scan")
