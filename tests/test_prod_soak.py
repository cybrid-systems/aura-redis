#!/usr/bin/env python3
"""P0.7 — short soak under maxmemory (delegates to scripts/prod-soak.sh)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUR = os.environ.get("AURA_REDIS_SOAK_SEC", "30")


def main() -> int:
    env = os.environ.copy()
    env["AURA_REDIS_SOAK_SEC"] = DUR
    env.setdefault("AURA_REDIS_TEST_PORT", "26907")
    r = subprocess.run(
        ["bash", str(ROOT / "scripts/prod-soak.sh"), DUR],
        cwd=str(ROOT),
        env=env,
    )
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
