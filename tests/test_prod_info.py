#!/usr/bin/env python3
"""P0.6 — structured INFO sections; policy_agent-compatible flat keys."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26906"))

# Keys policy_agent.info-get / info-num rely on (must remain present).
POLICY_AGENT_KEYS = [
    "gets",
    "sets",
    "hits",
    "misses",
    "evicted",
    "expired",
    "keys",
    "keys_with_ttl",
    "avg_ttl_ms",
    "evict",
    "layout",
    "policy_hints",
]

SECTIONS = [
    "# Server",
    "# Clients",
    "# Memory",
    "# Stats",
    "# Keyspace",
    "# Persistence",
    "# Aura",
]


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def start_server() -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    bin_path = ROOT / "native/build/aura_redis_server"
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-info-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(bin_path),
            "--port",
            str(PORT),
            "--evict",
            "lru",
            "--maxmemory",
            "100000",
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(80):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def main() -> int:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            redis_call(sock, "SET", "a", "1")
            redis_call(sock, "GET", "a")
            redis_call(sock, "SET", "b", "2", "EX", "60")
            raw = redis_call(sock, "INFO")
            assert isinstance(raw, str), raw
            for sec in SECTIONS:
                assert sec in raw, f"missing section {sec} in:\n{raw}"
            m = info_map(raw)
            for k in POLICY_AGENT_KEYS:
                assert k in m, f"missing policy_agent key {k}; have {sorted(m)}"
            assert m["evict"] == "lru"
            assert m["layout"] in ("flat", "hot_cold")
            assert int(m["gets"]) >= 1
            assert int(m["sets"]) >= 2
            assert int(m["hits"]) >= 1
            assert int(m["keys"]) >= 2
            assert int(m["keys_with_ttl"]) >= 1
            assert int(m["used_memory"]) > 0
            assert int(m["maxmemory"]) == 100000
            assert "tcp_port" in m and int(m["tcp_port"]) == PORT
            assert m.get("protected_mode") in ("yes", "no")
            assert m.get("aof_enabled") == "0"
            assert "connected_clients" in m
            # Monotonic under load
            g0, h0 = int(m["gets"]), int(m["hits"])
            for _ in range(20):
                redis_call(sock, "GET", "a")
            m2 = info_map(redis_call(sock, "INFO"))
            assert int(m2["gets"]) >= g0 + 20
            assert int(m2["hits"]) >= h0 + 20
            print("INFO sections + policy_agent keys OK")
            print(f"  gets {g0}->{m2['gets']} hits {h0}->{m2['hits']}")
    finally:
        proc.send_signal(__import__("signal").SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    print("test_prod_info: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
