#!/usr/bin/env python3
"""P0.2 — used_memory accounting + maxmemory invariant under eviction flood."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26902"))
MAXMEM = 200_000
# Slack: one oversized write may briefly sit at ≈ maxmemory + one entry before
# further SETs; after flood settles we require strict ≤ maxmemory + SLACK.
SLACK = 4096


def info_map(sock: socket.socket) -> dict[str, str]:
    raw = redis_call(sock, "INFO")
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def start_server(maxmemory: int = MAXMEM, evict: str = "lru") -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    bin_path = ROOT / "native/build/aura_redis_server"
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-memory-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(bin_path),
            "--port",
            str(PORT),
            "--evict",
            evict,
            "--maxmemory",
            str(maxmemory),
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(50):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def test_accounting_delta(sock: socket.socket) -> None:
    redis_call(sock, "FLUSHDB")
    m0 = info_map(sock)
    assert int(m0["used_memory"]) == 0, m0
    assert int(m0["keys"]) == 0, m0
    val = "v" * 100
    redis_call(sock, "SET", "acct", val)
    m1 = info_map(sock)
    used = int(m1["used_memory"])
    # key + val + sizeof(ArEntry) — ArEntry is > 32 bytes on 64-bit; allow range
    expect_min = len("acct") + len(val) + 32
    expect_max = len("acct") + len(val) + 256
    assert expect_min <= used <= expect_max, (used, expect_min, expect_max)
    redis_call(sock, "DEL", "acct")
    m2 = info_map(sock)
    assert int(m2["used_memory"]) == 0, m2
    print(f"accounting delta OK used_after_set={used}")


def test_maxmemory_bound(sock: socket.socket, label: str) -> None:
    redis_call(sock, "FLUSHDB")
    val = "x" * 800
    n = 800  # 800 * ~800B >> MAXMEM
    for i in range(n):
        redis_call(sock, "SET", f"k{i}", val)
    m = info_map(sock)
    used = int(m["used_memory"])
    maxm = int(m["maxmemory"])
    evicted = int(m["evicted"])
    keys = int(m["keys"])
    print(f"{label}: used={used} max={maxm} keys={keys} evicted={evicted}")
    assert maxm == MAXMEM, maxm
    assert evicted > 0, "expected eviction under flood"
    assert keys < n, "expected fewer survivors than SETs"
    assert used <= maxm + SLACK, f"used_memory {used} > maxmemory+slack {maxm}+{SLACK}"
    # Further SETs must not ratchet unbounded
    for i in range(n, n + 200):
        redis_call(sock, "SET", f"k{i}", val)
    m2 = info_map(sock)
    used2 = int(m2["used_memory"])
    print(f"{label} after more flood: used={used2} evicted={m2['evicted']}")
    assert used2 <= maxm + SLACK, f"unbounded growth used={used2}"
    assert int(m2["evicted"]) >= evicted



def test_large_set_many_evictions(sock: socket.socket) -> None:
    """One large SET must reclaim >>64 tiny victims (P0.2: old guard=64 undershot)."""
    redis_call(sock, "FLUSHDB")
    # Pack maxmemory with tiny values so one 50KiB SET needs hundreds of evictions.
    for i in range(5000):
        redis_call(sock, "SET", f"t{i}", "x")
    m0 = info_map(sock)
    keys0 = int(m0["keys"])
    assert keys0 > 200, m0
    assert int(m0["used_memory"]) <= MAXMEM + SLACK, m0
    ev_before = int(m0["evicted"])
    big = "B" * 50_000
    assert redis_call(sock, "SET", "huge", big) == "OK"
    m = info_map(sock)
    used = int(m["used_memory"])
    maxm = int(m["maxmemory"])
    delta = int(m["evicted"]) - ev_before
    print(
        f"large SET: used={used} max={maxm} keys={m['keys']} "
        f"(was {keys0}) evict_delta={delta}"
    )
    assert used <= maxm + SLACK, f"large SET broke maxmemory used={used}"
    assert redis_call(sock, "GET", "huge") == big
    assert delta > 64, f"expected >64 evictions in one SET, got delta={delta}"


def main() -> int:
    proc = start_server()
    try:
        s = socket.create_connection(("127.0.0.1", PORT), 5)
        test_accounting_delta(s)
        test_maxmemory_bound(s, "lru")
        test_large_set_many_evictions(s)
        s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Second pass with LFU
    proc = start_server(evict="lfu")
    try:
        s = socket.create_connection(("127.0.0.1", PORT), 5)
        test_maxmemory_bound(s, "lfu")
        s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("test_prod_memory PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
