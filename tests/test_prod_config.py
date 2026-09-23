#!/usr/bin/env python3
"""P1.1 — CONFIG GET/SET runtime knobs."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26908"))


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-config-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(ROOT / "native/build/aura_redis_server"),
            "--port",
            str(PORT),
            "--evict",
            "lru",
            "--maxmemory",
            "50000",
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
            got = redis_call(sock, "CONFIG", "GET", "maxmemory")
            assert got == ["maxmemory", "50000"], got
            assert redis_call(sock, "CONFIG", "SET", "maxmemory", "80000") == "OK"
            assert redis_call(sock, "CONFIG", "GET", "maxmemory") == [
                "maxmemory",
                "80000",
            ]
            assert redis_call(sock, "CONFIG", "SET", "evict-samples", "32") == "OK"
            assert redis_call(sock, "CONFIG", "GET", "evict-samples") == [
                "evict-samples",
                "32",
            ]
            allc = redis_call(sock, "CONFIG", "GET", "*")
            assert isinstance(allc, list) and len(allc) >= 8 and len(allc) % 2 == 0
            d = dict(zip(allc[0::2], allc[1::2]))
            assert "maxmemory" in d and "bind" in d and "protected-mode" in d
            assert redis_call(sock, "CONFIG", "SET", "requirepass", "cfgpass") == "OK"
            # Current connection already authenticated (no pass at connect)
            # New connection must AUTH
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            try:
                redis_call(sock, "GET", "x")
                raise AssertionError("expected NOAUTH after CONFIG SET requirepass")
            except RuntimeError as e:
                assert "NOAUTH" in str(e), e
            assert redis_call(sock, "AUTH", "cfgpass") == "OK"
            assert redis_call(sock, "PING") == "PONG"
            assert redis_call(sock, "CONFIG", "SET", "requirepass", "") == "OK"
        print("CONFIG GET/SET OK")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        kill_tcp_port(PORT)
    print("test_prod_config: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
