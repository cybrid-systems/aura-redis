#!/usr/bin/env python3
"""Industry-shaped harness pack (MVP M3) — thin wrapper around bench_dynamic_evict."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    workloads = "zipf_hotkey,hot_protect,oscillate"
    if len(sys.argv) > 1:
        workloads = sys.argv[1]
    cmd = [
        sys.executable,
        str(ROOT / "scripts/bench_dynamic_evict.py"),
        "--workloads",
        workloads,
        "--policies",
        "lru,lfu,adaptive",
    ]
    print("bench_industry:", " ".join(cmd[2:]))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
