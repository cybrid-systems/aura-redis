#!/usr/bin/env python3
"""Tier-2 ZSET hard size bar (AR_ZSET_MAX_MEMBERS=4096)."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "27022"))


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    bin_path = ROOT / "native/build/aura_redis_server"
    if not bin_path.exists() or os.environ.get("AURA_REDIS_SKIP_BUILD") != "1":
        subprocess.check_call(
            [str(ROOT / "scripts/build-native.sh")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-zset-cap-{PORT}.log")
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


def call(sock: socket.socket, *args: str):
    sock.sendall(encode_array(list(args)))
    buf = bytearray()
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


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            n = 0
            while n < 4096:
                batch: list[str] = []
                for i in range(n, min(n + 30, 4096)):
                    batch.extend([str(i), f"m{i}"])
                r = call(sock, *(["ZADD", "z"] + batch))
                assert isinstance(r, int), r
                n += 30
            assert call(sock, "ZCARD", "z") == 4096
            r = call(sock, "ZADD", "z", "1", "overflow")
            assert isinstance(r, Exception) and "max members" in str(r).lower(), r
            assert call(sock, "ZADD", "z", "99", "m0") == 0
            print("PASS ZSET max members cap")
            print("test_prod_zset_cap: ALL PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        kill_tcp_port(PORT)


if __name__ == "__main__":
    main()
