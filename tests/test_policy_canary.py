#!/usr/bin/env python3
"""A4 — canary choose-fn: trial N ticks → commit or auto-heal.

Two cases:
  1) bad trial (inject inverted/broken) → canary_heal within T ticks
  2) good trial (inject aggressive from normal) → canary_commit

Native-safe path: boundary-safe? + safety-snapshot; audit reasons
canary_start / canary_commit / canary_heal.
"""
from __future__ import annotations

import os
import re
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
)

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26953"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
CANARY_TICKS = 5
HEAL_DEADLINE_TICKS = CANARY_TICKS + 4  # within T (+ warm)


def _paths(tag: str):
    return {
        "hb": ROOT / f".ar-policy-hb-canary-{tag}-{PORT}",
        "audit": ROOT / f".ar-policy-audit-canary-{tag}-{PORT}.log",
        "agent_log": Path(f"/tmp/ar-policy-canary-{tag}-{PORT}.log"),
        "boot": ROOT / f".ar-agent-booted-{PORT}.flag",
    }


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call([str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-policy-canary-srv-{PORT}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(PORT), "--evict", "lru", "--maxmemory", "120000"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(100):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def start_agent(tag: str, inject: str, seed: str = "normal") -> str:
    p = _paths(tag)
    p["hb"].unlink(missing_ok=True)
    p["audit"].unlink(missing_ok=True)
    p["boot"].unlink(missing_ok=True)
    cid = _start_agent(
        env={
            "AURA_REDIS_PORT": str(PORT),
            "AURA_REDIS_HOST": "127.0.0.1",
            "AURA_REDIS_POLICY_MS": "80",
            "AURA_REDIS_DENY_PLUGIN": "1",
            "AURA_REDIS_FITNESS_MUTATE": "0",
            "AURA_REDIS_SEED_PROFILE": seed,
            "AURA_REDIS_CANARY": "1",
            "AURA_REDIS_CANARY_TICKS": str(CANARY_TICKS),
            "AURA_REDIS_CANARY_INJECT": inject,
        },
        log_path=p["agent_log"],
        path_env={
            "AURA_REDIS_POLICY_HEARTBEAT": p["hb"],
            "AURA_REDIS_POLICY_AUDIT": p["audit"],
        },
    )
    t0 = time.time()
    last = ""
    while time.time() - t0 < 12:
        last = _agent_logs(cid, p["agent_log"])
        if "PING" in last or "PONG" in last:
            return cid
        time.sleep(0.15)
    raise TimeoutError(f"agent did not PING; log:\n{last}")


def drive_load(rounds: int = 40) -> None:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
        for i in range(20):
            redis_call(s, "SET", f"z{i:04d}", "H" * 120)
            for _ in range(4):
                redis_call(s, "GET", f"z{i:04d}")
        for k in range(rounds):
            redis_call(s, "SET", f"hot{k % 8}", "v" * 48)
            redis_call(s, "GET", f"hot{k % 8}")
            if k % 5 == 0:
                redis_call(s, "GET", f"__miss{k}")
        time.sleep(0.4)


LINE_RE = re.compile(
    r"ts=(?P<ts>\d+)\s+op=(?P<op>\S+)\s+from=(?P<frm>\S+)\s+"
    r"to=(?P<to>\S+)\s+reason=(?P<reason>\S+)\s+version=(?P<ver>\d+)"
)


def _parse_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(errors="replace").splitlines():
        m = LINE_RE.search(ln)
        if m:
            out.append(m.groupdict())
    return out


def _stop(srv, cid):
    if cid:
        _stop_agent(cid)
    import signal as _sig
    try:
        srv.send_signal(_sig.SIGTERM)
    except Exception:
        pass
    try:
        srv.wait(timeout=3)
    except Exception:
        srv.kill()


def test_canary_bad_trial_heals() -> None:
    """Inject inverted → auto heal within T ticks; audit canary_heal."""
    srv = start_server()
    cid = None
    tag = "bad"
    p = _paths(tag)
    try:
        cid = start_agent(tag, inject="inverted", seed="normal")
        # Warm + trial window (+ margin)
        deadline = time.time() + (CANARY_TICKS + 8) * 0.12 + 6.0
        healed = False
        while time.time() < deadline:
            drive_load(12)
            log = _agent_logs(cid, p["agent_log"])
            audit = _parse_audit(p["audit"])
            reasons = {a["reason"] for a in audit}
            ops = {a["op"] for a in audit}
            if "canary_heal" in reasons or "canary-heal" in ops:
                healed = True
                break
            if "canary-heal!" in log or "canary_heal" in log:
                # wait briefly for audit flush
                time.sleep(0.3)
                audit = _parse_audit(p["audit"])
                if any(a["reason"] == "canary_heal" for a in audit):
                    healed = True
                    break
            time.sleep(0.15)

        log = _agent_logs(cid, p["agent_log"])
        audit = _parse_audit(p["audit"])
        assert any(a["op"] == "canary-start" for a in audit) or "canary-start" in log, (
            f"missing canary-start; audit={audit}\nlog:\n{log[-1500:]}"
        )
        assert healed or any(a["reason"] == "canary_heal" for a in audit), (
            f"bad trial did not heal within T={HEAL_DEADLINE_TICKS}; "
            f"audit={audit}\nlog:\n{log[-2000:]}"
        )
        # Must not have committed the bad body
        assert not any(
            a["op"] == "canary-commit" and a.get("to") in ("inverted", "broken")
            for a in audit
        ), f"bad trial must not commit: {audit}"

        # Heartbeat canary_heals advanced
        hb = p["hb"].read_text(errors="replace") if p["hb"].exists() else ""
        assert "canary_heals=" in hb, f"heartbeat missing canary_heals:\n{hb}"
        print(f"PASS test_canary_bad_trial_heals: audit ops={[a['op'] for a in audit]}")
    finally:
        _stop(srv, cid)


def test_canary_good_trial_commits() -> None:
    """Inject aggressive from normal under hit-friendly load → canary_commit."""
    srv = start_server()
    cid = None
    tag = "good"
    p = _paths(tag)
    try:
        cid = start_agent(tag, inject="aggressive", seed="normal")
        deadline = time.time() + (CANARY_TICKS + 10) * 0.12 + 8.0
        committed = False
        while time.time() < deadline:
            drive_load(16)
            _agent_logs(cid, p["agent_log"])
            audit = _parse_audit(p["audit"])
            if any(a["op"] == "canary-commit" and a["reason"] == "canary_commit" for a in audit):
                committed = True
                break
            time.sleep(0.15)

        log = _agent_logs(cid, p["agent_log"])
        audit = _parse_audit(p["audit"])
        assert any(a["op"] == "canary-start" for a in audit), (
            f"missing canary-start; audit={audit}\nlog:\n{log[-1500:]}"
        )
        assert committed or any(a["op"] == "canary-commit" for a in audit), (
            f"good trial did not commit; audit={audit}\nlog:\n{log[-2000:]}"
        )
        hb = p["hb"].read_text(errors="replace") if p["hb"].exists() else ""
        assert "canary_commits=" in hb, f"heartbeat missing canary_commits:\n{hb}"
        print(f"PASS test_canary_good_trial_commits: audit ops={[a['op'] for a in audit]}")
    finally:
        _stop(srv, cid)


if __name__ == "__main__":
    test_canary_bad_trial_heals()
    test_canary_good_trial_commits()
    print("PASS all canary tests")
