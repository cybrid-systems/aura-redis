#!/usr/bin/env python3
"""Eviction smoke under maxmemory (lru/lfu)."""
from __future__ import annotations
import argparse, socket, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call, encode_array  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=26410)
    p.add_argument("--evict", default="lru", choices=("lru", "lfu"))
    p.add_argument("--maxmemory", type=int, default=200_000)
    args = p.parse_args()
    bin_path = ROOT / "native/build/aura_redis_server"
    log = Path(f"/tmp/ar-evict-{args.port}.log")
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(args.port), "--evict", args.evict,
         "--maxmemory", str(args.maxmemory)],
        stdout=log.open("w"), stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(0.3)
        s = socket.create_connection(("127.0.0.1", args.port), timeout=5)
        # Fill beyond maxmemory with ~1KB values
        val = "v" * 1000
        n = 500
        for i in range(n):
            redis_call(s, "SET", f"k{i}", val)
        # Touch early keys less if lru; for lfu touch high keys often
        for _ in range(50):
            for i in range(400, 500):
                redis_call(s, "GET", f"k{i}")
        # Early keys should often be gone under pressure
        survivors = 0
        for i in range(n):
            if redis_call(s, "EXISTS", f"k{i}") == 1:
                survivors += 1
        print(f"evict={args.evict} maxmemory={args.maxmemory} set={n} survivors={survivors}")
        if survivors >= n:
            print("FAIL: expected some eviction")
            return 1
        if survivors == 0:
            print("WARN: all keys gone (ok if maxmemory tiny)")
        # Hot keys more likely present for lru/lfu after touching 400..499
        hot = sum(1 for i in range(400, 500) if redis_call(s, "EXISTS", f"k{i}") == 1)
        cold = sum(1 for i in range(0, 100) if redis_call(s, "EXISTS", f"k{i}") == 1)
        print(f"hot400-499={hot} cold0-99={cold}")
        redis_call(s, "QUIT")
        s.close()
        print("eviction test OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
