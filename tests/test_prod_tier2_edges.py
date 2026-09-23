#!/usr/bin/env python3
"""Tier-2 hardening edges — SCAN / SET-opts / APPEND-RENAME / STRLEN-meta / RDB.

Gaps beyond primary T2 suites (scan / set_opts / string_keys / string_meta / rdb):
  - SCAN MATCH no-hits; COUNT=1 pagination completeness; DEL between SCAN pages
  - KEYS after mixed typed keyspace
  - SET NX/XX + EX/PX order variants; SETNX on typed; GETSET clears TTL (recheck)
  - APPEND empty vs existing; RENAME across types; RENAMENX; UNLINK multi + missing
  - STRLEN WRONGTYPE; SETEX/PSETEX invalid TTL; DBSIZE after expire (polled)
  - Typed RDB: empty dump; larger hash/list/zset + TTL roundtrip

Flake harden: poll-until-expired (PING pump) instead of fixed short sleeps.
AURA_REDIS_DENY_PLUGIN=1 throughout.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import Incomplete, decode_one, encode_array, redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26999"))
BIN = ROOT / "native/build/aura_redis_server"


def err_str(v) -> str:
    return str(v)


def start_server(datadir: Path | None = None, dbfilename: str = "dump.aura-rdb") -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    if not BIN.exists() or os.environ.get("AURA_REDIS_SKIP_BUILD") != "1":
        subprocess.check_call(
            [str(ROOT / "scripts/build-native.sh")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-tier2-edges-{PORT}.log")
    cmd = [str(BIN), "--port", str(PORT), "--evict", "lru", "--maxmemory", "0"]
    if datadir is not None:
        cmd += ["--dir", str(datadir), "--dbfilename", dbfilename]
    proc = subprocess.Popen(
        cmd, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env
    )
    for _ in range(80):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop_kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


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
    """Poll GET until missing; PING pumps active-expire."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if call(sock, "GET", key) is None:
            return
        call(sock, "PING")
        time.sleep(0.05)
    raise AssertionError(f"{key} did not expire within {timeout}s")


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
        assert isinstance(cursor, str) and isinstance(keys, list)
        seen.extend(keys)
        rounds += 1
        if cursor == "0":
            break
        assert rounds < 20000, "SCAN did not terminate"
    return seen, rounds


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def test_scan_edges(sock: socket.socket) -> None:
    assert call(sock, "FLUSHDB") == "OK"

    assert call(sock, "SET", "alpha", "1") == "OK"
    assert call(sock, "SET", "beta", "2") == "OK"
    miss, rounds = scan_all(sock, match="zzz*", count=10)
    assert miss == [], miss
    assert rounds >= 1
    assert call(sock, "KEYS", "zzz*") == []

    for i in range(12):
        assert call(sock, "SET", f"p:{i}", str(i)) == "OK"
    assert call(sock, "HSET", "p:h", "f", "v") == 1
    assert call(sock, "LPUSH", "p:l", "x") == 1
    assert call(sock, "ZADD", "p:z", "1", "m") == 1
    all_keys, rounds = scan_all(sock, count=1)
    assert rounds >= 10, rounds
    expect = {f"p:{i}" for i in range(12)} | {"p:h", "p:l", "p:z", "alpha", "beta"}
    assert set(all_keys) == expect, (sorted(all_keys), sorted(expect))

    keys = call(sock, "KEYS", "p:*")
    assert set(keys) == {f"p:{i}" for i in range(12)} | {"p:h", "p:l", "p:z"}

    # DEL between SCAN pages (same connection; single-threaded interleaved)
    assert call(sock, "FLUSHDB") == "OK"
    for i in range(20):
        assert call(sock, "SET", f"d:{i}", "v") == "OK"
    cursor = "0"
    deleted_mid = False
    for _ in range(5000):
        reply = call(sock, "SCAN", cursor, "COUNT", "2")
        cursor, _keys = reply[0], reply[1]
        if not deleted_mid and cursor != "0":
            for i in range(0, 20, 2):
                call(sock, "DEL", f"d:{i}")
            deleted_mid = True
        if cursor == "0":
            break
    assert deleted_mid
    for i in range(1, 20, 2):
        assert call(sock, "EXISTS", f"d:{i}") == 1
    for i in range(0, 20, 2):
        assert call(sock, "EXISTS", f"d:{i}") == 0
    left, _ = scan_all(sock, count=3)
    assert set(left) == {f"d:{i}" for i in range(1, 20, 2)}, left
    print("scan edges OK")


