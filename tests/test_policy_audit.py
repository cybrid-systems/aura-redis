#!/usr/bin/env python3
"""A3 — mutation audit ring + explain schema (native-leaning).

Spins C server + Aura policy_agent with fitness on and conservative seed,
drives a miss spike so fitness-swap fires, then asserts:

  • durable audit file lines: ts=… op=… from=… to=… reason=… version=…
  • heartbeat carries last_audit_* / last_explain
  • at least one fitness-swap (or evict) audit with a known reason

Native provenance is best-effort (logged as native=… when query works).
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
    wait_log as _wait_log,
)

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26943"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
HEARTBEAT = ROOT / f".ar-policy-hb-audit-{PORT}"
AUDIT = ROOT / f".ar-policy-audit-{PORT}.log"
AGENT_LOG = Path(f"/tmp/ar-policy-audit-agent-{PORT}.log")


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call([str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-policy-audit-srv-{PORT}.log")
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


def start_agent() -> str:
    HEARTBEAT.unlink(missing_ok=True)
    AUDIT.unlink(missing_ok=True)
    (ROOT / f".ar-agent-booted-{PORT}.flag").unlink(missing_ok=True)
    cid = _start_agent(
        env={
            "AURA_REDIS_PORT": str(PORT),
            "AURA_REDIS_HOST": "127.0.0.1",
            "AURA_REDIS_POLICY_MS": "100",
            "AURA_REDIS_DENY_PLUGIN": "1",
            "AURA_REDIS_FITNESS_MUTATE": "1",
            "AURA_REDIS_SEED_PROFILE": "conservative",
        },
        log_path=AGENT_LOG,
        path_env={
            "AURA_REDIS_POLICY_HEARTBEAT": HEARTBEAT,
            "AURA_REDIS_POLICY_AUDIT": AUDIT,
        },
    )
    # Either needle is enough for boot; wait_log needs all — poll manually.
    t0 = time.time()
    last = ""
    while time.time() - t0 < 12:
        last = _agent_logs(cid, AGENT_LOG)
        if "PING" in last or "PONG" in last:
            return cid
        time.sleep(0.15)
    raise TimeoutError(f"agent did not PING; log:\n{last}")


def drive_miss_spike(rounds: int = 50) -> None:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
        for i in range(24):
            redis_call(s, "SET", f"z{i:04d}", "H" * 160)
            for _ in range(3):
                redis_call(s, "GET", f"z{i:04d}")
        for k in range(rounds):
            redis_call(s, "SET", f"__miss{k}", "m" * 64)
            redis_call(s, "GET", f"__nope{k}")
        time.sleep(0.8)
        for i in range(24):
            redis_call(s, "SET", f"z{i:04d}", "H" * 160)
        for k in range(40):
            redis_call(s, "SET", f"__c{k}", "c" * 48)
            redis_call(s, "GET", f"__gone{k}")
        time.sleep(0.7)


LINE_RE = re.compile(
    r"ts=(?P<ts>\d+)\s+op=(?P<op>\S+)\s+from=(?P<frm>\S+)\s+"
    r"to=(?P<to>\S+)\s+reason=(?P<reason>\S+)\s+version=(?P<ver>\d+)"
)


def test_policy_audit_ring_and_explain() -> None:
    srv = start_server()
    cid = None
    try:
        cid = start_agent()
        drive_miss_spike()
        log = _agent_logs(cid, AGENT_LOG)
        assert "audit-file=" in log or AUDIT.exists(), f"no audit path in log:\n{log[-800:]}"

        # Wait for audit file
        for _ in range(40):
            if AUDIT.exists() and AUDIT.stat().st_size > 0:
                break
            time.sleep(0.1)
        assert AUDIT.exists(), f"missing audit file {AUDIT}; agent log:\n{log[-1200:]}"
        body = AUDIT.read_text(errors="replace")
        lines = [ln for ln in body.splitlines() if ln.strip().startswith("ts=")]
        assert lines, f"no audit lines in {AUDIT}:\n{body!r}\nlog:\n{log[-1200:]}"

        parsed = []
        for ln in lines:
            m = LINE_RE.search(ln)
            assert m, f"bad audit schema line: {ln!r}"
            parsed.append(m.groupdict())

        ops = {p["op"] for p in parsed}
        reasons = {p["reason"] for p in parsed}
        assert ops & {"fitness-swap", "evict", "evolve-keep", "heal"}, (
            f"expected known op in {ops}; lines={lines}"
        )
        known_reasons = {
            "miss_spike", "ewma_drop", "stuck", "evict_pressure", "fitness",
            "choose", "evolve_keep", "poison_profile", "still_bad_after_swap",
            "write_heavy", "read_heavy", "mixed", "unknown",
        }
        assert reasons & known_reasons or any(
            r.replace("-ttl", "") in known_reasons or "spike" in r or "fitness" in r
            for r in reasons
        ), f"unexpected reasons {reasons}"

        # Heartbeat explain surface (wait until audit_count advances)
        hb = ""
        for _ in range(50):
            if HEARTBEAT.exists():
                hb = HEARTBEAT.read_text(errors="replace")
                if "audit_count=" in hb:
                    try:
                        n = int(
                            next(
                                ln.split("=", 1)[1]
                                for ln in hb.splitlines()
                                if ln.startswith("audit_count=")
                            )
                        )
                    except StopIteration:
                        n = 0
                    if n > 0 and "last_audit_op=" in hb and hb.split("last_audit_op=")[1][:1] not in ("", "\n"):
                        break
            time.sleep(0.1)
        assert HEARTBEAT.exists(), "missing heartbeat file"
        hb = HEARTBEAT.read_text(errors="replace")
        for key in ("last_audit_op=", "last_audit_reason=", "last_explain=", "audit_count="):
            assert key in hb, f"heartbeat missing {key}:\n{hb}"
        assert "audit_count=0" not in hb.splitlines() and any(
            ln.startswith("audit_count=") and ln != "audit_count=0" for ln in hb.splitlines()
        ), f"heartbeat audit_count not advanced:\n{hb}"

        print(f"PASS test_policy_audit: {len(parsed)} audit lines ops={ops} reasons={reasons}")
        print(f"  heartbeat last_explain={dict(x.split('=',1) for x in hb.splitlines() if '=' in x).get('last_explain')}")
    finally:
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


if __name__ == "__main__":
    test_policy_audit_ring_and_explain()
