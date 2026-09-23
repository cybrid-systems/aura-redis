#!/usr/bin/env python3
"""STRLEN / SETEX / PSETEX / DBSIZE — Tier-2 string & meta ops."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26998"))


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
    log = Path(f"/tmp/test-prod-string-meta-{PORT}.log")
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


def err_str(v) -> str:
    if isinstance(v, Exception):
        return str(v)
    return str(v)


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            assert call(sock, "FLUSHDB") == "OK"
            assert call(sock, "DBSIZE") == 0

            # --- STRLEN ---
            assert call(sock, "STRLEN", "missing") == 0
            assert call(sock, "SET", "s", "hello") == "OK"
            assert call(sock, "STRLEN", "s") == 5
            assert call(sock, "SET", "empty", "") == "OK"
            assert call(sock, "STRLEN", "empty") == 0
            assert call(sock, "HSET", "h", "f", "1") == 1
            r = call(sock, "STRLEN", "h")
            assert "WRONGTYPE" in err_str(r), r
            r = call(sock, "STRLEN")
            assert "wrong number" in err_str(r).lower(), r

            # --- SETEX ---
            assert call(sock, "SETEX", "ex", "3", "vex") == "OK"
            assert call(sock, "GET", "ex") == "vex"
            ttl = call(sock, "TTL", "ex")
            assert ttl in (2, 3), ttl
            assert call(sock, "STRLEN", "ex") == 3
            # overwrite typed with string+TTL
            assert call(sock, "SETEX", "h", "5", "nowstr") == "OK"
            assert call(sock, "TYPE", "h") == "string"
            assert call(sock, "GET", "h") == "nowstr"
            r = call(sock, "SETEX", "bad", "0", "x")
            assert "invalid expire" in err_str(r).lower(), r
            r = call(sock, "SETEX", "bad", "-1", "x")
            assert "invalid expire" in err_str(r).lower(), r
            r = call(sock, "SETEX", "only", "1")
            assert "wrong number" in err_str(r).lower(), r

            # --- PSETEX ---
            assert call(sock, "PSETEX", "px", "250", "pv") == "OK"
            assert call(sock, "GET", "px") == "pv"
            time.sleep(0.35)
            assert call(sock, "GET", "px") is None
            r = call(sock, "PSETEX", "badpx", "0", "x")
            assert "invalid expire" in err_str(r).lower(), r

            # --- DBSIZE (string + typed, excludes expired) ---
            assert call(sock, "FLUSHDB") == "OK"
            assert call(sock, "DBSIZE") == 0
            assert call(sock, "MSET", "a", "1", "b", "2") == "OK"
            assert call(sock, "HSET", "hh", "k", "v") == 1
            assert call(sock, "LPUSH", "ll", "x") == 1
            assert call(sock, "ZADD", "zz", "1", "m") == 1
            assert call(sock, "DBSIZE") == 5
            assert call(sock, "PSETEX", "soon", "200", "t") == "OK"
            assert call(sock, "DBSIZE") == 6
            time.sleep(0.35)
            # DBSIZE purges expired on walk
            assert call(sock, "DBSIZE") == 5
            assert call(sock, "EXISTS", "soon") == 0
            assert call(sock, "DEL", "a", "b") == 2
            assert call(sock, "DBSIZE") == 3
            r = call(sock, "DBSIZE", "extra")
            assert "wrong number" in err_str(r).lower(), r

            print("test_prod_string_meta: OK")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
