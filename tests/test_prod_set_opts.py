#!/usr/bin/env python3
"""SET NX/XX/EX/PX (+ SETNX/GETSET) — Redis-compatible cache option flags."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26996"))


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
    log = Path(f"/tmp/test-prod-set-opts-{PORT}.log")
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



def wait_gone(sock: socket.socket, key: str, timeout: float = 2.5) -> None:
    """Poll GET until missing; PING pumps active-expire (flake harden)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if call(sock, "GET", key) is None:
            return
        call(sock, "PING")
        time.sleep(0.05)
    raise AssertionError(f"{key} did not expire within {timeout}s")


def err_str(v) -> str:
    if isinstance(v, Exception):
        return str(v)
    return str(v)


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            assert call(sock, "FLUSHDB") == "OK"

            # --- NX ---
            assert call(sock, "SET", "nxk", "a", "NX") == "OK"
            assert call(sock, "GET", "nxk") == "a"
            assert call(sock, "SET", "nxk", "b", "NX") is None  # exists → null
            assert call(sock, "GET", "nxk") == "a"

            # --- XX ---
            assert call(sock, "SET", "xxmiss", "z", "XX") is None
            assert call(sock, "EXISTS", "xxmiss") == 0
            assert call(sock, "SET", "nxk", "c", "XX") == "OK"
            assert call(sock, "GET", "nxk") == "c"

            # --- EX ---
            assert call(sock, "SET", "exk", "v", "EX", "2") == "OK"
            ttl = call(sock, "TTL", "exk")
            assert ttl in (1, 2), ttl
            assert call(sock, "GET", "exk") == "v"

            # --- PX ---
            assert call(sock, "SET", "pxk", "pv", "PX", "250") == "OK"
            assert call(sock, "GET", "pxk") == "pv"
            wait_gone(sock, "pxk")

            # --- NX + EX / XX + PX ---
            assert call(sock, "SET", "combo", "1", "NX", "EX", "5") == "OK"
            assert call(sock, "SET", "combo", "2", "NX", "EX", "5") is None
            assert call(sock, "GET", "combo") == "1"
            assert call(sock, "SET", "combo", "3", "XX", "PX", "500") == "OK"
            assert call(sock, "GET", "combo") == "3"
            ttl2 = call(sock, "TTL", "combo")
            assert ttl2 in (0, 1), ttl2

            # option order: EX then NX
            assert call(sock, "DEL", "ord") in (0, 1)
            assert call(sock, "SET", "ord", "o", "EX", "3", "NX") == "OK"
            assert call(sock, "TTL", "ord") in (2, 3)

            # --- bad combos ---
            r = call(sock, "SET", "bad", "v", "NX", "XX")
            assert "syntax" in err_str(r).lower(), r
            r = call(sock, "SET", "bad", "v", "EX", "1", "PX", "100")
            assert "syntax" in err_str(r).lower(), r
            r = call(sock, "SET", "bad", "v", "EX")
            assert "syntax" in err_str(r).lower(), r
            r = call(sock, "SET", "bad", "v", "EX", "0")
            assert "invalid expire" in err_str(r).lower(), r
            r = call(sock, "SET", "bad", "v", "PX", "-1")
            assert "invalid expire" in err_str(r).lower(), r
            r = call(sock, "SET", "bad", "v", "KEEPTTL", "EX", "1")
            assert "syntax" in err_str(r).lower(), r

            # --- KEEPTTL / EXAT / PXAT ---
            assert call(sock, "SET", "kt", "1", "EX", "5") == "OK"
            assert call(sock, "SET", "kt", "2", "KEEPTTL") == "OK"
            assert call(sock, "GET", "kt") == "2"
            ttl_kt = call(sock, "TTL", "kt")
            assert isinstance(ttl_kt, int) and 1 <= ttl_kt <= 5, ttl_kt
            import time as _time
            now_ms = int(_time.time() * 1000)
            assert call(sock, "SET", "pxat", "v", "PXAT", str(now_ms + 2000)) == "OK"
            pttl = call(sock, "PTTL", "pxat")
            assert isinstance(pttl, int) and 500 <= pttl <= 2000, pttl
            now_sec = int(_time.time())
            assert call(sock, "SET", "exat", "v", "EXAT", str(now_sec + 3)) == "OK"
            ttl_ex = call(sock, "TTL", "exat")
            assert isinstance(ttl_ex, int) and 1 <= ttl_ex <= 3, ttl_ex

            # --- SET overwrites typed keys (Redis 7) ---
            assert call(sock, "HSET", "typed", "f", "1") == 1
            assert call(sock, "TYPE", "typed") == "hash"
            assert call(sock, "SET", "typed", "now-string") == "OK"
            assert call(sock, "TYPE", "typed") == "string"
            assert call(sock, "GET", "typed") == "now-string"

            assert call(sock, "LPUSH", "typedl", "x") == 1
            assert call(sock, "SET", "typedl", "s") == "OK"
            assert call(sock, "TYPE", "typedl") == "string"

            assert call(sock, "ZADD", "typedz", "1", "m") == 1
            assert call(sock, "SET", "typedz", "s") == "OK"
            assert call(sock, "TYPE", "typedz") == "string"

            # NX on existing typed key does NOT overwrite
            assert call(sock, "HSET", "hnx", "f", "1") == 1
            assert call(sock, "SET", "hnx", "s", "NX") is None
            assert call(sock, "TYPE", "hnx") == "hash"

            # XX on typed key overwrites to string
            assert call(sock, "SET", "hnx", "s", "XX") == "OK"
            assert call(sock, "TYPE", "hnx") == "string"

            # --- SETNX ---
            assert call(sock, "SETNX", "snx", "1") == 1
            assert call(sock, "SETNX", "snx", "2") == 0
            assert call(sock, "GET", "snx") == "1"

            # --- GETSET ---
            assert call(sock, "GETSET", "gsmiss", "new") is None
            assert call(sock, "GET", "gsmiss") == "new"
            assert call(sock, "GETSET", "gsmiss", "newer") == "new"
            assert call(sock, "GET", "gsmiss") == "newer"
            # GETSET clears TTL
            assert call(sock, "SET", "gsttl", "a", "EX", "10") == "OK"
            assert call(sock, "GETSET", "gsttl", "b") == "a"
            assert call(sock, "TTL", "gsttl") == -1
            # WRONGTYPE
            assert call(sock, "HSET", "gsh", "f", "v") == 1
            r = call(sock, "GETSET", "gsh", "x")
            assert "WRONGTYPE" in err_str(r), r

            # plain SET still clears TTL
            assert call(sock, "SET", "clr", "1", "EX", "10") == "OK"
            assert call(sock, "SET", "clr", "2") == "OK"
            assert call(sock, "TTL", "clr") == -1

            print("SET opts OK")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
