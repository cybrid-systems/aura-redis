#!/usr/bin/env python3
"""Headline regret harness — phase_marathon cumulative hit% vs per-phase oracle.

  python3 scripts/bench_regret.py
  python3 scripts/bench_regret.py phase_marathon,zipf_hotkey
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    workloads = "phase_marathon,zipf_hotkey,oscillate"
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
    print("bench_regret:", " ".join(cmd[2:]))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
