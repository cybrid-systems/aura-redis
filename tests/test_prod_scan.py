#!/usr/bin/env python3
"""Tier-2 SCAN / KEYS — empty DB, pagination, MATCH, COUNT, mixed types, after DEL."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26995"))


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
    log = Path(f"/tmp/test-prod-scan-{PORT}.log")
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


def recv_one(sock: socket.socket, buf: bytearray, timeout: float = 5.0):
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


def call(sock: socket.socket, *args: str):
    sock.sendall(encode_array(list(args)))
    return recv_one(sock, bytearray())


def scan_all(sock: socket.socket, match: str | None = None, count: int | None = None):
    cursor = "0"
    seen: list[str] = []
    rounds = 0
    while True:
        args = ["SCAN", cursor]
        if match is not None:
            args += ["MATCH", match]
        if count is not None:
            args += ["COUNT", str(count)]
        reply = call(sock, *args)
        assert isinstance(reply, list) and len(reply) == 2, reply
        cursor, keys = reply[0], reply[1]
        assert isinstance(cursor, str)
        assert isinstance(keys, list)
        seen.extend(keys)
        rounds += 1
        if cursor == "0":
            break
        assert rounds < 10000, "SCAN did not terminate"
    return seen, rounds


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            # empty DB
            r = call(sock, "SCAN", "0")
            assert r == ["0", []], r
            assert call(sock, "KEYS", "*") == []

            # seed mixed types
            assert call(sock, "SET", "s:a", "1") == "OK"
            assert call(sock, "SET", "s:b", "2") == "OK"
            assert call(sock, "SET", "other", "x") == "OK"
            assert call(sock, "HSET", "h:1", "f", "v") == 1
            assert call(sock, "LPUSH", "l:1", "x") == 1
            assert call(sock, "ZADD", "z:1", "1", "m") == 1

            all_keys, rounds = scan_all(sock, count=1)
            assert rounds >= 1
            assert sorted(all_keys) == sorted(["s:a", "s:b", "other", "h:1", "l:1", "z:1"]), all_keys

            # MATCH prefix
            matched, _ = scan_all(sock, match="s:*", count=2)
            assert sorted(matched) == ["s:a", "s:b"], matched

            # MATCH ?
            matched2, _ = scan_all(sock, match="?:1", count=10)
            assert sorted(matched2) == ["h:1", "l:1", "z:1"], matched2

            # KEYS full + glob
            keys_all = call(sock, "KEYS", "*")
            assert sorted(keys_all) == sorted(all_keys), keys_all
            assert sorted(call(sock, "KEYS", "s:*")) == ["s:a", "s:b"]

            # after DEL
            assert call(sock, "DEL", "s:a", "h:1") == 2
            left, _ = scan_all(sock, count=5)
            assert sorted(left) == sorted(["s:b", "other", "l:1", "z:1"]), left
            assert "s:a" not in left and "h:1" not in left

            # FLUSH then empty again
            assert call(sock, "FLUSHDB") == "OK"
            assert call(sock, "SCAN", "0") == ["0", []]
            assert call(sock, "KEYS", "*") == []

            # arity / syntax errors
            err = call(sock, "SCAN")
            assert isinstance(err, Exception) and "wrong number" in str(err).lower()
            err = call(sock, "SCAN", "0", "NOPE")
            assert isinstance(err, Exception) and "syntax" in str(err).lower()
            err = call(sock, "KEYS")
            assert isinstance(err, Exception) and "wrong number" in str(err).lower()

            print("test_prod_scan: OK")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
