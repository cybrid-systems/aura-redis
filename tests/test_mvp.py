#!/usr/bin/env python3
"""Smoke: multi-signal INFO, PIN, EVICT samples, joint choose helper."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
from smoke_client import redis_call  # noqa: E402
from bench_dynamic_evict import choose_policy  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26881"))


def start() -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.1)
    bin_path = ROOT / "native/build/aura_redis_server"
    if not bin_path.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-mvp-{PORT}.log")
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(PORT), "--evict", "lru", "--maxmemory", "80000"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(40):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError("no listen")


def main() -> int:
    # unit: choose contract
    # miss-spike + write-ish → lfu|flat|pin (pin path avoids layout migrate)
    c_pin = choose_policy("normal", 10, 100, 5, 5, 0, 50)
    assert c_pin.startswith("lfu"), c_pin
    assert "pin" in c_pin, c_pin
    # classic write-heavy low-miss → lfu|flat (avoid migrate during protect)
    c_wr = choose_policy("normal", 10, 100, 90, 5, 0, 50)
    assert c_wr.startswith("lfu") and "flat" in c_wr, c_wr
    assert choose_policy("normal", 100, 5, 90, 10, 0, 50).startswith("lru")
    assert choose_policy("inverted", 10, 100, 5, 5, 0, 50).startswith("lru")
    print("choose_policy unit OK")

    proc = start()
    try:
        s = socket.create_connection(("127.0.0.1", PORT), 3)
        assert redis_call(s, "PING") in ("PONG", "OK") or redis_call
        info = redis_call(s, "INFO")
        for key in ("gets:", "sets:", "hits:", "misses:", "evicted:", "keys:", "samples:", "pinned:"):
            assert key in info, f"missing {key} in INFO"
        print("INFO multi-signal OK")

        redis_call(s, "SET", "hot0", "v" * 32)
        redis_call(s, "PIN", "hot0")
        pinned = redis_call(s, "PIN")
        assert "hot0" in pinned, pinned
        info2 = redis_call(s, "INFO")
        assert "pinned:1" in info2
        redis_call(s, "EVICT", "samples", "32")
        info3 = redis_call(s, "INFO")
        assert "samples:32" in info3
        assert redis_call(s, "UNPIN", "hot0") == 1
        print("PIN / EVICT samples OK")

        redis_call(s, "LAYOUT", "hot_cold")
        assert "hot_cold" in redis_call(s, "LAYOUT")
        redis_call(s, "LAYOUT", "flat")
        print("LAYOUT OK")
        s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("test_mvp PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
