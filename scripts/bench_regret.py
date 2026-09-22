#!/usr/bin/env python3
"""Headline regret harness — phase_marathon cumulative hit% vs per-phase oracle.

DEFAULT adaptive control plane = Aura policy_agent.aura (Docker).
Pass --python-ctl for the host-only Python mirror of choose_*.aura.

  python3 scripts/bench_regret.py
  python3 scripts/bench_regret.py phase_marathon,zipf_hotkey
  python3 scripts/bench_regret.py phase_marathon --python-ctl
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    workloads = "phase_marathon,zipf_hotkey,oscillate"
    extra: list[str] = []
    if len(sys.argv) > 1:
        # First non-flag arg = workload list; remaining flags forward to bench.
        if not sys.argv[1].startswith("-"):
            workloads = sys.argv[1]
            extra = sys.argv[2:]
        else:
            extra = sys.argv[1:]
    cmd = [
        sys.executable,
        str(ROOT / "scripts/bench_dynamic_evict.py"),
        "--workloads",
        workloads,
        "--policies",
        "lru,lfu,adaptive",
        *extra,
    ]
    print("bench_regret:", " ".join(cmd[2:]))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
