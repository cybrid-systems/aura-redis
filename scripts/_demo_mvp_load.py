#!/usr/bin/env python3
"""Load phases for demo-mvp.sh (hot_protect / ws_shift)."""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402


def info_field(s: socket.socket, key: str) -> str:
    info = redis_call(s, "INFO")
    for ln in info.splitlines():
        if ln.startswith(key + ":"):
            return ln.split(":", 1)[1]
    return "?"


def phase_a(port: int, wait_lfu: bool) -> None:
    s = socket.create_connection(("127.0.0.1", port), 5)
    redis_call(s, "PING")
    if wait_lfu:
        for _ in range(30):
            if info_field(s, "evict") == "lfu":
                break
            for j in range(120):
                redis_call(s, "SET", f"__pre{j}", "p" * 64)
            time.sleep(0.1)
    else:
        for j in range(150):
            redis_call(s, "SET", f"__pre{j}", "p" * 64)

    nhot, boost, ncold, vlen = 40, 30, 800, 180
    val = "V" * vlen
    for i in range(nhot):
        redis_call(s, "SET", f"hot{i:04d}", val)
    for b in range(boost):
        for i in range(nhot):
            redis_call(s, "GET", f"hot{i:04d}")
        if wait_lfu and b % 5 == 0:
            for j in range(40):
                redis_call(s, "SET", f"__keep{j}", "k" * 32)
    for i in range(ncold):
        redis_call(s, "SET", f"cold{i:05d}", val)

    hits = misses = 0
    for i in range(nhot):
        if redis_call(s, "GET", f"hot{i:04d}") is None:
            misses += 1
        else:
            hits += 1
    total = hits + misses
    pct = 100.0 * hits / total if total else 0.0
    print(
        f"RESULT hits={hits} misses={misses} hit_pct={pct:.1f} "
        f"evict={info_field(s, 'evict')} layout={info_field(s, 'layout')}"
    )
    s.close()


def phase_b(port: int) -> None:
    s = socket.create_connection(("127.0.0.1", port), 5)
    redis_call(s, "FLUSHDB")
    for j in range(100):
        redis_call(s, "SET", f"__br{j}", "b" * 48)
    for _ in range(8):
        for j in range(100):
            redis_call(s, "GET", f"__br{j}")
    time.sleep(0.5)
    nA, nB, boostA, rounds, reads_per, vlen = 200, 150, 40, 8, 200, 400
    val = "V" * vlen
    for i in range(nA):
        redis_call(s, "SET", f"A{i:04d}", val)
    for _ in range(boostA):
        for i in range(nA):
            redis_call(s, "GET", f"A{i:04d}")
    hits = misses = 0
    for _ in range(rounds):
        for i in range(nB):
            redis_call(s, "SET", f"B{i:04d}", val)
        for j in range(reads_per):
            if redis_call(s, "GET", f"B{j % nB:04d}") is None:
                misses += 1
            else:
                hits += 1
    total = hits + misses
    pct = 100.0 * hits / total if total else 0.0
    print(
        f"RESULT hits={hits} misses={misses} hit_pct={pct:.1f} "
        f"evict={info_field(s, 'evict')}"
    )
    s.close()


def invert_nudge(port: int) -> None:
    s = socket.create_connection(("127.0.0.1", port), 5)
    for j in range(200):
        redis_call(s, "SET", f"__inv{j}", "x" * 64)
    time.sleep(0.5)
    print(f"RESULT after_invert evict={info_field(s, 'evict')}")
    s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["a", "b", "invert"])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--wait-lfu", action="store_true")
    args = ap.parse_args()
    if args.phase == "a":
        phase_a(args.port, args.wait_lfu)
    elif args.phase == "b":
        phase_b(args.port)
    else:
        invert_nudge(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
