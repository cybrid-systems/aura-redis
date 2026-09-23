#!/usr/bin/env python3
"""P2.14 — REPLICAOF async replica: converges under SET; read-only writes."""
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

PORT_M = int(os.environ.get("AURA_REDIS_TEST_PORT", "26914"))
PORT_R = PORT_M + 1
BIN = ROOT / "native/build/aura_redis_server"


def build() -> None:
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
    log = Path(f"/tmp/test-prod-replica-{tag}-{port}.log")
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


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


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
        # Seed master before replica attaches
        with socket.create_connection(("127.0.0.1", PORT_M), timeout=5) as sock:
            assert redis_call(sock, "SET", "pre", "seed") == "OK"
            assert redis_call(sock, "SET", "ttlkey", "tv", "EX", "60") == "OK"

        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as sock:
            assert redis_call(sock, "REPLICAOF", "127.0.0.1", str(PORT_M)) == "OK"
            inf = info_map(redis_call(sock, "INFO"))
            assert inf.get("role") == "slave", inf
            assert inf.get("master_port") == str(PORT_M), inf

        # Full sync should bring pre-existing keys
        wait_get(PORT_R, "pre", "seed")
        wait_get(PORT_R, "ttlkey", "tv")
        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as sock:
            ttl = redis_call(sock, "TTL", "ttlkey")
            assert isinstance(ttl, int) and 1 <= ttl <= 60, ttl

        # Live SET stream converges
        with socket.create_connection(("127.0.0.1", PORT_M), timeout=5) as sock:
            for i in range(20):
                assert redis_call(sock, "SET", f"k{i}", f"v{i}") == "OK"
            assert redis_call(sock, "SET", "hot", "live") == "OK"
            assert redis_call(sock, "EXPIRE", "hot", "30") == 1
            assert redis_call(sock, "DEL", "pre") == 1

        wait_get(PORT_R, "hot", "live")
        wait_get(PORT_R, "k19", "v19")
        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as sock:
            assert redis_call(sock, "GET", "pre") is None
            # read-only
            try:
                redis_call(sock, "SET", "nope", "x")
                raise AssertionError("replica SET should fail READONLY")
            except RuntimeError as e:
                assert "READONLY" in str(e), e
            # GET still works
            assert redis_call(sock, "GET", "hot") == "live"
            # policy_agent flat keys intact
            inf = info_map(redis_call(sock, "INFO"))
            for k in ("gets", "sets", "hits", "misses", "evict", "layout", "keys"):
                assert k in inf, k

        with socket.create_connection(("127.0.0.1", PORT_M), timeout=5) as sock:
            inf = info_map(redis_call(sock, "INFO"))
            assert inf.get("role") == "master"
            assert int(inf.get("connected_slaves", "0")) >= 1

        # REPLICAOF NO ONE restores writes
        with socket.create_connection(("127.0.0.1", PORT_R), timeout=5) as sock:
            assert redis_call(sock, "REPLICAOF", "NO", "ONE") == "OK"
            assert redis_call(sock, "SET", "solo", "1") == "OK"
            assert redis_call(sock, "GET", "solo") == "1"

        print("test_prod_replica: ALL PASSED")
        return 0
    finally:
        stop(pr)
        stop(pm)
        print("--- master log ---")
        print(logm.read_text(errors="replace")[-800:])
        print("--- replica log ---")
        print(logr.read_text(errors="replace")[-800:])
        kill_tcp_port(PORT_M)
        kill_tcp_port(PORT_R)


if __name__ == "__main__":
    raise SystemExit(main())
