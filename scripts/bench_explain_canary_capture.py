#!/usr/bin/env python3
"""E3/E4 capture — A17 provenance explain + A18 shadow→canary promote artifacts.

Not a Redis perf compare. Produces sanitized operator artifacts Redis lacks:
  - POLICY EXPLAIN / INFO explain_* / heartbeat mid join (A17)
  - shadow-autopromote → canary-start without full restart (A18)
  - Redis side: CONFIG GET maxmemory-policy only
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _agentutil import start_agent, stop_agent, agent_logs  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

SERVER = ROOT / "native/build/aura_redis_server"
IMG_REDIS = os.environ.get("REDIS_IMAGE", "redis:7-alpine")


def wait_listen(port: int, timeout: float = 10.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), 0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} not listening")


def info_map(info: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for ln in info.splitlines():
        if ":" in ln and not ln.startswith("#"):
            k, v = ln.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def start_aura(port: int, maxmemory: int = 200_000) -> subprocess.Popen:
    kill_tcp_port(port)
    if not SERVER.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/ar-diff-aura-{port}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(port), "--evict", "lru", "--maxmemory", str(maxmemory)],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    wait_listen(port)
    return proc


def drive_phase_shift(port: int, rounds: int = 40) -> None:
    """zipf-like hot → ws-like shift so shadow diverges from champ."""
    with socket.create_connection(("127.0.0.1", port), 5) as s:
        for i in range(20):
            redis_call(s, "SET", f"hot{i}", "H" * 80)
            for _ in range(5):
                redis_call(s, "GET", f"hot{i}")
        for k in range(rounds):
            redis_call(s, "SET", f"flash{k}", "f" * 48)
            redis_call(s, "GET", f"miss{k}")
        # shift working set
        for i in range(24):
            redis_call(s, "SET", f"B{i}", "B" * 64)
            redis_call(s, "GET", f"B{i}")
        time.sleep(0.5)


def capture_a17(port: int = 26680) -> Dict[str, Any]:
    proc = start_aura(port)
    hb = ROOT / f".ar-diff-explain-hb-{port}"
    audit = ROOT / f".ar-diff-explain-audit-{port}.log"
    boot = ROOT / f".ar-agent-booted-{port}.flag"
    pin = Path(str(hb) + ".pin")
    for p in (hb, audit, boot, pin):
        p.unlink(missing_ok=True)
    log = Path(f"/tmp/ar-diff-explain-agent-{port}.log")
    cid = None
    art: Dict[str, Any] = {"kind": "a17_explain"}
    try:
        cid = start_agent(
            env={
                "AURA_REDIS_PORT": str(port),
                "AURA_REDIS_HOST": "127.0.0.1",
                "AURA_REDIS_POLICY_MS": "80",
                "AURA_REDIS_DENY_PLUGIN": "1",
                "AURA_REDIS_FITNESS_MUTATE": "0",
                "AURA_REDIS_SEED_PROFILE": "normal",
                "AURA_REDIS_POISON_DEFENSE": "1",
                "AURA_REDIS_POISON_UNIQUE_RATE": "8",
            },
            log_path=log,
            path_env={
                "AURA_REDIS_POLICY_HEARTBEAT": hb,
                "AURA_REDIS_POLICY_AUDIT": audit,
            },
        )
        t0 = time.time()
        while time.time() - t0 < 15:
            if "policy_agent:" in agent_logs(cid, log) or boot.exists():
                break
            time.sleep(0.1)
        # Unique-SET storm → poison-defense audit → POLICY EXPLAIN mid join
        with socket.create_connection(("127.0.0.1", port), 5) as s:
            for i in range(16):
                redis_call(s, "SET", f"keep{i}", "K" * 64)
                redis_call(s, "GET", f"keep{i}")
            for wave in range(8):
                for i in range(80):
                    redis_call(s, "SET", f"poison{wave}_{i}", "p" * 48)
                time.sleep(0.4)
            time.sleep(1.2)
            try:
                redis_call(s, "QUIT")
            except Exception:
                pass
        time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", port), 5) as s:
            info = str(redis_call(s, "INFO"))
            expl = redis_call(s, "POLICY", "EXPLAIN")
            evict = redis_call(s, "EVICT")
            art["policy_explain"] = expl if isinstance(expl, str) else str(expl)
            m = info_map(info)
            art["info_explain"] = {
                k: m.get(k, "")
                for k in (
                    "explain_mid",
                    "explain_reason",
                    "explain_op",
                    "explain_evict",
                    "explain_layout",
                    "explain",
                    "evict",
                    "layout",
                    "unique_sets",
                )
            }
            art["evict"] = evict if isinstance(evict, str) else str(evict)
            # If C explain empty, still surface heartbeat last_explain (A17 join)
            try:
                redis_call(s, "QUIT")
            except Exception:
                pass
        art["heartbeat"] = hb.read_text(errors="replace")[-1200:] if hb.exists() else ""
        art["audit_tail"] = audit.read_text(errors="replace")[-1200:] if audit.exists() else ""
        art["agent_tail"] = "\n".join(agent_logs(cid, log).splitlines()[-30:])
        # sanitize host paths
        for k in ("heartbeat", "audit_tail", "agent_tail"):
            art[k] = art[k].replace(str(ROOT), "<repo>")
        print("A17 POLICY EXPLAIN:", art["policy_explain"])
        print("A17 INFO explain_*:", art["info_explain"])
        return art
    finally:
        if cid:
            stop_agent(cid)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        kill_tcp_port(port)


def capture_a18(port: int = 26681) -> Dict[str, Any]:
    proc = start_aura(port)
    hb = ROOT / f".ar-diff-canary-hb-{port}"
    audit = ROOT / f".ar-diff-canary-audit-{port}.log"
    boot = ROOT / f".ar-agent-booted-{port}.flag"
    pin = Path(str(hb) + ".pin")
    for p in (hb, audit, boot, pin):
        p.unlink(missing_ok=True)
    log = Path(f"/tmp/ar-diff-canary-agent-{port}.log")
    cid = None
    art: Dict[str, Any] = {"kind": "a18_shadow_canary", "autopromote": True}
    try:
        cid = start_agent(
            env={
                "AURA_REDIS_PORT": str(port),
                "AURA_REDIS_HOST": "127.0.0.1",
                "AURA_REDIS_POLICY_MS": "80",
                "AURA_REDIS_DENY_PLUGIN": "1",
                "AURA_REDIS_FITNESS_MUTATE": "1",
                "AURA_REDIS_SHADOW_AB": "1",
                "AURA_REDIS_SHADOW_PROFILE": "aggressive",
                "AURA_REDIS_SHADOW_SAMPLE_PCT": "50",
                "AURA_REDIS_CANARY": "1",
                "AURA_REDIS_CANARY_TICKS": "4",
                "AURA_REDIS_SHADOW_AUTOPROMOTE": "1",
                "AURA_REDIS_SEED_PROFILE": "normal",
            },
            log_path=log,
            path_env={
                "AURA_REDIS_POLICY_HEARTBEAT": hb,
                "AURA_REDIS_POLICY_AUDIT": audit,
            },
        )
        t0 = time.time()
        text = ""
        while time.time() - t0 < 18:
            text = agent_logs(cid, log)
            if "shadow-autopromote on" in text or "policy_agent:" in text:
                break
            time.sleep(0.1)
        art["boot_saw_autopromote"] = "shadow-autopromote on" in text
        saw = False
        deadline = time.time() + 20
        evict_before = ""
        with socket.create_connection(("127.0.0.1", port), 5) as s:
            evict_before = str(redis_call(s, "EVICT"))
        while time.time() < deadline:
            drive_phase_shift(port, 35)
            text = agent_logs(cid, log)
            if (
                "shadow-ab autopromote" in text
                or "canary-start" in text
                or "canary_start" in text
            ):
                saw = True
                break
            time.sleep(0.2)
        time.sleep(1.0)
        with socket.create_connection(("127.0.0.1", port), 5) as s:
            evict_after = str(redis_call(s, "EVICT"))
            info = info_map(str(redis_call(s, "INFO")))
            try:
                redis_call(s, "QUIT")
            except Exception:
                pass
        text = agent_logs(cid, log)
        art["saw_promote_or_canary"] = saw
        art["evict_before"] = evict_before
        art["evict_after"] = evict_after
        art["info_evict"] = info.get("evict", "")
        art["restart_required"] = False  # in-process; no server restart
        art["agent_lines"] = [
            ln
            for ln in text.splitlines()
            if any(
                x in ln
                for x in (
                    "shadow-ab",
                    "autopromote",
                    "canary",
                    "EVICT",
                    "poison",
                    "fitness",
                )
            )
        ][-40:]
        art["audit_tail"] = audit.read_text(errors="replace")[-800:] if audit.exists() else ""
        art["heartbeat"] = hb.read_text(errors="replace")[-800:] if hb.exists() else ""
        for k in ("audit_tail", "heartbeat"):
            art[k] = art[k].replace(str(ROOT), "<repo>")
        print("A18 saw_promote_or_canary=", saw, "evict", evict_before, "->", evict_after)
        return art
    finally:
        if cid:
            stop_agent(cid)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        kill_tcp_port(port)


def capture_redis_contrast(port: int = 26682) -> Dict[str, Any]:
    kill_tcp_port(port)
    name = f"ar-diff-redis-{port}-{os.getpid()}"
    cid = subprocess.check_output(
        [
            "sudo", "docker", "run", "-d", "--network", "host", "--name", name,
            IMG_REDIS,
            "redis-server", "--port", str(port), "--bind", "127.0.0.1",
            "--save", "", "--appendonly", "no",
            "--maxmemory-policy", "allkeys-lru",
        ],
        text=True,
    ).strip()
    try:
        wait_listen(port)
        with socket.create_connection(("127.0.0.1", port), 5) as s:
            pol = redis_call(s, "CONFIG", "GET", "maxmemory-policy")
            # Redis has no POLICY EXPLAIN
            art = {
                "kind": "redis_contrast",
                "config_maxmemory_policy": pol if isinstance(pol, (list, str)) else str(pol),
                "has_policy_explain": False,
                "has_shadow_canary": False,
                "has_in_process_codegen": False,
                "note": "Redis exposes CONFIG GET maxmemory-policy only; no mid join / canary / live choose-fn.",
            }
            try:
                redis_call(s, "QUIT")
            except Exception:
                pass
        print("Redis contrast:", art)
        return art
    finally:
        subprocess.run(["sudo", "docker", "rm", "-f", cid], capture_output=True)
        kill_tcp_port(port)


def main() -> int:
    os.environ["AURA_REDIS_DENY_PLUGIN"] = "1"
    out_dir = Path(os.environ.get("DIFF_OUT", "/tmp/aura-diff-vs-redis"))
    out_dir.mkdir(parents=True, exist_ok=True)
    a17 = capture_a17()
    (out_dir / "a17_explain.json").write_text(json.dumps(a17, indent=2))
    a18 = capture_a18()
    (out_dir / "a18_canary.json").write_text(json.dumps(a18, indent=2))
    redis_c = capture_redis_contrast()
    (out_dir / "redis_contrast.json").write_text(json.dumps(redis_c, indent=2))
    print(json.dumps({"summary": "ok", "out": str(out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
