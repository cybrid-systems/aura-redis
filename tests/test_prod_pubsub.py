#!/usr/bin/env python3
"""P3.17b — Pub/Sub SUBSCRIBE/UNSUBSCRIBE/PUBLISH (two clients)."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26944"))


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
    log = Path(f"/tmp/test-prod-pubsub-{PORT}.log")
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
        sub = socket.create_connection(("127.0.0.1", PORT), timeout=2)
        pub = socket.create_connection(("127.0.0.1", PORT), timeout=2)
        try:
            # SUBSCRIBE returns confirm array ["subscribe", channel, n]
            sub.sendall(encode_array(["SUBSCRIBE", "ch1"]))
            conf = recv_one(sub, bytearray())
            assert conf == ["subscribe", "ch1", 1], conf

            # publisher
            assert call(pub, "PUBLISH", "ch1", "hello") == 1
            msg = recv_one(sub, bytearray())
            assert msg == ["message", "ch1", "hello"], msg

            # second subscriber
            sub2 = socket.create_connection(("127.0.0.1", PORT), timeout=2)
            sub2.sendall(encode_array(["SUBSCRIBE", "ch1"]))
            assert recv_one(sub2, bytearray()) == ["subscribe", "ch1", 1]
            assert call(pub, "PUBLISH", "ch1", "hi") == 2
            assert recv_one(sub, bytearray()) == ["message", "ch1", "hi"]
            assert recv_one(sub2, bytearray()) == ["message", "ch1", "hi"]

            # pubsub mode rejects SET
            sub.sendall(encode_array(["SET", "k", "v"]))
            e = recv_one(sub, bytearray())
            assert "ERR" in err_str(e)

            sub.sendall(encode_array(["UNSUBSCRIBE", "ch1"]))
            u = recv_one(sub, bytearray())
            assert u[0] == "unsubscribe" and u[1] == "ch1" and u[2] == 0, u

            assert call(pub, "PUBLISH", "ch1", "gone") == 1  # only sub2
            assert recv_one(sub2, bytearray()) == ["message", "ch1", "gone"]

            sub2.close()
            print("test_prod_pubsub: OK")
        finally:
            sub.close()
            pub.close()
    finally:
        proc.terminate()
        proc.wait(timeout=3)


if __name__ == "__main__":
    main()
