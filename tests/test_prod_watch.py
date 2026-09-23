#!/usr/bin/env python3
"""T2.12 — WATCH/UNWATCH optimistic locking for MULTI/EXEC."""
from __future__ import annotations

import os
import socket
import threading
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import Incomplete, decode_one, encode_array  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26955"))


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
    log = Path(f"/tmp/test-prod-watch-{PORT}.log")
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


def recv_one(sock, buf, timeout=5.0):
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


def call(sock, *args):
    sock.sendall(encode_array(list(args)))
    return recv_one(sock, bytearray())


def err_str(v):
    if isinstance(v, Exception):
        return str(v)
    raise AssertionError(f"expected error, got {v!r}")


def main():
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as a, \
             socket.create_connection(("127.0.0.1", PORT), timeout=2) as b:
            # Clean success path: WATCH → MULTI → SET → EXEC
            assert call(a, "SET", "wk", "1") == "OK"
            assert call(a, "WATCH", "wk") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "SET", "wk", "2") == "QUEUED"
            assert call(a, "GET", "wk") == "QUEUED"
            out = call(a, "EXEC")
            assert out == ["OK", "2"], out
            assert call(a, "GET", "wk") == "2"

            # Concurrent writer aborts EXEC (null array)
            assert call(a, "WATCH", "wk") == "OK"
            assert call(b, "SET", "wk", "dirty") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "SET", "wk", "cas") == "QUEUED"
            aborted = call(a, "EXEC")
            assert aborted is None, aborted
            assert call(a, "GET", "wk") == "dirty"  # queued SET not applied

            # Same-client write before MULTI also dirties
            assert call(a, "WATCH", "wk") == "OK"
            assert call(a, "SET", "wk", "self") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "INCR", "wk") == "QUEUED"
            assert call(a, "EXEC") is None
            assert call(a, "GET", "wk") == "self"

            # UNWATCH clears; subsequent EXEC succeeds
            assert call(a, "WATCH", "wk") == "OK"
            assert call(b, "SET", "wk", "x") == "OK"
            assert call(a, "UNWATCH") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "SET", "wk", "y") == "QUEUED"
            assert call(a, "EXEC") == ["OK"]
            assert call(a, "GET", "wk") == "y"

            # DISCARD clears watches
            assert call(a, "WATCH", "wk") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "SET", "wk", "z") == "QUEUED"
            assert call(a, "DISCARD") == "OK"
            assert call(b, "SET", "wk", "after-discard") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "GET", "wk") == "QUEUED"
            assert call(a, "EXEC") == ["after-discard"]

            # WATCH missing key; create dirties
            assert call(a, "DEL", "missing") in (0, 1)
            assert call(a, "WATCH", "missing") == "OK"
            assert call(b, "SET", "missing", "now") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "GET", "missing") == "QUEUED"
            assert call(a, "EXEC") is None

            # WATCH inside MULTI rejected
            assert call(a, "MULTI") == "OK"
            e = call(a, "WATCH", "wk")
            assert "watch inside multi" in err_str(e).lower()
            assert call(a, "DISCARD") == "OK"

            # Typed key WATCH (HASH)
            assert call(a, "DEL", "hk") in (0, 1)
            assert call(a, "HSET", "hk", "f", "1") == 1
            assert call(a, "WATCH", "hk") == "OK"
            assert call(b, "HSET", "hk", "f", "2") == 0
            assert call(a, "MULTI") == "OK"
            assert call(a, "HGET", "hk", "f") == "QUEUED"
            assert call(a, "EXEC") is None
            assert call(a, "HGET", "hk", "f") == "2"

            # FLUSHDB dirties all watches
            assert call(a, "SET", "fk", "1") == "OK"
            assert call(a, "WATCH", "fk") == "OK"
            assert call(b, "FLUSHDB") == "OK"
            assert call(a, "MULTI") == "OK"
            assert call(a, "SET", "fk", "2") == "QUEUED"
            assert call(a, "EXEC") is None

            # Concurrent writers soak edge: many clients race WATCH/CAS on one key
            call(a, "SET", "race", "0")
            stop = threading.Event()
            wins = {"n": 0}
            aborts = {"n": 0}
            errs = []

            def worker(wid: int):
                try:
                    with socket.create_connection(("127.0.0.1", PORT), timeout=2) as s:
                        while not stop.is_set():
                            call(s, "WATCH", "race")
                            cur = call(s, "GET", "race")
                            call(s, "MULTI")
                            call(s, "SET", "race", f"{wid}-{cur}")
                            r = call(s, "EXEC")
                            if r is None:
                                aborts["n"] += 1
                            else:
                                wins["n"] += 1
                except Exception as ex:  # noqa: BLE001
                    errs.append(ex)

            threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(4)]
            for th in threads:
                th.start()
            time.sleep(0.4)
            stop.set()
            for th in threads:
                th.join(timeout=3)
            assert not errs, errs
            assert wins["n"] + aborts["n"] > 20, (wins, aborts)
            assert aborts["n"] > 0, "expected some CAS aborts under contention"
            assert call(a, "EXISTS", "race") == 1

            print("test_prod_watch: OK")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


if __name__ == "__main__":
    main()
