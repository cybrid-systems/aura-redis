#!/usr/bin/env python3
"""P3.17a — MULTI/EXEC/DISCARD subset."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26943"))


def start_server() -> subprocess.Popen:
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
    log = Path(f"/tmp/test-prod-multi-{PORT}.log")
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
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            assert call(sock, "MULTI") == "OK"
            assert call(sock, "SET", "k", "v") == "QUEUED"
            assert call(sock, "GET", "k") == "QUEUED"
            out = call(sock, "EXEC")
            assert out == ["OK", "v"], out
            assert call(sock, "GET", "k") == "v"

            assert call(sock, "MULTI") == "OK"
            assert call(sock, "SET", "k2", "x") == "QUEUED"
            assert call(sock, "DISCARD") == "OK"
            assert call(sock, "GET", "k2") is None

            e = call(sock, "EXEC")
            assert "without multi" in err_str(e).lower()
            d = call(sock, "DISCARD")
            assert "without multi" in err_str(d).lower()

            assert call(sock, "MULTI") == "OK"
            assert "nested" in err_str(call(sock, "MULTI")).lower()
            assert call(sock, "DISCARD") == "OK"

            # empty EXEC
            assert call(sock, "MULTI") == "OK"
            assert call(sock, "EXEC") == []
            print("test_prod_multi: OK")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


if __name__ == "__main__":
    main()
