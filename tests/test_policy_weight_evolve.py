#!/usr/bin/env python3
"""A13/A15/A16 — signal-weight evolve + optional swarm backend + fiber shadow.

Exit:
  - evolve logs contain w-miss= / w-write= / w-evict= (A13 weight path)
  - with EVOLVE_BACKEND=pso: swarm-gen= / swarm-init logs (A15)
  - with FIBER_SHADOW=1: fiber-shadow champ=/trial= (A16, no apply loser)
  - Does not require full evolve_gain ≥8pp (that stays on bench_regret).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26973"))
IMG = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
BOOT = ROOT / f".ar-agent-booted-{PORT}.flag"
AGENT_LOG = Path(f"/tmp/ar-policy-weight-evolve-{PORT}.log")
BACKEND = os.environ.get("AURA_REDIS_EVOLVE_BACKEND", "pso")
FIBER = os.environ.get("AURA_REDIS_FIBER_SHADOW", "1")


def start_server() -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call(
            [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-policy-weight-srv-{PORT}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(PORT), "--evict", "lru", "--maxmemory", "200000"],
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


def start_agent() -> str:
    BOOT.unlink(missing_ok=True)
    (ROOT / f".ar-policy-pin-{PORT}.pin").unlink(missing_ok=True)
    AGENT_LOG.write_text("")
    cmd = [
        "sudo", "docker", "run", "-d", "--network", "host", "--entrypoint", "",
        "-v", f"{ROOT}:/work", "-v", "/tmp:/tmp", "-w", "/work",
        "-e", "AURA_SANDBOX=off",
        "-e", "AURA_PIPELINE_STRICT=0",
        "-e", "AURA_PATH=/work/.deps/aura/lib",
        "-e", f"AURA_REDIS_PORT={PORT}",
        "-e", "AURA_REDIS_HOST=127.0.0.1",
        "-e", "AURA_REDIS_POLICY_MS=80",
        "-e", "AURA_REDIS_DENY_PLUGIN=1",
        "-e", "AURA_REDIS_FITNESS_MUTATE=0",
        "-e", "AURA_REDIS_EVOLVE=1",
        "-e", "AURA_REDIS_EVOLVE_MAX_GENS=4",
        "-e", "AURA_REDIS_EVOLVE_WINDOW=3",
        "-e", "AURA_REDIS_THRESH_MIN_OPS=200",
        "-e", "AURA_REDIS_THRESH_MISS_PIN=40",
        "-e", "AURA_REDIS_WEIGHT_EVOLVE=1",
        "-e", f"AURA_REDIS_EVOLVE_BACKEND={BACKEND}",
        "-e", f"AURA_REDIS_FIBER_SHADOW={FIBER}",
        IMG,
        "/work/.deps/aura/build/aura",
        "/work/src/redis/policy_agent.aura",
    ]
    cid = subprocess.check_output(cmd, text=True).strip()
    for _ in range(120):
        subprocess.run(
            ["sudo", "docker", "logs", cid],
            stdout=AGENT_LOG.open("w"),
            stderr=subprocess.STDOUT,
            check=False,
        )
        text = AGENT_LOG.read_text(errors="replace")
        if "PING" in text or "evolve=" in text:
            return cid
        time.sleep(0.15)
    raise TimeoutError(
        f"agent did not boot; log:\n{AGENT_LOG.read_text(errors='replace')[-2000:]}"
    )


def drive_traffic(rounds: int = 10) -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=2)
    try:
        for r in range(rounds):
            for i in range(40):
                redis_call(s, "SET", f"hot:{i}", f"v{r}-{i}")
            for i in range(40):
                redis_call(s, "GET", f"hot:{i}")
            for i in range(60):
                redis_call(s, "SET", f"cold:{r}:{i}", "x" * 20)
            time.sleep(0.25)
    finally:
        s.close()


def main() -> int:
    proc = None
    cid = None
    try:
        proc = start_server()
        cid = start_agent()
        drive_traffic()
        # PSO lazy-require + step can take several seconds
        time.sleep(8.0 if BACKEND in ("pso", "fss", "grid", "swarm") else 2.0)
        subprocess.run(
            ["sudo", "docker", "logs", cid],
            stdout=AGENT_LOG.open("w"),
            stderr=subprocess.STDOUT,
            check=False,
        )
        text = AGENT_LOG.read_text(errors="replace")
        print("--- agent log (tail) ---")
        print("\n".join(text.splitlines()[-40:]))

        w_logs = [ln for ln in text.splitlines() if "w-miss=" in ln and "policy_agent:" in ln]
        evo_logs = [ln for ln in text.splitlines() if "evolve gen=" in ln]
        assert w_logs, f"A13 FAIL: expected w-miss= weight path; sample={text.splitlines()[:20]}"
        assert evo_logs, f"A13 FAIL: expected evolve gen= logs; got none"
        print(f"PASS A13: weight logs={len(w_logs)} evolve_logs={len(evo_logs)}")
        print("  ·", w_logs[0][:160])

        if BACKEND in ("pso", "fss", "grid", "swarm"):
            swarm_logs = [
                ln for ln in text.splitlines()
                if "swarm-gen=" in ln or "swarm-init" in ln
            ]
            assert swarm_logs, (
                f"A15 FAIL: backend={BACKEND} expected swarm-gen/init logs"
            )
            print(f"PASS A15: swarm logs={len(swarm_logs)} backend={BACKEND}")
            print("  ·", swarm_logs[0][:160])
        else:
            print(f"SKIP A15: backend={BACKEND}")

        if FIBER in ("1", "true", "on", "yes"):
            sh = [ln for ln in text.splitlines() if "fiber-shadow" in ln]
            assert sh, "A16 FAIL: expected fiber-shadow logs"
            assert any("no apply loser" in ln for ln in sh), sh[:3]
            print(f"PASS A16: fiber-shadow logs={len(sh)}")
            print("  ·", sh[0][:160])
        else:
            print("SKIP A16: FIBER_SHADOW off")

        return 0
    finally:
        if cid:
            subprocess.run(
                ["sudo", "docker", "rm", "-f", cid], capture_output=True
            )
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
        BOOT.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
