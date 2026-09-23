#!/usr/bin/env python3
"""Ops — CONFIG persist across restart (SET auto-rewrite + load on boot)."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26928"))
BIN = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"


def build() -> None:
    if os.environ.get("AURA_REDIS_SKIP_BUILD") == "1":
        return
    subprocess.check_call(
        [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
    )


def start_server(cfg: Path, *extra: str) -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-config-persist-{PORT}.log")
    cmd = [
        str(BIN),
        "--port",
        str(PORT),
        "--evict",
        "lru",
        "--maxmemory",
        "50000",
        "--config",
        str(cfg),
        *extra,
    ]
    proc = subprocess.Popen(
        cmd, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env
    )
    for _ in range(100):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        kill_tcp_port(PORT)
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    kill_tcp_port(PORT)


def test_set_rewrite_restart() -> None:
    tdir = Path(tempfile.mkdtemp(prefix="aura-cfg-"))
    cfg = tdir / "aura-redis.conf"
    build()
    proc = start_server(cfg)
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "CONFIG", "GET", "config-file") == [
                "config-file",
                str(cfg),
            ]
            assert redis_call(sock, "CONFIG", "SET", "maxmemory", "90000") == "OK"
            assert redis_call(sock, "CONFIG", "SET", "timeout", "7") == "OK"
            assert redis_call(sock, "CONFIG", "SET", "maxclients", "42") == "OK"
            assert redis_call(sock, "CONFIG", "SET", "evict-samples", "24") == "OK"
            assert redis_call(sock, "CONFIG", "SET", "requirepass", "persist-pw") == "OK"
            # AUTH on this conn still ok (already authenticated before pass set)
            assert redis_call(sock, "CONFIG", "GET", "maxmemory") == [
                "maxmemory",
                "90000",
            ]
        assert cfg.is_file(), "CONFIG SET should auto-rewrite file"
        body = cfg.read_text()
        assert "maxmemory 90000" in body
        assert "timeout 7" in body
        assert "maxclients 42" in body
        assert "evict-samples 24" in body
        assert "requirepass persist-pw" in body
    finally:
        stop(proc)

    # Restart with --config only (no CLI maxmemory/timeout/…) so file sticks.
    # Precedence: defaults → config file → CLI/env (CLI wins).
    kill_tcp_port(PORT)
    time.sleep(0.05)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-config-persist-reload-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(BIN),
            "--port",
            str(PORT),
            "--evict",
            "lru",
            "--config",
            str(cfg),
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        for _ in range(100):
            if "listening on" in log.read_text(errors="replace"):
                break
            if proc.poll() is not None:
                raise RuntimeError(log.read_text())
            time.sleep(0.05)
        else:
            raise TimeoutError(log.read_text())
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            # PING is allowed pre-AUTH; GET must NOAUTH when requirepass loaded
            try:
                redis_call(sock, "GET", "x")
                raise AssertionError("expected NOAUTH after reload with requirepass")
            except RuntimeError as e:
                assert "NOAUTH" in str(e), e
            assert redis_call(sock, "AUTH", "persist-pw") == "OK"
            assert redis_call(sock, "CONFIG", "GET", "maxmemory") == [
                "maxmemory",
                "90000",
            ]
            assert redis_call(sock, "CONFIG", "GET", "timeout") == ["timeout", "7"]
            assert redis_call(sock, "CONFIG", "GET", "maxclients") == [
                "maxclients",
                "42",
            ]
            assert redis_call(sock, "CONFIG", "GET", "evict-samples") == [
                "evict-samples",
                "24",
            ]
            # Explicit REWRITE still works
            assert redis_call(sock, "CONFIG", "SET", "timeout", "9") == "OK"
            assert redis_call(sock, "CONFIG", "REWRITE") == "OK"
        body2 = cfg.read_text()
        assert "timeout 9" in body2
        print("PASS CONFIG persist set→rewrite→restart")
    finally:
        stop(proc)


def test_rewrite_disabled_without_path() -> None:
    build()
    kill_tcp_port(PORT)
    time.sleep(0.05)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    # Explicit empty disables even if a file exists in cwd
    env["AURA_REDIS_CONFIG"] = ""
    log = Path(f"/tmp/test-prod-config-persist-off-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(BIN),
            "--port",
            str(PORT),
            "--evict",
            "noop",
            "--config",
            "",
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        for _ in range(100):
            if "listening on" in log.read_text(errors="replace"):
                break
            if proc.poll() is not None:
                raise RuntimeError(log.read_text())
            time.sleep(0.05)
        else:
            raise TimeoutError(log.read_text())
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "CONFIG", "GET", "config-file") == [
                "config-file",
                "",
            ]
            assert redis_call(sock, "CONFIG", "SET", "maxmemory", "12345") == "OK"
            try:
                redis_call(sock, "CONFIG", "REWRITE")
                raise AssertionError("expected REWRITE error when disabled")
            except RuntimeError as e:
                assert "disabled" in str(e).lower() or "REWRITE" in str(e), e
        print("PASS CONFIG REWRITE disabled without path")
    finally:
        stop(proc)


def main() -> int:
    test_set_rewrite_restart()
    test_rewrite_disabled_without_path()
    print("test_prod_config_persist: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
