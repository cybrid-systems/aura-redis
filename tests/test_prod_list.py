#!/usr/bin/env python3
"""P3.16b — LIST commands + WRONGTYPE + memory."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import Incomplete, decode_one, encode_array  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26941"))


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    bin_path = ROOT / "native/build/aura_redis_server"
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-list-{PORT}.log")
    proc = subprocess.Popen(
        [str(bin_path), "--port", str(PORT), "--evict", "lru", "--maxmemory", "0"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(50):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def recv_one(sock, buf, timeout=5.0):
    sock.settimeout(timeout)
    while True:
        try:
            v, c = decode_one(buf)
            del buf[:c]
            return v
        except Incomplete:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed")
            buf.extend(chunk)


def call(sock, *args):
    sock.sendall(encode_array(list(args)))
    return recv_one(sock, bytearray())


def err_str(v):
    if isinstance(v, Exception):
        return str(v)
    raise AssertionError(f"expected error, got {v!r}")


def main():
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            assert call(sock, "RPUSH", "L", "a", "b", "c") == 3
            assert call(sock, "LLEN", "L") == 3
            assert call(sock, "LRANGE", "L", "0", "-1") == ["a", "b", "c"]
            assert call(sock, "LRANGE", "L", "1", "2") == ["b", "c"]
            assert call(sock, "LRANGE", "L", "-2", "-1") == ["b", "c"]
            assert call(sock, "LINDEX", "L", "0") == "a"
            assert call(sock, "LINDEX", "L", "-1") == "c"
            assert call(sock, "LINDEX", "L", "99") is None
            assert call(sock, "LPUSH", "L", "z") == 4
            assert call(sock, "LRANGE", "L", "0", "0") == ["z"]
            assert call(sock, "LPOP", "L") == "z"
            assert call(sock, "RPOP", "L") == "c"
            assert call(sock, "LLEN", "L") == 2
            assert call(sock, "TYPE", "L") == "list"

            assert call(sock, "SET", "s", "x") == "OK"
            assert "WRONGTYPE" in err_str(call(sock, "LPUSH", "s", "y"))
            assert "WRONGTYPE" in err_str(call(sock, "LLEN", "s"))

            assert call(sock, "DEL", "L") == 1
            assert call(sock, "LPOP", "L") is None
            assert call(sock, "LLEN", "L") == 0

            for i in range(100):
                call(sock, "RPUSH", "big", f"v{i}")
            assert call(sock, "LLEN", "big") == 100
            call(sock, "FLUSHDB")
            print("test_prod_list: OK")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


if __name__ == "__main__":
    main()
