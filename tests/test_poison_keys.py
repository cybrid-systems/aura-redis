#!/usr/bin/env python3
"""A19 — unique-SET / poison-key flood defense (S2 variant).

C INFO unique_sets advances; policy_agent mutates to defensive choose
(lru + refuse pin + soft). Demo scoreboard = keep* hit quality, not ops/s.
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
from _portutil import kill_tcp_port  # noqa: E402
from _agentutil import (  # noqa: E402
    agent_logs as _agent_logs,
    start_agent as _start_agent,
    stop_agent as _stop_agent,
    wait_log as _wait_log,
)

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26995"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
AGENT_LOG = Path(f"/tmp/ar-policy-poison-{PORT}.log")
AUDIT = ROOT / f".ar-policy-audit-poison-{PORT}.log"
HB = ROOT / f".ar-policy-hb-poison-{PORT}"


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists() and os.environ.get("AURA_REDIS_SKIP_BUILD") != "1":
        subprocess.check_call([str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-poison-srv-{PORT}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(PORT), "--evict", "lru", "--maxmemory", "250000"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(80):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
            s.close()
            return proc
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not listen")


def info_field(info: str, key: str) -> str:
    for ln in info.splitlines():
        if ln.startswith(key + ":"):
            return ln.split(":", 1)[1]
    return ""


def test_unique_sets_counter() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
            before = int(info_field(redis_call(s, "INFO"), "unique_sets") or "0")
            for i in range(50):
                redis_call(s, "SET", f"poison{i}", "x" * 32)
            after = int(info_field(redis_call(s, "INFO"), "unique_sets") or "0")
            redis_call(s, "QUIT")
        assert after >= before + 40, (before, after)
        print(f"PASS A19 C: unique_sets {before} -> {after}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_poison_defense_mutates() -> None:
    proc = start_server()
    cid = None
    try:
        AUDIT.unlink(missing_ok=True)
        HB.unlink(missing_ok=True)
        (ROOT / f".ar-agent-booted-{PORT}.flag").unlink(missing_ok=True)
        cid = _start_agent(
            env={
                "AURA_REDIS_PORT": str(PORT),
                "AURA_REDIS_HOST": "127.0.0.1",
                "AURA_REDIS_POLICY_MS": "80",
                "AURA_REDIS_DENY_PLUGIN": "1",
                "AURA_REDIS_FITNESS_MUTATE": "0",
                "AURA_REDIS_SEED_PROFILE": "normal",
                "AURA_REDIS_POISON_DEFENSE": "1",
                "AURA_REDIS_POISON_UNIQUE_RATE": "10",
            },
            log_path=AGENT_LOG,
            path_env={
                "AURA_REDIS_POLICY_HEARTBEAT": HB,
                "AURA_REDIS_POLICY_AUDIT": AUDIT,
            },
        )
        _wait_log(
            cid,
            ["PING", "policy_agent:"],
            timeout=30,
            log_path=AGENT_LOG,
            match_any=True,
        )

        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
            for i in range(20):
                redis_call(s, "SET", f"keep{i}", "H" * 64)
                for _ in range(3):
                    redis_call(s, "GET", f"keep{i}")
            for wave in range(6):
                for i in range(100):
                    redis_call(s, "SET", f"poison{wave}_{i}", "p" * 48)
                time.sleep(0.5)
            # allow policy ticks to observe unique_sets deltas
            time.sleep(2.0)
            hits = misses = 0
            for i in range(20):
                v = redis_call(s, "GET", f"keep{i}")
                if v is None or v == "" or v == b"":
                    misses += 1
                else:
                    hits += 1
            info = redis_call(s, "INFO")
            evict = redis_call(s, "EVICT")
            redis_call(s, "QUIT")

        text = _agent_logs(cid, AGENT_LOG)
        audit = AUDIT.read_text(errors="replace") if AUDIT.exists() else ""
        hb = HB.read_text(errors="replace") if HB.exists() else ""
        defended = (
            "poison-storm" in text
            or "poison-defense" in text
            or "unique_set_storm" in audit
            or "poison_active=1" in hb
            or "poison_trips=" in hb and not any(ln == "poison_trips=0" for ln in hb.splitlines())
            or "defensive" in audit
            or "defensive" in text
            or "to=defensive" in audit
        )
        assert defended, (
            f"A19 expected defensive mutate; evict={evict} hits={hits}/{hits+misses}\n"
            f"unique={info_field(info, 'unique_sets')}\n"
            f"audit={audit[-800:]}\nlog={text[-1200:]}"
        )
        keep_hit = hits / max(hits + misses, 1)
        print(f"PASS A19 agent: defense fired keep_hit={keep_hit:.2f} evict={evict}")
    finally:
        if cid:
            _stop_agent(cid)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    test_unique_sets_counter()
    test_poison_defense_mutates()
    print("PASS all A19 poison_keys tests")
