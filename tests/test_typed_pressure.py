#!/usr/bin/env python3
"""A7 — typed pressure signals in INFO + policy_agent choose reaction.

Exit:
  - INFO exposes keys_{string,hash,list,zset} / mem_* / bigkey_*
  - HASH flood + hot string set: adaptive with type signals protects prot*
    better than fixed LFU (pinned via typed-pressure → lfu|flat|pin)
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
from _portutil import kill_tcp_port  # noqa: E402
from _agentutil import (  # noqa: E402
    agent_logs as _agent_logs,
    start_agent as _start_agent,
    stop_agent as _stop_agent,
)

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26983"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
BOOT = ROOT / f".ar-agent-booted-{PORT}.flag"
AGENT_LOG = Path(f"/tmp/ar-policy-typed-pressure-{PORT}.log")


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def start_server(maxmemory: str = "180000") -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call(
            [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-typed-pressure-srv-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(SERVER),
            "--port",
            str(PORT),
            "--evict",
            "lfu",
            "--maxmemory",
            maxmemory,
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(60):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
            s.close()
            return proc
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not listen: " + log.read_text(errors="replace"))


def test_c_type_info() -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    redis_call(s, "FLUSHDB")
    redis_call(s, "SET", "s1", "hello")
    redis_call(s, "SET", "s2", "world")
    redis_call(s, "HSET", "h1", "f1", "v" * 200, "f2", "w" * 200)
    redis_call(s, "LPUSH", "l1", "a", "b", "c")
    redis_call(s, "ZADD", "z1", "1", "m1", "2", "m2")
    m = info_map(redis_call(s, "INFO"))
    for k in (
        "keys_string",
        "keys_hash",
        "keys_list",
        "keys_zset",
        "mem_string",
        "mem_hash",
        "mem_list",
        "mem_zset",
        "bigkey_bytes",
        "bigkey_type",
    ):
        assert k in m, f"missing {k} in {sorted(m)}"
    assert int(m["keys_string"]) >= 2, m
    assert int(m["keys_hash"]) >= 1, m
    assert int(m["keys_list"]) >= 1, m
    assert int(m["keys_zset"]) >= 1, m
    assert int(m["mem_hash"]) > 0, m
    assert int(m["bigkey_bytes"]) > 0, m
    assert m["bigkey_type"] in ("string", "hash", "list", "zset"), m
    redis_call(s, "QUIT")
    s.close()
    print("PASS A7 C: INFO typed pressure shares/counts/bigkey")


def start_agent() -> str:
    BOOT.unlink(missing_ok=True)
    (ROOT / f".ar-policy-pin-{PORT}.pin").unlink(missing_ok=True)
    hb_path = ROOT / f".ar-typed-hb-{PORT}.hb"
    hb_path.unlink(missing_ok=True)
    cid = _start_agent(
        env={
            "AURA_REDIS_PORT": str(PORT),
            "AURA_REDIS_HOST": "127.0.0.1",
            "AURA_REDIS_POLICY_MS": "80",
            "AURA_REDIS_DENY_PLUGIN": "1",
            "AURA_REDIS_FITNESS_MUTATE": "0",
            "AURA_REDIS_TYPED_PRESSURE": "1",
            "AURA_REDIS_POLICY_PROFILE": "normal",
        },
        log_path=AGENT_LOG,
        path_env={"AURA_REDIS_POLICY_HEARTBEAT": hb_path},
    )
    t0 = time.time()
    last = ""
    while time.time() - t0 < 18:
        last = _agent_logs(cid, AGENT_LOG)
        if "policy_agent:" in last or "PING" in last or BOOT.exists():
            return cid
        time.sleep(0.15)
    _stop_agent(cid)
    raise RuntimeError("agent boot timeout:\n" + last[-2000:])


def stop_agent(cid: str) -> None:
    _stop_agent(cid)


def prot_hit_rate(s: socket.socket, n: int = 20) -> float:
    hits = 0
    for i in range(n):
        if redis_call(s, "GET", f"prot{i:04d}") is not None:
            hits += 1
    return hits / n


def run_flood(s: socket.socket) -> None:
    """HASH flood + hot protected strings under maxmemory."""
    for i in range(20):
        redis_call(s, "SET", f"prot{i:04d}", "P" * 40)
    for _ in range(12):
        for i in range(20):
            redis_call(s, "GET", f"prot{i:04d}")
    # Large HASH flood to pressure memory / eviction
    blob = "H" * 120
    for i in range(120):
        fields = []
        for f in range(10):
            fields.extend([f"f{f}", blob])
        redis_call(s, "HSET", f"blob{i:04d}", *fields)
    # Drive miss/write so choose fires
    for k in range(30):
        redis_call(s, "SET", f"__tw{k}", "w" * 30)
        redis_call(s, "GET", f"__tm{k}")


def test_adaptive_protects() -> None:
    """Adaptive typed-pressure should beat fixed LFU on protected string set."""
    # --- fixed LFU baseline (no agent) ---
    proc = start_server("120000")
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        run_flood(s)
        time.sleep(0.2)
        fixed = prot_hit_rate(s)
        m = info_map(redis_call(s, "INFO"))
        assert int(m.get("keys_hash", "0")) > 0, m
        redis_call(s, "QUIT")
        s.close()
    finally:
        proc.send_signal(__import__("signal").SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    # --- adaptive with typed pressure ---
    proc = start_server("120000")
    cid = None
    try:
        cid = start_agent()
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        run_flood(s)
        time.sleep(0.9)  # agent ticks see type shares + PIN
        # re-assert prot keys after agent PIN
        for i in range(20):
            redis_call(s, "SET", f"prot{i:04d}", "P" * 40)
        time.sleep(0.5)
        for i in range(40):
            redis_call(s, "HSET", f"more{i:04d}", "f", "X" * 200)
        time.sleep(0.4)
        adaptive = prot_hit_rate(s)
        log = _agent_logs(cid, AGENT_LOG)
        assert "typed-pressure" in log, log[-2500:]
        m = info_map(redis_call(s, "INFO"))
        assert int(m.get("pinned", "0")) >= 1 or "PIN prot" in log, (m, log[-1500:])
        redis_call(s, "QUIT")
        s.close()
        print(
            f"PASS A7 adaptive: prot hit fixed={100*fixed:.0f}% "
            f"adaptive={100*adaptive:.0f}% (need adaptive >= fixed)"
        )
        # Allow equal if both perfect; otherwise adaptive must not lose
        if "typed-pressure" not in log and "PIN prot" not in log:
            raise AssertionError("missing typed-pressure/PIN prot evidence:\n" + log[-2000:])
        if adaptive + 1e-9 < fixed:
            raise AssertionError(
                f"adaptive prot hit {adaptive} < fixed {fixed}; log=\n{log[-2000:]}"
            )
        if adaptive < 0.6:
            raise AssertionError(
                f"adaptive prot hit too low {adaptive}; log=\n{log[-2000:]}"
            )
        # When fixed loses keys, adaptive must show a clear edge
        if fixed < 0.95 and (adaptive - fixed) < 0.05:
            raise AssertionError(
                f"expected adaptive edge when fixed loses; fixed={fixed} adaptive={adaptive}"
            )
    finally:
        if cid:
            stop_agent(cid)
        proc.send_signal(__import__("signal").SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        kill_tcp_port(PORT)


def main() -> int:
    proc = start_server()
    try:
        test_c_type_info()
    finally:
        proc.send_signal(__import__("signal").SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    test_adaptive_protects()
    print("test_typed_pressure: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
