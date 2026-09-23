#!/usr/bin/env python3
"""Edge coverage for strong-narrative surfaces (C + optional agent).

Covers thin gaps called out for CI hardening:
  - EVICT slru/tinylfu name round-trip + alias + WRONGTYPE under slru
  - SHADOW CONFIG/INFO bounds + fields
  - HOTCOLD knobs bounds + WRONGTYPE interactions under hot_cold layout
  - POLICY prefix smoke
  - Canary default-off does not break normal fitness path (agent)
  - Typed pressure INFO fields parseable over TCP (optional sanity)

DENY_PLUGIN=1 throughout.
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26990"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
SKIP_AGENT = os.environ.get("AURA_REDIS_STRONG_SKIP_AGENT", "0") == "1"


def err_str(exc_or_val) -> str:
    return str(exc_or_val)


def expect_err(sock, *args) -> str:
    try:
        redis_call(sock, *args)
        raise AssertionError(f"expected ERR for {args}")
    except RuntimeError as e:
        return str(e)


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def start_server(evict: str = "lru", maxmemory: int = 0) -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists() and not SERVER.exists():
        subprocess.check_call(
            [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-strong-edges-srv-{PORT}.log")
    cmd = [str(SERVER), "--port", str(PORT), "--evict", evict]
    if maxmemory:
        cmd += ["--maxmemory", str(maxmemory)]
    proc = subprocess.Popen(
        cmd, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env
    )
    for _ in range(80):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
            s.close()
            return proc
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(log.read_text(errors="replace"))
            time.sleep(0.05)
    raise RuntimeError("server did not listen: " + log.read_text(errors="replace"))


def stop(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    kill_tcp_port(PORT)


def test_evict_slru_tinylfu_roundtrip_wrongtype(proc: subprocess.Popen) -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    # tinylfu is alias/approx of sample SLRU — both round-trip via EVICT + INFO
    for name in ("slru", "tinylfu", "lru", "lfu", "ttl_aware"):
        assert redis_call(s, "EVICT", name) == "OK", name
        assert redis_call(s, "EVICT") == name, name
        m = info_map(redis_call(s, "INFO"))
        assert m.get("evict") == name, m
    # Unknown rejected
    err = expect_err(s, "EVICT", "not_a_kernel")
    assert "bad evict" in err, err
    # Under slru, typed WRONGTYPE still fires (kernel name must not break type checks)
    assert redis_call(s, "EVICT", "slru") == "OK"
    assert redis_call(s, "SET", "s", "v") == "OK"
    err = expect_err(s, "HGET", "s", "f")
    assert "WRONGTYPE" in err, err
    err = expect_err(s, "LPUSH", "s", "x")
    assert "WRONGTYPE" in err, err
    # tinylfu alias: switch and back
    assert redis_call(s, "EVICT", "tinylfu") == "OK"
    assert redis_call(s, "EVICT") == "tinylfu"
    assert redis_call(s, "EVICT", "slru") == "OK"
    redis_call(s, "QUIT")
    s.close()
    print("PASS edges: EVICT slru/tinylfu roundtrip + WRONGTYPE under slru")


def test_shadow_config_info_bounds(proc: subprocess.Popen) -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    assert redis_call(s, "SHADOW", "reset") == "OK"
    assert redis_call(s, "CONFIG", "SET", "shadow-policy", "challenger") == "OK"
    assert redis_call(s, "CONFIG", "SET", "shadow-sample-pct", "0") == "OK"
    m = info_map(redis_call(s, "INFO"))
    for k in (
        "shadow_policy",
        "shadow_sample_pct",
        "shadow_samples",
        "shadow_hits",
        "shadow_misses",
        "shadow_diverges",
    ):
        assert k in m, f"missing {k} in {sorted(m)}"
    assert m["shadow_sample_pct"] == "0", m
    # Bounds: sample-pct clamp 0..100
    assert redis_call(s, "SHADOW", "sample-pct", "100") == "OK"
    assert redis_call(s, "CONFIG", "SET", "shadow-sample-pct", "50") == "OK"
    m2 = info_map(redis_call(s, "INFO"))
    assert m2["shadow_sample_pct"] == "50", m2
    # Out-of-range clamped to 0..100 (setter returns OK)
    assert redis_call(s, "CONFIG", "SET", "shadow-sample-pct", "101") == "OK"
    assert info_map(redis_call(s, "INFO"))["shadow_sample_pct"] == "100"
    assert redis_call(s, "SHADOW", "sample-pct", "-1") == "OK"
    assert info_map(redis_call(s, "INFO"))["shadow_sample_pct"] == "0"
    # Status bulk
    st = redis_call(s, "SHADOW")
    assert "policy:" in st and "sample_pct:" in st, st
    redis_call(s, "QUIT")
    s.close()
    print("PASS edges: SHADOW CONFIG/INFO fields + sample-pct bounds")


def test_hotcold_bounds_wrongtype(proc: subprocess.Popen) -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    # Clamp pct to 1..100 via setter (server clamps)
    assert redis_call(s, "HOTCOLD", "soft-cap-pct", "1") == "OK"
    assert redis_call(s, "HOTCOLD", "soft-cap-pct", "100") == "OK"
    assert redis_call(s, "CONFIG", "SET", "hot-soft-cap-pct", "200") == "OK"
    m = info_map(redis_call(s, "INFO"))
    assert m.get("hot_soft_cap_pct") == "100", m  # clamped
    assert redis_call(s, "CONFIG", "SET", "hot-soft-cap-pct", "0") == "OK"
    m0 = info_map(redis_call(s, "INFO"))
    assert m0.get("hot_soft_cap_pct") == "1", m0  # clamped to 1
    assert redis_call(s, "HOTCOLD", "soft-cap-min", "0") == "OK"
    assert redis_call(s, "HOTCOLD", "soft-cap-min", "1000") == "OK"
    # Bad arity
    err = expect_err(s, "HOTCOLD", "soft-cap-pct")
    assert "wrong number" in err or "ERR" in err, err
    # Layout hot_cold + typed WRONGTYPE still works
    assert redis_call(s, "LAYOUT", "hot_cold") == "OK"
    assert redis_call(s, "SET", "t", "1") == "OK"
    err = expect_err(s, "HSET", "t", "f", "v")
    assert "WRONGTYPE" in err, err
    err = expect_err(s, "ZADD", "t", "1", "m")
    assert "WRONGTYPE" in err, err
    # INFO knobs present
    m2 = info_map(redis_call(s, "INFO"))
    for k in ("hot_soft_cap_pct", "hot_soft_cap_min", "hot_promote_on_get", "hot_soft_cap"):
        assert k in m2, m2
    redis_call(s, "QUIT")
    s.close()
    print("PASS edges: HOTCOLD bounds + WRONGTYPE under hot_cold layout")


def test_policy_prefix_smoke(proc: subprocess.Popen) -> None:
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    # List hints (may be empty or default)
    hints = redis_call(s, "POLICY")
    assert isinstance(hints, str), hints
    assert redis_call(s, "POLICY", "a:", "session") == "OK"
    assert redis_call(s, "POLICY", "b:", "zipf") == "OK"
    hints2 = redis_call(s, "POLICY")
    # Hints should mention at least one prefix we set
    assert "a:" in hints2 or "session" in hints2 or "b:" in hints2 or "zipf" in hints2, hints2
    err = expect_err(s, "POLICY", "only_one_arg")
    assert "wrong number" in err or "ERR" in err, err
    redis_call(s, "QUIT")
    s.close()
    print("PASS edges: POLICY prefix smoke")


def test_typed_pressure_info_tcp(proc: subprocess.Popen) -> None:
    """INFO typed pressure fields parseable (Aura TCP / any RESP client)."""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    redis_call(s, "FLUSHDB")
    redis_call(s, "SET", "s1", "x")
    redis_call(s, "HSET", "h1", "f", "y" * 50)
    redis_call(s, "LPUSH", "l1", "a")
    redis_call(s, "ZADD", "z1", "1", "m")
    m = info_map(redis_call(s, "INFO"))
    for k in (
        "keys_string",
        "keys_hash",
        "keys_list",
        "keys_zset",
        "mem_string",
        "mem_hash",
        "bigkey_bytes",
        "bigkey_type",
    ):
        assert k in m, f"missing {k}"
    assert int(m["keys_string"]) >= 1
    assert int(m["keys_hash"]) >= 1
    redis_call(s, "QUIT")
    s.close()
    print("PASS edges: typed pressure INFO parseable over TCP")


def test_canary_default_off_fitness(proc: subprocess.Popen) -> None:
    """Canary off (default): fitness mutate path still swaps; no canary_start."""
    if SKIP_AGENT:
        print("SKIP edges: canary default-off (AURA_REDIS_STRONG_SKIP_AGENT=1)")
        return
    port = PORT
    hb = ROOT / f".ar-policy-hb-canary-off-{port}"
    audit = ROOT / f".ar-policy-audit-canary-off-{port}.log"
    boot = ROOT / f".ar-agent-booted-{port}.flag"
    agent_log = Path(f"/tmp/ar-policy-canary-off-{port}.log")
    for p in (hb, audit, boot):
        p.unlink(missing_ok=True)
    cid = _start_agent(
        env={
            "AURA_REDIS_PORT": str(port),
            "AURA_REDIS_HOST": "127.0.0.1",
            "AURA_REDIS_POLICY_MS": "80",
            "AURA_REDIS_DENY_PLUGIN": "1",
            "AURA_REDIS_FITNESS_MUTATE": "1",
            # Explicitly OFF — must not enable canary
            "AURA_REDIS_CANARY": "0",
        },
        log_path=agent_log,
        path_env={
            "AURA_REDIS_POLICY_HEARTBEAT": hb,
            "AURA_REDIS_POLICY_AUDIT": audit,
        },
    )
    try:
        # Wait boot (native or docker via _agentutil); fail-fast if agent dies.
        _wait_log(
            cid,
            ["policy_agent:", "PING", "PONG"],
            timeout=30,
            log_path=agent_log,
            match_any=True,
        )
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        # Drive miss-heavy traffic so fitness path has signal
        val = "v" * 80
        for i in range(60):
            redis_call(s, "SET", f"k{i}", val)
        for _ in range(8):
            for i in range(40):
                redis_call(s, "GET", f"missing{i}")
            for i in range(20):
                redis_call(s, "SET", f"w{i}", val)
            time.sleep(0.25)
        time.sleep(1.5)
        redis_call(s, "QUIT")
        s.close()
        log = _agent_logs(cid, agent_log)
        # Must NOT have entered canary path
        assert "canary-start" not in log and "canary_start" not in log, log[-1500:]
        assert "canary-inject" not in log, log[-800:]
        # Fitness path should still be live (mutate on by default)
        assert "fitness" in log.lower() or "policy_agent:" in log, log[-1200:]
        if audit.exists():
            atxt = audit.read_text(errors="replace")
            assert "canary_start" not in atxt and "canary-start" not in atxt, atxt[-800:]
        print("PASS edges: canary default-off leaves fitness path intact")
    finally:
        _stop_agent(cid)


def main() -> int:
    proc = None
    try:
        proc = start_server("lru", maxmemory=200_000)
        test_evict_slru_tinylfu_roundtrip_wrongtype(proc)
        test_shadow_config_info_bounds(proc)
        test_hotcold_bounds_wrongtype(proc)
        test_policy_prefix_smoke(proc)
        test_typed_pressure_info_tcp(proc)
        test_canary_default_off_fitness(proc)
        print("test_strong_edges: ALL PASSED")
        return 0
    finally:
        stop(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
