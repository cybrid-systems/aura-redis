#!/usr/bin/env python3
"""Headline regret + mutation_gain harness.

DEFAULT adaptive control plane = Aura policy_agent.aura (native in GHA jobs / docker locally).
Pass --python-ctl for the host-only Python mirror of choose_*.aura.

  python3 scripts/bench_regret.py
  python3 scripts/bench_regret.py phase_marathon,zipf_hotkey
  python3 scripts/bench_regret.py mutation_gain --  # frozen vs mutate
  python3 scripts/bench_regret.py poison_heal
  python3 scripts/bench_regret.py ttl_wave
  python3 scripts/bench_regret.py flash_churn
  python3 scripts/bench_regret.py evolve_gain
  python3 scripts/bench_regret.py prefix_mix
  python3 scripts/bench_regret.py prefix_mix_v2
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    workloads = "phase_marathon,zipf_hotkey,oscillate"
    extra: list[str] = []
    policies = "lru,lfu,adaptive"
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        workloads = sys.argv[1]
        extra = sys.argv[2:]
    else:
        extra = sys.argv[1:]

    if workloads in ("mutation_gain", "diurnal_shift"):
        policies = "lru,lfu,adaptive_frozen,adaptive_mutate"
    elif workloads in ("ttl_wave", "session_churn"):
        policies = "lru,lfu,ttl_aware,adaptive"
    elif workloads == "flash_churn":
        policies = "lfu,lru,adaptive_nosoft,adaptive_soft"
    elif workloads == "evolve_gain":
        policies = "lru,lfu,adaptive_evolve_frozen,adaptive_evolve"
    elif workloads == "prefix_mix":
        policies = "lru,lfu,adaptive,adaptive_prefix"
    elif workloads == "prefix_mix_v2":
        policies = "lru,lfu,ttl_aware,adaptive,adaptive_prefix"
    elif workloads == "poison_heal":
        policies = "lru,lfu,poison_frozen,poison_mutate"
    elif "mutation_gain" in workloads or "diurnal_shift" in workloads:
        # keep adaptive + frozen/mutate if user listed them
        if "adaptive_frozen" not in ",".join(extra):
            policies = "lru,lfu,adaptive_frozen,adaptive_mutate,adaptive"

    cmd = [
        sys.executable,
        str(ROOT / "scripts/bench_dynamic_evict.py"),
        "--workloads",
        workloads,
        "--policies",
        policies,
        *extra,
    ]
    # If user already passed --policies in extra, drop our default
    if any(a == "--policies" or a.startswith("--policies=") for a in extra):
        cmd = [
            sys.executable,
            str(ROOT / "scripts/bench_dynamic_evict.py"),
            "--workloads",
            workloads,
            *extra,
        ]
    print("bench_regret:", " ".join(cmd[2:]))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