def test_set_opts_edges(sock: socket.socket) -> None:
    assert call(sock, "FLUSHDB") == "OK"

    assert call(sock, "SET", "c1", "a", "NX", "PX", "800") == "OK"
    assert call(sock, "SET", "c1", "b", "XX", "EX", "5") == "OK"
    assert call(sock, "GET", "c1") == "b"
    assert call(sock, "TTL", "c1") in (4, 5)

    assert call(sock, "DEL", "c2") in (0, 1)
    assert call(sock, "SET", "c2", "z", "PX", "500", "NX") == "OK"
    assert call(sock, "GET", "c2") == "z"
    wait_gone(sock, "c2", timeout=2.5)

    assert call(sock, "HSET", "th", "f", "1") == 1
    assert call(sock, "SETNX", "th", "s") == 0
    assert call(sock, "TYPE", "th") == "hash"
    assert call(sock, "LPUSH", "tl", "x") == 1
    assert call(sock, "SETNX", "tl", "s") == 0
    assert call(sock, "TYPE", "tl") == "list"
    assert call(sock, "ZADD", "tz", "1", "m") == 1
    assert call(sock, "SETNX", "tz", "s") == 0
    assert call(sock, "TYPE", "tz") == "zset"

    assert call(sock, "SET", "gs", "old", "EX", "30") == "OK"
    assert call(sock, "TTL", "gs") > 0
    assert call(sock, "GETSET", "gs", "new") == "old"
    assert call(sock, "TTL", "gs") == -1
    assert call(sock, "GET", "gs") == "new"
    print("set opts edges OK")


def test_string_key_edges(sock: socket.socket) -> None:
    assert call(sock, "FLUSHDB") == "OK"

    assert call(sock, "APPEND", "e", "") == 0
    assert call(sock, "EXISTS", "e") == 1
    assert call(sock, "STRLEN", "e") == 0
    assert call(sock, "APPEND", "e", "ab") == 2
    assert call(sock, "APPEND", "e", "cd") == 4
    assert call(sock, "GET", "e") == "abcd"

    assert call(sock, "SET", "s1", "sv") == "OK"
    assert call(sock, "HSET", "h1", "f", "hv") == 1
    assert call(sock, "RENAME", "s1", "h1") == "OK"
    assert call(sock, "TYPE", "h1") == "string"
    assert call(sock, "GET", "h1") == "sv"
    assert call(sock, "EXISTS", "s1") == 0

    assert call(sock, "HSET", "h2", "a", "1") == 1
    assert call(sock, "SET", "s2", "keep") == "OK"
    assert call(sock, "RENAME", "h2", "s2") == "OK"
    assert call(sock, "TYPE", "s2") == "hash"
    assert call(sock, "HGET", "s2", "a") == "1"

    assert call(sock, "LPUSH", "l1", "x", "y") == 2
    assert call(sock, "ZADD", "z1", "1", "m") == 1
    assert call(sock, "RENAME", "l1", "z1") == "OK"
    assert call(sock, "TYPE", "z1") == "list"
    assert call(sock, "LRANGE", "z1", "0", "-1") == ["y", "x"]

    assert call(sock, "SET", "nxsrc", "1") == "OK"
    assert call(sock, "HSET", "nxdst", "f", "v") == 1
    assert call(sock, "RENAMENX", "nxsrc", "nxdst") == 0
    assert call(sock, "GET", "nxsrc") == "1"
    assert call(sock, "TYPE", "nxdst") == "hash"

    assert call(sock, "FLUSHDB") == "OK"
    assert call(sock, "MSET", "u1", "a", "u2", "b") == "OK"
    assert call(sock, "HSET", "uh", "f", "1") == 1
    assert call(sock, "LPUSH", "ul", "x") == 1
    assert call(sock, "UNLINK", "u1", "missing", "uh", "ghost", "ul", "u2") == 4
    assert call(sock, "DBSIZE") == 0
    print("string key edges OK")


