#!/usr/bin/env python3
"""Thin adaptive EVICT controller for bench-e2e throughput runs."""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402


def main() -> None:
    port = int(sys.argv[1])
    min_ops = 80
    prev = None
    s = None
    for _ in range(80):
        try:
            s = socket.create_connection(("127.0.0.1", port), 2)
            break
        except OSError:
            time.sleep(0.05)
    if s is None:
        raise SystemExit("no connect")

    while True:
        try:
            info = redis_call(s, "INFO")
        except Exception:
            time.sleep(0.08)
            try:
                s.close()
            except Exception:
                pass
            try:
                s = socket.create_connection(("127.0.0.1", port), 2)
            except OSError:
                break
            continue
        d = {}
        for ln in info.splitlines():
            if ":" in ln and not ln.startswith("#"):
                k, v = ln.split(":", 1)
                d[k.strip()] = v.strip()
        g = int(d.get("gets", 0))
        se = int(d.get("sets", 0))
        h = int(d.get("hits", 0))
        m = int(d.get("misses", 0))
        cur = d.get("evict", "")
        if prev is not None:
            dg = g - prev[0]
            ds = se - prev[1]
            dh = h - prev[2]
            dm = m - prev[3]
            ops = dg + ds
            choice = ""
            if ops >= min_ops:
                hit_pct = (100 * dh) // (dh + dm) if (dh + dm) > 0 else 0
                if ds > dg * 2:
                    choice = "lfu"
                elif dg > ds * 5 and hit_pct >= 60:
                    choice = "lru"
            if choice and choice != cur:
                try:
                    redis_call(s, "EVICT", choice)
                except Exception:
                    pass
        prev = (g, se, h, m)
        time.sleep(0.08)


if __name__ == "__main__":
    main()
