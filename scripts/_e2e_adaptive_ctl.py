#!/usr/bin/env python3
"""Host-only CI mirror of choose_normal.aura for bench-e2e throughput path.

Product adaptive control plane is policy_agent.aura (see --aura-agent default).
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

# Import shared choose from bench module
sys.path.insert(0, str(ROOT / "scripts"))
from bench_dynamic_evict import apply_choice, choose_policy, parse_info, info_int  # noqa: E402


def main() -> None:
    port = int(sys.argv[1])
    profile = sys.argv[2] if len(sys.argv) > 2 else "normal"
    prev = None
    s = None
    swaps: list[str] = []
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
            info = parse_info(redis_call(s, "INFO"))
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
        g = info_int(info, "gets")
        se = info_int(info, "sets")
        h = info_int(info, "hits")
        m = info_int(info, "misses")
        ev = info_int(info, "evicted")
        nk = info_int(info, "keys")
        cur = info.get("evict", "")
        cur_ly = info.get("layout", "flat")
        if prev is not None:
            dg, ds, dh, dm, de = (
                g - prev[0],
                se - prev[1],
                h - prev[2],
                m - prev[3],
                ev - prev[4],
            )
            choice = choose_policy(profile, dg, ds, dh, dm, de, nk)
            try:
                apply_choice(s, choice, cur, cur_ly, swaps)
            except Exception:
                pass
        prev = (g, se, h, m, ev)
        time.sleep(0.08)


if __name__ == "__main__":
    main()
