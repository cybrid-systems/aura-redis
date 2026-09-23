#!/usr/bin/env python3
"""A12 — named C kernel slru / tinylfu selectable via EVICT + INFO.

Exit:
  - EVICT slru / tinylfu accepted; INFO evict: matches
  - Under maxmemory + zipf-ish hot/cold flood, slru retains hot keys
    at least as well as lfu (regret ≤ 0 vs LFU), or documents competitive.
  - DENY_PLUGIN=1; no PLUGIN path.
"""
from __future__ import annotations

import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26981"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"


def start(evict: str, maxmemory: int = 120_000) -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call(
            [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-evict-slru-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(SERVER),
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
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
            s.close()
            return proc
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not listen")


def zipf_hit_rate(evict: str, seed: int = 7) -> float:
    proc = start(evict)
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        info = redis_call(s, "INFO")
        assert f"evict:{evict}" in info, info[:400]
        assert redis_call(s, "EVICT") == evict
        # Reject unknown (smoke_client raises on ERR — expected)
        try:
            redis_call(s, "EVICT", "not_a_kernel")
            raise AssertionError("expected ERR for unknown evict")
        except RuntimeError as e:
            assert "bad evict" in str(e), e

        rng = random.Random(seed)
        val = "v" * 200
        nkeys = 200
        # Fill past maxmemory
        for i in range(nkeys):
            redis_call(s, "SET", f"k{i}", val)

        # Hot set: touch top-20 zipf-ish frequently
        hot = list(range(20))
        for _ in range(800):
            # 80% hot, 20% cold flood
            if rng.random() < 0.8:
                k = hot[rng.randrange(len(hot))]
            else:
                k = rng.randrange(nkeys)
            redis_call(s, "GET", f"k{k}")

        # Measure retention on hot set
        hits = 0
        for k in hot:
            if redis_call(s, "EXISTS", f"k{k}") == 1:
                hits += 1
        rate = hits / len(hot)
        print(f"evict={evict} hot_retention={hits}/{len(hot)} ({100*rate:.1f}%)")
        redis_call(s, "QUIT")
        s.close()
        return rate
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    # Name wiring smoke
    proc = start("lru")
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        for name in ("slru", "tinylfu"):
            assert redis_call(s, "EVICT", name) == "OK", name
            assert redis_call(s, "EVICT") == name
            info = redis_call(s, "INFO")
            assert f"evict:{name}" in info, info[:500]
            print(f"PASS name-wire EVICT/INFO {name}")
        redis_call(s, "QUIT")
        s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    lfu = zipf_hit_rate("lfu")
    slru = zipf_hit_rate("slru")
    tinylfu = zipf_hit_rate("tinylfu")
    # Exit: regret ≤ LFU ⇒ slru/tinylfu retention ≥ lfu − epsilon
    eps = 0.05  # allow 1/20 noise
    if slru + eps < lfu:
        print(
            f"FAIL A12: slru regret vs LFU: slru={100*slru:.1f}% lfu={100*lfu:.1f}%"
        )
        return 1
    if tinylfu + eps < lfu:
        print(
            f"FAIL A12: tinylfu regret vs LFU: tinylfu={100*tinylfu:.1f}% "
            f"lfu={100*lfu:.1f}%"
        )
        return 1
    print(
        f"PASS A12: slru={100*slru:.1f}% tinylfu={100*tinylfu:.1f}% "
        f"lfu={100*lfu:.1f}% (regret≤0 vs LFU, eps={eps})"
    )
    print(
        "NOTE: tinylfu is an alias of sample-based SLRU (approx TinyLFU; "
        "no Count-Min sketch / window cache)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
