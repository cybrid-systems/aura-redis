#!/usr/bin/env python3
"""P2.13 — aura-rdb SAVE → kill → restart restores GET/TTL."""
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
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26913"))
BIN = ROOT / "native/build/aura_redis_server"


def build() -> None:
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def start_server(datadir: Path, dbfilename: str = "dump.aura-rdb") -> tuple[subprocess.Popen, Path]:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-rdb-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(BIN),
            "--port",
            str(PORT),
            "--evict",
            "lru",
            "--dir",
            str(datadir),
            "--dbfilename",
            dbfilename,
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(100):
        if "listening on" in log.read_text(errors="replace"):
            return proc, log
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


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def test_save_restart_restore() -> None:
    datadir = Path(tempfile.mkdtemp(prefix="aura-rdb-"))
    dbfilename = "warm.aura-rdb"
    dump = datadir / dbfilename
    try:
        proc, log = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "SET", "k1", "v1") == "OK"
                assert redis_call(sock, "SET", "k2", "v2", "EX", "120") == "OK"
                assert redis_call(sock, "EXPIRE", "k1", "90") == 1
                assert redis_call(sock, "SET", "plain", "no-ttl") == "OK"
                ttl2 = redis_call(sock, "TTL", "k2")
                assert isinstance(ttl2, int) and 100 <= ttl2 <= 120, ttl2
                assert redis_call(sock, "SAVE") == "OK"
                info = info_map(redis_call(sock, "INFO"))
                assert info.get("aura_rdb") == "2"
                assert int(info.get("rdb_last_save_time", "0")) > 0
                assert info.get("dbfilename") == dbfilename
        finally:
            stop_kill(proc)

        assert dump.is_file(), f"missing dump {dump}"
        assert dump.stat().st_size > 16

        # Restart — keys + TTL must warm-start
        proc2, log2 = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "GET", "k1") == "v1"
                assert redis_call(sock, "GET", "k2") == "v2"
                assert redis_call(sock, "GET", "plain") == "no-ttl"
                t1 = redis_call(sock, "TTL", "k1")
                t2 = redis_call(sock, "TTL", "k2")
                tp = redis_call(sock, "TTL", "plain")
                assert isinstance(t1, int) and 1 <= t1 <= 90, t1
                assert isinstance(t2, int) and 1 <= t2 <= 120, t2
                assert tp == -1, tp
                # BGSAVE smoke
                assert redis_call(sock, "BGSAVE") == "OK"
                # wait briefly for child
                for _ in range(40):
                    inf = info_map(redis_call(sock, "INFO"))
                    if inf.get("rdb_bgsave_in_progress") == "0" and inf.get(
                        "rdb_last_bgsave_status"
                    ) == "ok":
                        break
                    time.sleep(0.05)
                inf = info_map(redis_call(sock, "INFO"))
                assert inf.get("rdb_last_bgsave_status") == "ok", inf
                # policy_agent flat keys still present
                for k in ("gets", "sets", "hits", "misses", "evict", "layout", "keys"):
                    assert k in inf, k
            print("SAVE→kill→restart GET/TTL restore OK")
        finally:
            stop_kill(proc2)
            print(log2.read_text(errors="replace")[-500:])
    finally:
        shutil.rmtree(datadir, ignore_errors=True)


def test_missing_dump_ok() -> None:
    datadir = Path(tempfile.mkdtemp(prefix="aura-rdb-empty-"))
    try:
        proc, _ = start_server(datadir, "missing.aura-rdb")
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "PING") == "PONG"
                assert redis_call(sock, "GET", "nope") is None
            print("missing dump cold-start OK")
        finally:
            stop_kill(proc)
    finally:
        shutil.rmtree(datadir, ignore_errors=True)




def test_typed_roundtrip() -> None:
    """HASH/LIST/ZSET survive SAVE → kill → restart (aura-rdb v2)."""
    datadir = Path(tempfile.mkdtemp(prefix="aura-rdb-typed-"))
    dbfilename = "typed.aura-rdb"
    dump = datadir / dbfilename
    try:
        proc, log = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "HSET", "h1", "f1", "v1", "f2", "v2") == 2
                assert redis_call(sock, "RPUSH", "l1", "a", "b", "c") == 3
                assert redis_call(sock, "ZADD", "z1", "1.5", "m1", "2.5", "m2") == 2
                assert redis_call(sock, "SET", "s1", "str") == "OK"
                assert redis_call(sock, "EXPIRE", "h1", "300") == 1
                assert redis_call(sock, "SAVE") == "OK"
                info = info_map(redis_call(sock, "INFO"))
                assert info.get("aura_rdb") == "2"
        finally:
            stop_kill(proc)

        assert dump.is_file() and dump.stat().st_size > 16

        proc2, log2 = start_server(datadir, dbfilename)
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "TYPE", "h1") == "hash"
                assert redis_call(sock, "HGET", "h1", "f1") == "v1"
                assert redis_call(sock, "HGET", "h1", "f2") == "v2"
                assert redis_call(sock, "HLEN", "h1") == 2
                ttl_h = redis_call(sock, "TTL", "h1")
                assert isinstance(ttl_h, int) and 1 <= ttl_h <= 300, ttl_h
                assert redis_call(sock, "TYPE", "l1") == "list"
                assert redis_call(sock, "LRANGE", "l1", "0", "-1") == ["a", "b", "c"]
                assert redis_call(sock, "TYPE", "z1") == "zset"
                assert redis_call(sock, "ZCARD", "z1") == 2
                assert redis_call(sock, "ZSCORE", "z1", "m1") == "1.5"
                assert redis_call(sock, "ZSCORE", "z1", "m2") == "2.5"
                assert redis_call(sock, "GET", "s1") == "str"
            print("typed HASH/LIST/ZSET SAVE→restart OK")
        finally:
            stop_kill(proc2)
            print(log2.read_text(errors="replace")[-500:])
    finally:
        shutil.rmtree(datadir, ignore_errors=True)


def main() -> int:
    build()
    test_save_restart_restore()
    test_typed_roundtrip()
    test_missing_dump_ok()
    print("test_prod_rdb: ALL PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        kill_tcp_port(PORT)
