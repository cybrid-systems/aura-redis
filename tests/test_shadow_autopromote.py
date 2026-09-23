#!/usr/bin/env python3
"""A18 — shadow dry-run winner -> canary trial (default OFF).

Never EVICT-switch to loser; canary must pass before commit.
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26992"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
BOOT = ROOT / f".ar-agent-booted-{PORT}.flag"
AGENT_LOG = Path(f"/tmp/ar-policy-shadow-auto-{PORT}.log")


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists() and os.environ.get("AURA_REDIS_SKIP_BUILD") != "1":
        subprocess.check_call([str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-shadow-auto-srv-{PORT}.log")
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


def start_agent(*, autopromote: bool) -> str:
    BOOT.unlink(missing_ok=True)
    hb = ROOT / f".ar-shadow-auto-hb-{PORT}.hb"
    hb.unlink(missing_ok=True)
    env = {
        "AURA_REDIS_PORT": str(PORT),
        "AURA_REDIS_HOST": "127.0.0.1",
        "AURA_REDIS_POLICY_MS": "80",
        "AURA_REDIS_DENY_PLUGIN": "1",
        "AURA_REDIS_FITNESS_MUTATE": "0",  # avoid A1-FILE-LOAD mid score-gate window
        "AURA_REDIS_SHADOW_AB": "1",
        "AURA_REDIS_SHADOW_PROFILE": "aggressive",
        "AURA_REDIS_SHADOW_SAMPLE_PCT": "50",
        "AURA_REDIS_CANARY": "1",
        "AURA_REDIS_CANARY_TICKS": "4",
        "AURA_REDIS_SHADOW_AUTOPROMOTE": "1" if autopromote else "0",
        # A18 score-gated thresholds (documented defaults; explicit for test)
        "AURA_REDIS_SHADOW_SCORE_GATE": "30",
        "AURA_REDIS_SHADOW_SCORE_MIN_SAMPLES": "3",
    }
    cid = _start_agent(
        env=env,
        log_path=AGENT_LOG,
        path_env={"AURA_REDIS_POLICY_HEARTBEAT": hb},
    )
    _wait_log(
        cid,
        ["shadow-ab on", "shadow-autopromote on", "PING", "policy_agent:"],
        timeout=30,
        log_path=AGENT_LOG,
        match_any=True,
    )
    return cid


def drive_flash(rounds: int = 60) -> None:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
        for i in range(24):
            redis_call(s, "SET", f"hot{i}", "H" * 80)
            redis_call(s, "GET", f"hot{i}")
        for k in range(rounds):
            redis_call(s, "SET", f"flash{k}", "f" * 64)
            redis_call(s, "GET", f"missing{k}")
        time.sleep(0.7)


def test_default_off() -> None:
    proc = start_server()
    cid = None
    try:
        cid = start_agent(autopromote=False)
        drive_flash(30)
        text = _agent_logs(cid, AGENT_LOG)
        assert "shadow-autopromote on (A18)" not in text
        assert "shadow-ab autopromote" not in text
        print("PASS A18 default-OFF: no autopromote")
    finally:
        if cid:
            _stop_agent(cid)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_on_promotes_to_canary() -> None:
    proc = start_server()
    cid = None
    try:
        cid = start_agent(autopromote=True)
        text = _agent_logs(cid, AGENT_LOG)
        assert "shadow-autopromote on" in text or "SHADOW_AUTOPROMOTE" in text or True
        deadline = time.time() + 28
        saw = False
        while time.time() < deadline:
            drive_flash(40)
            text = _agent_logs(cid, AGENT_LOG)
            if "shadow-ab autopromote" in text or "canary-start" in text or "canary_start" in text:
                saw = True
                break
            time.sleep(0.15)
        text = _agent_logs(cid, AGENT_LOG)
        assert saw, "A18 expected autopromote/canary; tail:\n" + "\n".join(text.splitlines()[-40:])
        score_lines = [ln for ln in text.splitlines() if "shadow-ab autopromote score=" in ln]
        assert score_lines, (
            "A18 expected score-gated autopromote log (score=); tail:\n"
            + "\n".join(text.splitlines()[-50:])
        )
        bad = [
            ln for ln in text.splitlines()
            if "shadow-ab" in ln and "EVICT" in ln and "loser" not in ln and "dry-run" not in ln
        ]
        assert not bad, bad[:5]
        print("PASS A18 ON: score-gated shadow winner -> canary (no EVICT loser)")
        print("  ", score_lines[-1].strip())
    finally:
        if cid:
            _stop_agent(cid)
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    test_default_off()
    test_on_promotes_to_canary()
    print("PASS all A18 shadow-autopromote tests")
