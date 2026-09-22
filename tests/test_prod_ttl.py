#!/usr/bin/env python3
"""P0.3 — TTL/EXPIRE correctness: lazy + active expire, under maxmemory pressure."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26903"))


def info_map(sock: socket.socket) -> dict[str, str]:
    raw = redis_call(sock, "INFO")
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def start_server(maxmemory: int = 0, evict: str = "lru") -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    bin_path = ROOT / "native/build/aura_redis_server"
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-ttl-{PORT}.log")
    args = [str(bin_path), "--port", str(PORT), "--evict", evict]
    if maxmemory:
        args += ["--maxmemory", str(maxmemory)]
    proc = subprocess.Popen(
        args, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env
    )
    for _ in range(50):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def test_lazy_expire(sock: socket.socket) -> None:
    redis_call(sock, "FLUSHDB")
    assert redis_call(sock, "SET", "lazy", "v", "EX", "1") == "OK"
    ttl = redis_call(sock, "TTL", "lazy")
    assert ttl in (0, 1), ttl
    assert redis_call(sock, "GET", "lazy") == "v"
    time.sleep(1.15)
    assert redis_call(sock, "GET", "lazy") is None
    m = info_map(sock)
    assert int(m["expired"]) >= 1, m
    assert redis_call(sock, "TTL", "lazy") == -2
    print("lazy expire OK")


def test_expire_cmd(sock: socket.socket) -> None:
    redis_call(sock, "FLUSHDB")
    redis_call(sock, "SET", "e", "1")
    assert redis_call(sock, "TTL", "e") == -1
    assert redis_call(sock, "EXPIRE", "e", "1") == 1
    assert redis_call(sock, "EXPIRE", "missing", "1") == 0
    time.sleep(1.15)
    assert redis_call(sock, "EXISTS", "e") == 0
    print("EXPIRE cmd OK")


def test_active_expire(sock: socket.socket) -> None:
    """Keys should disappear via active expire without per-key GET."""
    redis_call(sock, "FLUSHDB")
    n = 80
    for i in range(n):
        redis_call(sock, "SET", f"a{i}", "v", "EX", "1")
    m0 = info_map(sock)
    assert int(m0["keys"]) == n, m0
    time.sleep(1.2)
    # Pump server event loop (idle serve_once + active expire)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        redis_call(sock, "PING")
        time.sleep(0.05)
        left = sum(1 for i in range(n) if redis_call(sock, "EXISTS", f"a{i}") == 1)
        if left == 0:
            break
    left = sum(1 for i in range(n) if redis_call(sock, "EXISTS", f"a{i}") == 1)
    m = info_map(sock)
    print(f"active expire: left={left} expired={m.get('expired')} keys={m.get('keys')}")
    assert left == 0, f"active/lazy should clear all, left={left}"
    assert int(m["expired"]) >= n // 2, m  # most via expire paths
    print("active expire OK")


def test_ttl_under_memory_pressure(sock: socket.socket) -> None:
    redis_call(sock, "FLUSHDB")
    # Permanent ballast + short-TTL keys; after TTL wave, memory must drop
    # and expired values must never be returned.
    for i in range(100):
        redis_call(sock, "SET", f"perm{i}", "p" * 400)
    for i in range(100):
        redis_call(sock, "SET", f"tmp{i}", "t" * 400, "EX", "1")
    m1 = info_map(sock)
    used1 = int(m1["used_memory"])
    time.sleep(1.2)
    for _ in range(40):
        redis_call(sock, "PING")
        time.sleep(0.05)
    for i in range(100):
        assert redis_call(sock, "GET", f"tmp{i}") is None, f"stale tmp{i}"
    # permanent keys should mostly survive (not guaranteed all under LRU)
    perm = sum(1 for i in range(100) if redis_call(sock, "EXISTS", f"perm{i}") == 1)
    m2 = info_map(sock)
    used2 = int(m2["used_memory"])
    print(f"pressure: used {used1}→{used2} perm_left={perm} expired={m2['expired']} evicted={m2['evicted']}")
    assert used2 < used1, "expected used_memory drop after TTL wave"
    assert int(m2["expired"]) > 0
    assert perm > 0
    assert used2 <= int(m2["maxmemory"]) + 4096
    print("TTL under pressure OK")


def main() -> int:
    proc = start_server(maxmemory=0)
    try:
        s = socket.create_connection(("127.0.0.1", PORT), 5)
        test_lazy_expire(s)
        test_expire_cmd(s)
        test_active_expire(s)
        s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    proc = start_server(maxmemory=120_000, evict="lru")
    try:
        s = socket.create_connection(("127.0.0.1", PORT), 5)
        test_ttl_under_memory_pressure(s)
        s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("test_prod_ttl PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
