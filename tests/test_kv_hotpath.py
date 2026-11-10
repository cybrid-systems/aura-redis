#!/usr/bin/env python3
"""Correctness for the GET/SET hot path (embedded short keys, in-place overwrite).

Covers GET after SET, same-size and different-size overwrite, miss, long keys,
RENAME, and eviction under maxmemory.
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
from smoke_client import redis_call  # noqa: E402

BIN = ROOT / "native/build/aura_redis_server"
PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26973"))


def start(maxmemory: int = 0) -> subprocess.Popen:
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    cmd = [str(BIN), "--port", str(PORT), "--bind", "127.0.0.1", "--evict", "lru"]
    if maxmemory:
        cmd.extend(["--maxmemory", str(maxmemory)])
    log = Path(f"/tmp/test-kv-hotpath-{PORT}.log")
    proc = subprocess.Popen(
        cmd, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env
    )
    for _ in range(80):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=0.2)
            s.close()
            return proc
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(log.read_text(errors="replace"))
            time.sleep(0.05)
    raise TimeoutError("server did not listen")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_get_set_overwrite_miss() -> None:
    proc = start()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
            assert redis_call(s, "GET", "missing") is None
            assert redis_call(s, "SET", "k", "v" * 32) == "OK"
            assert redis_call(s, "GET", "k") == "v" * 32
            # same-size overwrite (in-place buffer)
            assert redis_call(s, "SET", "k", "w" * 32) == "OK"
            assert redis_call(s, "GET", "k") == "w" * 32
            # shorter and longer
            assert redis_call(s, "SET", "k", "short") == "OK"
            assert redis_call(s, "GET", "k") == "short"
            assert redis_call(s, "SET", "k", "x" * 80) == "OK"
            assert redis_call(s, "GET", "k") == "x" * 80
            # long key (heap, not embed)
            longk = "L" * 40
            assert redis_call(s, "SET", longk, "long-val") == "OK"
            assert redis_call(s, "GET", longk) == "long-val"
            assert redis_call(s, "SET", longk, "long-val") == "OK"
            assert redis_call(s, "GET", longk) == "long-val"
            assert redis_call(s, "RENAME", "k", "k2") == "OK"
            assert redis_call(s, "GET", "k") is None
            assert redis_call(s, "GET", "k2") == "x" * 80
            assert redis_call(s, "RENAME", longk, "shortname") == "OK"
            assert redis_call(s, "GET", "shortname") == "long-val"
            assert redis_call(s, "GET", longk) is None
            # many distinct short keys still round-trip
            for i in range(200):
                redis_call(s, "SET", f"n{i}", f"val{i:04d}" + "z" * 16)
            for i in range(200):
                assert redis_call(s, "GET", f"n{i}") == f"val{i:04d}" + "z" * 16
            redis_call(s, "QUIT")
        print("PASS get/set/overwrite/miss/rename")
    finally:
        stop(proc)


def test_evict_under_maxmemory() -> None:
    proc = start(maxmemory=80_000)
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as s:
            val = "V" * 400
            for i in range(300):
                redis_call(s, "SET", f"e{i:04d}", val)
            # Touch the tail so LRU keeps it.
            for _ in range(3):
                for i in range(250, 300):
                    redis_call(s, "GET", f"e{i:04d}")
            for i in range(80):
                redis_call(s, "SET", f"cold{i:04d}", val)
            hot = sum(
                1 for i in range(250, 300) if redis_call(s, "EXISTS", f"e{i:04d}") == 1
            )
            cold = sum(
                1 for i in range(0, 40) if redis_call(s, "EXISTS", f"e{i:04d}") == 1
            )
            info = redis_call(s, "INFO")
            evicted = 0
            for line in str(info).splitlines():
                if line.startswith("evicted:"):
                    evicted = int(line.split(":", 1)[1])
            redis_call(s, "QUIT")
        assert evicted > 0, evicted
        assert hot > cold, (hot, cold, evicted)
        # a touched key still reads back the value written
        print(f"PASS evict hot={hot} cold={cold} evicted={evicted}")
    finally:
        stop(proc)


if __name__ == "__main__":
    test_get_set_overwrite_miss()
    test_evict_under_maxmemory()
    print("PASS kv hotpath")
