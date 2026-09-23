#!/usr/bin/env python3
"""A10 — shadow / A/B sample path (C hook + agent dual dry-run).

Exit:
  - CONFIG/INFO/SHADOW expose shadow_* stats
  - C sample-pct samples GET hit/miss without changing eviction kernel
  - Agent SHADOW_AB dry-run logs diverge and never EVICT-switches to loser
  - DENY_PLUGIN=1
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26982"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
IMG = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")
BOOT = ROOT / f".ar-agent-booted-{PORT}.flag"
AGENT_LOG = Path(f"/tmp/ar-policy-shadow-ab-{PORT}.log")


def start_server() -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call(
            [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-shadow-ab-srv-{PORT}.log")
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


def test_c_shadow_hook() -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    assert redis_call(s, "SHADOW", "policy", "inverted") == "OK"
    assert redis_call(s, "SHADOW", "sample-pct", "100") == "OK"
    assert redis_call(s, "SHADOW", "reset") == "OK"
    assert redis_call(s, "EVICT") == "lru"
    redis_call(s, "SET", "a", "1")
    redis_call(s, "SET", "b", "2")
    redis_call(s, "GET", "a")
    redis_call(s, "GET", "missing")
    info = redis_call(s, "INFO")
    assert "shadow_policy:inverted" in info, info
    assert "shadow_sample_pct:100" in info, info
    sh = redis_call(s, "SHADOW")
    assert "policy:inverted" in sh and "samples:" in sh, sh
    samples = int(
        [ln for ln in info.splitlines() if ln.startswith("shadow_samples:")][0].split(":")[1]
    )
    hits = int(
        [ln for ln in info.splitlines() if ln.startswith("shadow_hits:")][0].split(":")[1]
    )
    misses = int(
        [ln for ln in info.splitlines() if ln.startswith("shadow_misses:")][0].split(":")[1]
    )
    assert samples >= 2, (samples, hits, misses, info)
    assert hits >= 1 and misses >= 1, (hits, misses)
    cfg = redis_call(s, "CONFIG", "GET", "shadow-policy")
    assert "shadow-policy" in str(cfg) and "inverted" in str(cfg), cfg
    assert redis_call(s, "CONFIG", "SET", "shadow-sample-pct", "10") == "OK"
    assert redis_call(s, "SHADOW", "diverge") == "OK"
    info2 = redis_call(s, "INFO")
    div = int(
        [ln for ln in info2.splitlines() if ln.startswith("shadow_diverges:")][0].split(":")[1]
    )
    assert div >= 1, info2
    assert redis_call(s, "EVICT") == "lru"
    redis_call(s, "QUIT")
    s.close()
    print("PASS A10 C: SHADOW/CONFIG/INFO sample hook (no EVICT switch)")


def start_agent() -> str:
    BOOT.unlink(missing_ok=True)
    (ROOT / f".ar-policy-pin-{PORT}.pin").unlink(missing_ok=True)
    AGENT_LOG.write_text("")
    hb = str(ROOT / f".ar-shadow-hb-{PORT}.hb")
    try:
        Path(hb).unlink(missing_ok=True)
    except OSError:
        pass
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
        "-e", "AURA_REDIS_FITNESS_MUTATE=1",
        "-e", "AURA_REDIS_SHADOW_AB=1",
        "-e", "AURA_REDIS_SHADOW_PROFILE=aggressive",
        "-e", "AURA_REDIS_SHADOW_SAMPLE_PCT=50",
        "-e", f"AURA_REDIS_POLICY_HEARTBEAT={hb}",
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
        if "shadow-ab on" in text or "PING" in text or "policy_agent:" in text:
            return cid
        time.sleep(0.15)
    raise TimeoutError(
        f"agent did not boot; log:\n{AGENT_LOG.read_text(errors='replace')[-2000:]}"
    )


def test_agent_shadow_dryrun(cid: str) -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    val = "x" * 100
    for i in range(80):
        redis_call(s, "SET", f"w{i}", val)
    for i in range(40):
        redis_call(s, "GET", f"missing{i}")
    time.sleep(1.2)
    for i in range(40):
        redis_call(s, "SET", f"w{i+80}", val)
        redis_call(s, "GET", f"nope{i}")
    time.sleep(1.5)
    evict_before = redis_call(s, "EVICT")
    info = redis_call(s, "INFO")
    redis_call(s, "QUIT")
    s.close()

    subprocess.run(
        ["sudo", "docker", "logs", cid],
        stdout=AGENT_LOG.open("w"),
        stderr=subprocess.STDOUT,
        check=False,
    )
    text = AGENT_LOG.read_text(errors="replace")
    assert "shadow-ab on (A10)" in text, text[-2000:]
    diverges = [
        ln for ln in text.splitlines()
        if "shadow-ab champ=" in ln and "dry-run no EVICT loser" in ln
    ]
    # Ensure agent never applied challenger via shadow path
    bad = [ln for ln in text.splitlines() if "shadow-ab" in ln and "EVICT" in ln and "loser" not in ln]
    assert not bad, bad[:5]
    assert diverges, (
        "A10 FAIL: expected shadow-ab dry-run diverge logs; tail:\n"
        + "\n".join(text.splitlines()[-30:])
    )
    print(f"PASS A10 agent: dry-run diverges={len(diverges)} live_evict={evict_before}")
    print("  ·", diverges[0][:160])


def main() -> int:
    proc = None
    cid = None
    try:
        proc = start_server()
        test_c_shadow_hook()
        cid = start_agent()
        test_agent_shadow_dryrun(cid)
        print("PASS A10 shadow policy sample path")
        return 0
    finally:
        if cid:
            subprocess.run(["sudo", "docker", "rm", "-f", cid], capture_output=True)
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
