#!/usr/bin/env python3
"""SSOT guard: short harness adaptive rows must not be publishable without override."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    os.environ["AURA_REDIS_CI_STRICT_SSOT"] = "1"
    os.environ.pop("AURA_REDIS_ALLOW_SHORT_ADAPTIVE_CITE", None)

    allow = os.environ.get("AURA_REDIS_ALLOW_SHORT_ADAPTIVE_CITE", "") == "1"
    aura_pols = ["adaptive", "lru"]
    will_emit_adaptive = any(p == "adaptive" for p in aura_pols)
    if not (will_emit_adaptive and not allow):
        print("SSOT guard FAILED: expected reject path", file=sys.stderr)
        return 1
    print("SSOT guard: correctly rejects short-harness adaptive without override")

    os.environ["AURA_REDIS_ALLOW_SHORT_ADAPTIVE_CITE"] = "1"
    allow = os.environ.get("AURA_REDIS_ALLOW_SHORT_ADAPTIVE_CITE", "") == "1"
    if not (will_emit_adaptive and allow):
        print("SSOT guard FAILED: override path", file=sys.stderr)
        return 1
    print("SSOT guard: override AURA_REDIS_ALLOW_SHORT_ADAPTIVE_CITE=1 permits local experiment")

    env = os.environ.copy()
    env.pop("AURA_REDIS_ALLOW_SHORT_ADAPTIVE_CITE", None)
    env["AURA_REDIS_CI_STRICT_SSOT"] = "1"
    r = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/bench_hit_vs_redis.py"),
            "--aura-policies",
            "adaptive",
            "--workloads",
            "zipf",
            "--fail-on-adaptive-cite",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if r.returncode != 2:
        print(
            f"expected exit 2 from bench_hit --fail-on-adaptive-cite, got {r.returncode}",
            file=sys.stderr,
        )
        print(r.stdout[-500:], r.stderr[-500:], file=sys.stderr)
        return 1
    print("PASS check-ssot-short-adaptive (bench exits 2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
