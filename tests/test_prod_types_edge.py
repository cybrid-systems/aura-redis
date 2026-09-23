#!/usr/bin/env python3
"""P3.16/17 edge cases: cross-type WRONGTYPE, TYPE none, pipeline, MULTI+WRONGTYPE.

MULTI semantics (aura-redis): EXEC always returns an array of per-command replies.
A WRONGTYPE (or other error) inside the transaction becomes an error *element*
in that array; remaining queued commands still run (Redis-ish EXEC behavior;
aura-redis does not abort the whole EXEC on the first error).
"""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26991"))


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
    log = Path(f"/tmp/test-prod-types-edge-{PORT}.log")
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


def pipeline(sock: socket.socket, cmds: list[tuple[str, ...]]):
    for c in cmds:
        sock.sendall(encode_array(list(c)))
    buf = bytearray()
    return [recv_one(sock, buf) for _ in cmds]


def err_str(v) -> str:
    if isinstance(v, Exception):
        return str(v)
    raise AssertionError(f"expected error, got {v!r}")


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            # empty / missing key
            assert call(sock, "TYPE", "no-such-key") == "none"

            # SET → HGET WRONGTYPE
            assert call(sock, "SET", "s", "x") == "OK"
            assert "WRONGTYPE" in err_str(call(sock, "HGET", "s", "f"))

            # HSET → LPUSH WRONGTYPE
            assert call(sock, "HSET", "h", "a", "1") == 1
            assert "WRONGTYPE" in err_str(call(sock, "LPUSH", "h", "v"))

            # LPUSH → ZADD WRONGTYPE
            assert call(sock, "LPUSH", "L", "a") == 1
            assert "WRONGTYPE" in err_str(call(sock, "ZADD", "L", "1", "m"))

            # pipeline HSET + HGET
            outs = pipeline(sock, [("HSET", "p", "k", "v"), ("HGET", "p", "k")])
            assert outs == [1, "v"], outs

            # MULTI with WRONGTYPE inside EXEC: error element, rest still runs
            assert call(sock, "SET", "t", "str") == "OK"
            assert call(sock, "MULTI") == "OK"
            assert call(sock, "SET", "ok1", "1") == "QUEUED"
            assert call(sock, "HGET", "t", "f") == "QUEUED"  # WRONGTYPE at EXEC
            assert call(sock, "SET", "ok2", "2") == "QUEUED"
            ex = call(sock, "EXEC")
            assert isinstance(ex, list) and len(ex) == 3, ex
            assert ex[0] == "OK", ex
            assert isinstance(ex[1], Exception) and "WRONGTYPE" in str(ex[1]), ex
            assert ex[2] == "OK", ex
            assert call(sock, "GET", "ok1") == "1"
            assert call(sock, "GET", "ok2") == "2"

            print("test_prod_types_edge: OK")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
