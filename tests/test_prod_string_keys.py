#!/usr/bin/env python3
"""APPEND / RENAME / RENAMENX / UNLINK — Tier-2 string & key ops."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26997"))


def start_server() -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
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
    log = Path(f"/tmp/test-prod-string-keys-{PORT}.log")
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

            # --- APPEND ---
            assert call(sock, "APPEND", "a", "hello") == 5
            assert call(sock, "GET", "a") == "hello"
            assert call(sock, "APPEND", "a", " world") == 11
            assert call(sock, "GET", "a") == "hello world"
            assert call(sock, "APPEND", "a", "") == 11
            # create empty then append
            assert call(sock, "APPEND", "empty", "") == 0
            assert call(sock, "APPEND", "empty", "x") == 1
            # TTL preserved across APPEND
            assert call(sock, "SET", "ttl", "v", "EX", "5") == "OK"
            assert call(sock, "APPEND", "ttl", "!") == 2
            t = call(sock, "TTL", "ttl")
            assert t in (4, 5), t
            assert call(sock, "GET", "ttl") == "v!"
            # WRONGTYPE on hash
            assert call(sock, "HSET", "h", "f", "1") == 1
            r = call(sock, "APPEND", "h", "x")
            assert "WRONGTYPE" in err_str(r), r

            # --- RENAME ---
            assert call(sock, "SET", "src", "val") == "OK"
            assert call(sock, "RENAME", "src", "dst") == "OK"
            assert call(sock, "EXISTS", "src") == 0
            assert call(sock, "GET", "dst") == "val"
            # missing source
            r = call(sock, "RENAME", "nosuch", "anywhere")
            assert "no such key" in err_str(r).lower(), r
            # overwrite destination
            assert call(sock, "SET", "old", "a") == "OK"
            assert call(sock, "SET", "new", "b") == "OK"
            assert call(sock, "RENAME", "old", "new") == "OK"
            assert call(sock, "GET", "new") == "a"
            assert call(sock, "EXISTS", "old") == 0
            # same key → OK
            assert call(sock, "RENAME", "new", "new") == "OK"
            assert call(sock, "GET", "new") == "a"
            # rename typed value (hash)
            assert call(sock, "HSET", "hs", "k", "v") == 1
            assert call(sock, "RENAME", "hs", "hs2") == "OK"
            assert call(sock, "TYPE", "hs2") == "hash"
            assert call(sock, "HGET", "hs2", "k") == "v"
            assert call(sock, "EXISTS", "hs") == 0
            # rename list / zset
            assert call(sock, "LPUSH", "ls", "x") == 1
            assert call(sock, "RENAME", "ls", "ls2") == "OK"
            assert call(sock, "LINDEX", "ls2", "0") == "x"
            assert call(sock, "ZADD", "zs", "1", "m") == 1
            assert call(sock, "RENAME", "zs", "zs2") == "OK"
            assert call(sock, "ZSCORE", "zs2", "m") == "1"
            # TTL preserved
            assert call(sock, "SET", "tex", "t", "EX", "8") == "OK"
            assert call(sock, "RENAME", "tex", "tex2") == "OK"
            t2 = call(sock, "TTL", "tex2")
            assert t2 in (7, 8), t2

            # --- RENAMENX ---
            assert call(sock, "SET", "nx1", "1") == "OK"
            assert call(sock, "SET", "nx2", "2") == "OK"
            assert call(sock, "RENAMENX", "nx1", "nx2") == 0  # dest exists
            assert call(sock, "GET", "nx1") == "1"
            assert call(sock, "GET", "nx2") == "2"
            assert call(sock, "RENAMENX", "nx1", "nx3") == 1
            assert call(sock, "GET", "nx3") == "1"
            assert call(sock, "EXISTS", "nx1") == 0
            r = call(sock, "RENAMENX", "gone", "g2")
            assert "no such key" in err_str(r).lower(), r

            # --- UNLINK (DEL-equivalent multi-key count) ---
            assert call(sock, "MSET", "u1", "a", "u2", "b", "u3", "c") == "OK"
            assert call(sock, "UNLINK", "u1", "missing", "u2") == 2
            assert call(sock, "EXISTS", "u1") == 0
            assert call(sock, "EXISTS", "u3") == 1
            assert call(sock, "UNLINK", "u3") == 1
            assert call(sock, "UNLINK", "nobody") == 0
            # DEL still multi-key
            assert call(sock, "MSET", "d1", "x", "d2", "y") == "OK"
            assert call(sock, "DEL", "d1", "d2", "d3") == 2

            # arity
            r = call(sock, "APPEND", "only")
            assert "wrong number" in err_str(r).lower(), r
            r = call(sock, "RENAME", "one")
            assert "wrong number" in err_str(r).lower(), r
            r = call(sock, "UNLINK")
            assert "wrong number" in err_str(r).lower(), r

            print("test_prod_string_keys: OK")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