def test_string_meta_edges(sock: socket.socket) -> None:
    assert call(sock, "FLUSHDB") == "OK"

    assert call(sock, "LPUSH", "L", "x") == 1
    r = call(sock, "STRLEN", "L")
    assert "WRONGTYPE" in err_str(r), r
    assert call(sock, "ZADD", "Z", "1", "m") == 1
    r = call(sock, "STRLEN", "Z")
    assert "WRONGTYPE" in err_str(r), r

    for args in (
        ("SETEX", "bad", "0", "x"),
        ("SETEX", "bad", "-5", "x"),
        ("PSETEX", "bad", "0", "x"),
        ("PSETEX", "bad", "-1", "x"),
    ):
        r = call(sock, *args)
        assert "invalid expire" in err_str(r).lower(), (args, r)

    assert call(sock, "FLUSHDB") == "OK"
    assert call(sock, "MSET", "a", "1", "b", "2") == "OK"
    assert call(sock, "PSETEX", "tmp", "200", "t") == "OK"
    assert call(sock, "DBSIZE") == 3
    wait_gone(sock, "tmp", timeout=2.5)
    assert call(sock, "DBSIZE") == 2
    assert call(sock, "EXISTS", "tmp") == 0
    print("string meta edges OK")


def test_rdb_edges() -> None:
    datadir = Path(tempfile.mkdtemp(prefix="aura-rdb-empty-typed-"))
    dbfilename = "empty.aura-rdb"
    try:
        proc = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "FLUSHDB") == "OK"
                assert redis_call(sock, "DBSIZE") == 0
                assert redis_call(sock, "SAVE") == "OK"
                info = info_map(redis_call(sock, "INFO"))
                assert info.get("aura_rdb") == "2"
        finally:
            stop_kill(proc)
        dump = datadir / dbfilename
        assert dump.is_file() and dump.stat().st_size >= 8

        proc2 = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "DBSIZE") == 0
                assert redis_call(sock, "KEYS", "*") == []
            print("empty typed RDB OK")
        finally:
            stop_kill(proc2)
    finally:
        shutil.rmtree(datadir, ignore_errors=True)

    datadir = Path(tempfile.mkdtemp(prefix="aura-rdb-large-"))
    dbfilename = "large.aura-rdb"
    n = 64
    try:
        proc = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                for i in range(n):
                    assert redis_call(sock, "HSET", "bigH", f"f{i}", f"v{i}") == 1
                    assert redis_call(sock, "RPUSH", "bigL", f"e{i}") == i + 1
                    assert redis_call(sock, "ZADD", "bigZ", str(float(i)), f"m{i}") == 1
                assert redis_call(sock, "SET", "s", "str", "EX", "600") == "OK"
                assert redis_call(sock, "EXPIRE", "bigH", "600") == 1
                assert redis_call(sock, "EXPIRE", "bigL", "600") == 1
                assert redis_call(sock, "SAVE") == "OK"
                assert redis_call(sock, "HLEN", "bigH") == n
                assert redis_call(sock, "LLEN", "bigL") == n
                assert redis_call(sock, "ZCARD", "bigZ") == n
        finally:
            stop_kill(proc)

        proc2 = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "HLEN", "bigH") == n
                assert redis_call(sock, "HGET", "bigH", "f0") == "v0"
                assert redis_call(sock, "HGET", "bigH", f"f{n-1}") == f"v{n-1}"
                assert redis_call(sock, "LLEN", "bigL") == n
                assert redis_call(sock, "LINDEX", "bigL", "0") == "e0"
                assert redis_call(sock, "LINDEX", "bigL", str(n - 1)) == f"e{n-1}"
                assert redis_call(sock, "ZCARD", "bigZ") == n
                assert float(redis_call(sock, "ZSCORE", "bigZ", "m0")) == 0.0
                assert float(redis_call(sock, "ZSCORE", "bigZ", f"m{n-1}")) == float(n - 1)
                assert redis_call(sock, "GET", "s") == "str"
                th = redis_call(sock, "TTL", "bigH")
                ts = redis_call(sock, "TTL", "s")
                assert isinstance(th, int) and 1 <= th <= 600, th
                assert isinstance(ts, int) and 1 <= ts <= 600, ts
                assert redis_call(sock, "DBSIZE") == 4
            print("large typed RDB + TTL OK")
        finally:
            stop_kill(proc2)
    finally:
        shutil.rmtree(datadir, ignore_errors=True)


def main() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            test_scan_edges(sock)
            test_set_opts_edges(sock)
            test_string_key_edges(sock)
            test_string_meta_edges(sock)
    finally:
        stop_kill(proc)

    test_rdb_edges()
    print("test_prod_tier2_edges: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        kill_tcp_port(PORT)
