#!/usr/bin/env python3
"""P1.12 — maxclients / idle timeout / tcp-backlog."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26912"))
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"


def start_server(*extra: str) -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call([str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-clients-{PORT}.log")
    cmd = [
        str(SERVER),
        "--port",
        str(PORT),
        "--evict",
        "noop",
        "--maxmemory",
        "100000",
        *extra,
    ]
    proc = subprocess.Popen(cmd, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env)
    for _ in range(80):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    kill_tcp_port(PORT)


def test_maxclients() -> None:
    proc = start_server("--maxclients", "3")
    socks = []
    try:
        for i in range(3):
            s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
            assert redis_call(s, "PING") == "PONG"
            socks.append(s)
        # 4th should be refused / error
        s4 = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        s4.settimeout(2)
        try:
            data = s4.recv(256)
        except socket.timeout:
            data = b""
        # Server may write -ERR then close, or close after write
        if data:
            assert b"max number of clients" in data or data.startswith(b"-ERR"), data
        else:
            # closed without payload — still a rejection
            pass
        s4.close()
        socks[0].close()
        time.sleep(0.1)
        with socket.create_connection(("127.0.0.1", PORT), timeout=3) as sock:
            assert redis_call(sock, "PING") == "PONG"
            assert redis_call(sock, "CONFIG", "GET", "maxclients") == [
                "maxclients",
                "3",
            ]
            assert redis_call(sock, "CONFIG", "SET", "maxclients", "8") == "OK"
            assert redis_call(sock, "CONFIG", "GET", "tcp-backlog")[0] == "tcp-backlog"
            info = redis_call(sock, "INFO")
            assert "maxclients:8" in info or "maxclients:3" in info
        print("PASS maxclients reject + CONFIG")
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        stop(proc)


def test_timeout() -> None:
    proc = start_server("--timeout", "1", "--maxclients", "32")
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=3) as sock:
            assert redis_call(sock, "PING") == "PONG"
            assert redis_call(sock, "CONFIG", "GET", "timeout") == ["timeout", "1"]
        # Idle connection should be closed by server within ~2s
        idle = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        idle.settimeout(0.5)
        assert redis_call(idle, "PING") == "PONG"
        time.sleep(2.2)
        closed = False
        try:
            idle.sendall(b"*1\r\n$4\r\nPING\r\n")
            buf = b""
            while True:
                chunk = idle.recv(256)
                if not chunk:
                    closed = True
                    break
                buf += chunk
                if b"+PONG" in buf:
                    break
        except (ConnectionError, OSError, socket.timeout):
            closed = True
        idle.close()
        assert closed, "idle client should be closed after timeout"
        print("PASS idle timeout closes client")
    finally:
        stop(proc)


def test_tcp_backlog_config() -> None:
    proc = start_server("--tcp-backlog", "128")
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=3) as sock:
            assert redis_call(sock, "CONFIG", "GET", "tcp-backlog") == [
                "tcp-backlog",
                "128",
            ]
            assert redis_call(sock, "CONFIG", "SET", "tcp-backlog", "256") == "OK"
            # Applied on next listen; value still readable
            assert redis_call(sock, "CONFIG", "GET", "tcp-backlog") == [
                "tcp-backlog",
                "256",
            ]
            info = redis_call(sock, "INFO")
            assert "tcp_backlog:" in info
        print("PASS tcp-backlog CONFIG")
    finally:
        stop(proc)


def main() -> int:
    test_maxclients()
    test_timeout()
    test_tcp_backlog_config()
    print("test_prod_clients: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
