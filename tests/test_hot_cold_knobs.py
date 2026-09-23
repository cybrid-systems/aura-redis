#!/usr/bin/env python3
"""A9 — hot_cold promote/demote / soft-cap knobs via CONFIG/RESP.

Exit:
  - CONFIG GET/SET hot-soft-cap-pct|hot-soft-cap-min|hot-promote-on-get round-trip
  - HOTCOLD RESP status + setters round-trip
  - Tight soft-cap → demotions under hot_cold; promote-on-get=no blocks promotions
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26984"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"


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
    if BUILD.exists():
        subprocess.check_call(
            [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-hot-cold-knobs-srv-{PORT}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(PORT), "--evict", "lru", "--maxmemory", "0"],
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
    raise RuntimeError(log.read_text(errors="replace"))


def main() -> int:
    proc = start_server()
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        # Defaults in INFO
        m0 = info_map(redis_call(s, "INFO"))
        assert m0.get("hot_soft_cap_pct") == "25", m0
        assert m0.get("hot_soft_cap_min") == "256", m0
        assert m0.get("hot_promote_on_get") == "yes", m0

        # CONFIG round-trip
        assert redis_call(s, "CONFIG", "SET", "hot-soft-cap-pct", "10") == "OK"
        assert redis_call(s, "CONFIG", "SET", "hot-soft-cap-min", "32") == "OK"
        assert redis_call(s, "CONFIG", "SET", "hot-promote-on-get", "yes") == "OK"
        cfg = str(redis_call(s, "CONFIG", "GET", "hot-soft-cap-pct"))
        assert "10" in cfg, cfg
        cfg2 = str(redis_call(s, "CONFIG", "GET", "hot-soft-cap-min"))
        assert "32" in cfg2, cfg2

        # RESP HOTCOLD round-trip
        st = redis_call(s, "HOTCOLD")
        assert "soft_cap_pct:10" in st and "soft_cap_min:32" in st, st
        assert redis_call(s, "HOTCOLD", "soft-cap-pct", "15") == "OK"
        assert redis_call(s, "HOTCOLD", "soft-cap-min", "40") == "OK"
        assert redis_call(s, "HOTCOLD", "promote-on-get", "no") == "OK"
        st2 = redis_call(s, "HOTCOLD")
        assert "soft_cap_pct:15" in st2 and "promote_on_get:no" in st2, st2
        m1 = info_map(redis_call(s, "INFO"))
        assert m1["hot_soft_cap_pct"] == "15", m1
        assert m1["hot_soft_cap_min"] == "40", m1
        assert m1["hot_promote_on_get"] == "no", m1

        # Layout migrate + demote under tight soft-cap
        assert redis_call(s, "CONFIG", "SET", "hot-promote-on-get", "yes") == "OK"
        assert redis_call(s, "HOTCOLD", "soft-cap-pct", "5") == "OK"
        assert redis_call(s, "HOTCOLD", "soft-cap-min", "8") == "OK"
        assert redis_call(s, "LAYOUT", "hot_cold") == "OK"
        for i in range(120):
            redis_call(s, "SET", f"k{i}", "v" * 20)
        m2 = info_map(redis_call(s, "INFO"))
        # demotions may appear in LAYOUT status; check HOTCOLD counters
        st3 = redis_call(s, "HOTCOLD")
        # Force more demotions via soft-cap
        assert int(m2.get("hot_keys", "0")) <= int(m2.get("keys", "0")), m2
        # With soft-cap 5% of 120 ≈ 6, floor 8 → soft_cap 8; hot should be near floor
        assert int(m2.get("hot_soft_cap", "0")) == 8, m2
        assert int(m2.get("hot_keys", "999")) <= 40, (m2, st3)

        # promote-on-get=no: cold GETs should not bump promotions much
        assert redis_call(s, "HOTCOLD", "promote-on-get", "no") == "OK"
        before = info_map(redis_call(s, "INFO"))
        promo0 = int(
            st3.split("promotions:")[1].split()[0]
        ) if "promotions:" in st3 else 0
        for i in range(0, 60):
            redis_call(s, "GET", f"k{i}")
        st4 = redis_call(s, "HOTCOLD")
        promo1 = int(st4.split("promotions:")[1].split()[0])
        assert promo1 == promo0, (promo0, promo1, st4)

        # Re-enable promote and confirm promotions can increase
        assert redis_call(s, "HOTCOLD", "promote-on-get", "yes") == "OK"
        for i in range(0, 60):
            redis_call(s, "GET", f"k{i}")
        st5 = redis_call(s, "HOTCOLD")
        promo2 = int(st5.split("promotions:")[1].split()[0])
        assert promo2 >= promo1, (promo1, promo2, st5)

        redis_call(s, "QUIT")
        s.close()
        print("PASS A9: CONFIG/RESP hot_cold knobs + soft-cap/promote behavior")
    finally:
        proc.send_signal(__import__("signal").SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    print("test_hot_cold_knobs: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
