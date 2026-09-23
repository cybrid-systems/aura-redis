#!/usr/bin/env python3
"""Tier-2 replica honesty: no auto-reconnect; manual re-REPLICAOF after master death."""
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

PORT_M = int(os.environ.get("AURA_REDIS_TEST_PORT", "27010"))
PORT_R = PORT_M + 1
BIN = ROOT / "native/build/aura_redis_server"


def build() -> None:
    if BIN.exists() and os.environ.get("AURA_REDIS_SKIP_BUILD") == "1":
        return
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def start(port: int, tag: str) -> tuple[subprocess.Popen, Path]:
    kill_tcp_port(port)
    time.sleep(0.05)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-replica-fo-{tag}-{port}.log")
    proc = subprocess.Popen(
        [str(BIN), "--port", str(port), "--evict", "lru"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(100):
        if "listening on" in log.read_text(errors="replace"):
            return proc, log
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def wait_get(port: int, key: str, expect: str, timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                last = redis_call(sock, "GET", key)
                if last == expect:
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"GET {key} on :{port} got {last!r}, want {expect!r}")


def main() -> int:
    build()
    pm, logm = start(PORT_M, "master")
    pr, logr = start(PORT_R, "replica")
    try:
        with socket.create_connection(("127.0.0.1", PORT_M), timeout=5) as sock:
            assert redis_call(sock, "SET", "k", "v1") == "OK"
        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as sock:
            assert redis_call(sock, "REPLICAOF", "127.0.0.1", str(PORT_M)) == "OK"
        wait_get(PORT_R, "k", "v1")

        # Kill master — replica must NOT auto-promote / auto-reconnect
        stop(pm)
        pm = None
        time.sleep(0.4)
        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as sock:
            assert redis_call(sock, "GET", "k") == "v1"
            try:
                redis_call(sock, "SET", "nope", "x")
                raise AssertionError("expected READONLY after master death")
            except RuntimeError as e:
                assert "READONLY" in str(e), e

        # New master on same port; replica stays detached until re-REPLICAOF
        pm, logm = start(PORT_M, "master2")
        with socket.create_connection(("127.0.0.1", PORT_M), timeout=5) as sock:
            assert redis_call(sock, "SET", "k", "v2") == "OK"
            assert redis_call(sock, "SET", "fresh", "1") == "OK"
        time.sleep(0.3)
        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as sock:
            # Still old data — no auto-reconnect
            assert redis_call(sock, "GET", "k") == "v1"
            assert redis_call(sock, "GET", "fresh") is None
            assert redis_call(sock, "REPLICAOF", "127.0.0.1", str(PORT_M)) == "OK"
        wait_get(PORT_R, "k", "v2")
        wait_get(PORT_R, "fresh", "1")

        print("PASS replica fail-closed + manual re-REPLICAOF")
        print("test_prod_replica_failover: ALL PASSED")
        return 0
    finally:
        stop(pr)
        if pm is not None:
            stop(pm)
        kill_tcp_port(PORT_M)
        kill_tcp_port(PORT_R)


if __name__ == "__main__":
    raise SystemExit(main())
