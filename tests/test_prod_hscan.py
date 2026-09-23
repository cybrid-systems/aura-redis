#!/usr/bin/env python3
"""Tier-2 HSCAN — empty/missing, pagination, MATCH, COUNT, WRONGTYPE."""
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
    log = Path(f"/tmp/test-prod-hscan-{PORT}.log")
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
    raise AssertionError(f"expected error, got {v!r}")


def hscan_all(sock: socket.socket, key: str, match: str | None = None,
              count: int | None = None):
    cursor = "0"
    pairs: list[tuple[str, str]] = []
    rounds = 0
    while True:
        args = ["HSCAN", key, cursor]
        if match is not None:
            args += ["MATCH", match]
        if count is not None:
            args += ["COUNT", str(count)]
        reply = call(sock, *args)
        assert isinstance(reply, list) and len(reply) == 2, reply
        cursor, elems = reply[0], reply[1]
        assert isinstance(cursor, str)
        assert isinstance(elems, list)
        assert len(elems) % 2 == 0, elems
        for i in range(0, len(elems), 2):
            pairs.append((elems[i], elems[i + 1]))
        rounds += 1
        if cursor == "0":
            break
        assert rounds < 10000, "HSCAN did not terminate"
    return pairs, rounds


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            # missing key
            r = call(sock, "HSCAN", "nope", "0")
            assert r == ["0", []], r

            # WRONGTYPE
            assert call(sock, "SET", "s", "v") == "OK"
            e = err_str(call(sock, "HSCAN", "s", "0"))
            assert "WRONGTYPE" in e, e

            # seed hash
            assert call(sock, "HSET", "h", "a", "1", "b", "2", "c:x", "3",
                        "c:y", "4", "d", "5") == 5
            assert call(sock, "HLEN", "h") == 5

            pairs, rounds = hscan_all(sock, "h", count=1)
            assert rounds >= 1
            got = dict(pairs)
            assert got == {"a": "1", "b": "2", "c:x": "3", "c:y": "4", "d": "5"}, got

            # MATCH
            pairs, _ = hscan_all(sock, "h", match="c:*", count=2)
            assert dict(pairs) == {"c:x": "3", "c:y": "4"}, pairs

            pairs, _ = hscan_all(sock, "h", match="?")
            assert dict(pairs) == {"a": "1", "b": "2", "d": "5"}, pairs

            # empty hash after delete all fields
            assert call(sock, "HDEL", "h", "a", "b", "c:x", "c:y", "d") == 5
            r = call(sock, "HSCAN", "h", "0")
            assert r == ["0", []], r

            print("PASS HSCAN missing/WRONGTYPE/paginate/MATCH")
            print("test_prod_hscan: ALL PASSED")
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
