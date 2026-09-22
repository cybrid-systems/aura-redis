#!/usr/bin/env python3
"""P3.16a — HASH commands + WRONGTYPE + memory accounting."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26940"))


def start_server(maxmemory: str = "0") -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    bin_path = ROOT / "native/build/aura_redis_server"
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-hash-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(bin_path),
            "--port",
            str(PORT),
            "--evict",
            "lru",
            "--maxmemory",
            maxmemory,
        ],
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
    buf = bytearray()
    return recv_one(sock, buf)


def err_str(v) -> str:
    if isinstance(v, Exception):
        return str(v)
    raise AssertionError(f"expected error, got {v!r}")


def info_used(sock: socket.socket) -> int:
    info = call(sock, "INFO", "memory")
    assert isinstance(info, str)
    for line in info.splitlines():
        if line.startswith("used_memory:"):
            return int(line.split(":", 1)[1])
    raise AssertionError("no used_memory in INFO")


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            assert call(sock, "HSET", "h", "a", "1") == 1
            assert call(sock, "HSET", "h", "a", "2") == 0  # update
            assert call(sock, "HSET", "h", "b", "3", "c", "4") == 2
            assert call(sock, "HGET", "h", "a") == "2"
            assert call(sock, "HGET", "h", "missing") is None
            assert call(sock, "HMGET", "h", "a", "b", "z") == ["2", "3", None]
            allv = call(sock, "HGETALL", "h")
            assert isinstance(allv, list) and len(allv) == 6
            d = dict(zip(allv[0::2], allv[1::2]))
            assert d == {"a": "2", "b": "3", "c": "4"}
            assert call(sock, "HEXISTS", "h", "a") == 1
            assert call(sock, "HEXISTS", "h", "z") == 0
            assert call(sock, "HLEN", "h") == 3
            assert call(sock, "HINCRBY", "h", "a", "5") == 7
            assert call(sock, "HDEL", "h", "b", "z") == 1
            assert call(sock, "HLEN", "h") == 2
            assert call(sock, "TYPE", "h") == "hash"
            assert call(sock, "EXISTS", "h") == 1

            # WRONGTYPE
            assert call(sock, "SET", "s", "x") == "OK"
            assert "WRONGTYPE" in err_str(call(sock, "HGET", "s", "f"))
            assert "WRONGTYPE" in err_str(call(sock, "GET", "h"))

            # DEL clears hash
            assert call(sock, "DEL", "h") == 1
            assert call(sock, "HGET", "h", "a") is None
            assert call(sock, "TYPE", "h") == "none"

            # memory does not explode under many fields
            base = info_used(sock)
            for i in range(200):
                call(sock, "HSET", "big", f"f{i}", f"v{i}" * 4)
            after = info_used(sock)
            assert after > base
            assert after < base + 5_000_000
            assert call(sock, "HLEN", "big") == 200
            call(sock, "FLUSHDB")
            assert info_used(sock) == 0
            print("test_prod_hash: OK")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


if __name__ == "__main__":
    main()
