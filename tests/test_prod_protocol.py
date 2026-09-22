#!/usr/bin/env python3
"""P0.1 — RESP protocol / command-contract harness for aura_redis_server."""
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

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26901"))


def start_server() -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    bin_path = ROOT / "native/build/aura_redis_server"
    if not bin_path.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    else:
        # rebuild if sources newer than binary (cheap when unchanged)
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")],
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-protocol-{PORT}.log")
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
    raise TimeoutError("server did not listen:\n" + log.read_text())


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


def call_raw(sock: socket.socket, payload: bytes, n_replies: int = 1):
    sock.sendall(payload)
    buf = bytearray()
    out = []
    for _ in range(n_replies):
        out.append(recv_one(sock, buf))
    return out[0] if n_replies == 1 else out


def call(sock: socket.socket, *args: str):
    return call_raw(sock, encode_array(list(args)))


def err_str(v) -> str:
    if isinstance(v, Exception):
        return str(v)
    raise AssertionError(f"expected error reply, got {v!r}")


def test_basic(sock: socket.socket) -> None:
    assert call(sock, "PING") == "PONG"
    assert call(sock, "PING", "hello") == "hello"
    assert "wrong number" in err_str(call(sock, "PING", "a", "b")).lower()
    assert call(sock, "SET", "k", "v") == "OK"
    assert call(sock, "GET", "k") == "v"
    assert call(sock, "GET", "missing") is None
    print("basic OK")


def test_arity_and_unknown(sock: socket.socket) -> None:
    assert "wrong number" in err_str(call(sock, "GET")).lower()
    assert "wrong number" in err_str(call(sock, "SET", "onlykey")).lower()
    assert "unknown" in err_str(call(sock, "NOSUCHCMD")).lower()
    print("arity/unknown OK")


def test_pipeline(sock: socket.socket) -> None:
    payload = b"".join(
        [
            encode_array(["SET", "p1", "a"]),
            encode_array(["SET", "p2", "b"]),
            encode_array(["GET", "p1"]),
            encode_array(["GET", "p2"]),
            encode_array(["MGET", "p1", "p2", "p3"]),
        ]
    )
    r = call_raw(sock, payload, n_replies=5)
    assert r[0] == "OK" and r[1] == "OK"
    assert r[2] == "a" and r[3] == "b"
    assert r[4] == ["a", "b", None]
    print("pipeline OK")


def test_large_value(sock: socket.socket) -> None:
    # Well under 16MiB parser cap; stresses bulk encode/decode path.
    n = 256 * 1024
    val = "x" * n
    assert call(sock, "SET", "big", val) == "OK"
    got = call(sock, "GET", "big")
    assert got == val, f"len got={len(got) if got else None}"
    print(f"large value {n}B OK")


def test_partial_reads(sock: socket.socket) -> None:
    payload = encode_array(["SET", "partial", "ok"])
    # Send one byte at a time; server must assemble before dispatch.
    for i in range(0, len(payload)):
        sock.sendall(payload[i : i + 1])
        time.sleep(0.001)
    assert recv_one(sock, bytearray()) == "OK"
    assert call(sock, "GET", "partial") == "ok"
    print("partial reads OK")


def test_null_bulk_argv_rejected(sock: socket.socket) -> None:
    # *2\r\n$3\r\nGET\r\n$-1\r\n  — null bulk as key must be protocol error / close
    bad = b"*2\r\n$3\r\nGET\r\n$-1\r\n"
    sock.sendall(bad)
    buf = bytearray()
    try:
        v = recv_one(sock, buf, timeout=2.0)
        assert isinstance(v, Exception), v
        assert "protocol" in str(v).lower() or "err" in str(v).lower()
        print("null bulk argv → error OK", v)
    except (ConnectionError, OSError, TimeoutError):
        # closing without reply is also acceptable hardening
        print("null bulk argv → connection close OK")
    # reconnect for further tests
    raise _Reconnect()


class _Reconnect(Exception):
    pass


def test_inline_rejected(sock: socket.socket) -> None:
    sock.sendall(b"PING\r\n")
    buf = bytearray()
    try:
        v = recv_one(sock, buf, timeout=2.0)
        assert isinstance(v, Exception)
        print("inline rejected OK", v)
    except (ConnectionError, OSError, TimeoutError):
        print("inline rejected → close OK")
    raise _Reconnect()


def test_oversize_array(sock: socket.socket) -> None:
    # AR_MAX_ARGV = 64; *65 is protocol error
    sock.sendall(b"*65\r\n")
    buf = bytearray()
    try:
        v = recv_one(sock, buf, timeout=2.0)
        assert isinstance(v, Exception)
        print("oversize array OK", v)
    except (ConnectionError, OSError, TimeoutError):
        print("oversize array → close OK")
    raise _Reconnect()


def test_deny_plugin(sock: socket.socket) -> None:
    v = call(sock, "PLUGIN", "/tmp/nope.so")
    assert isinstance(v, Exception)
    assert "denied" in str(v).lower() or "plugin" in str(v).lower()
    print("DENY_PLUGIN OK")


def connect() -> socket.socket:
    return socket.create_connection(("127.0.0.1", PORT), 3)


def main() -> int:
    docs = (ROOT / "docs/commands.md").read_text()
    for name in ("GET", "SET", "PING", "EVICT", "INFO", "POLICY", "PLUGIN"):
        assert name in docs or name.lower() in docs.lower(), f"commands.md missing {name}"
    print("commands.md catalog present OK")

    proc = start_server()
    try:
        s = connect()
        test_basic(s)
        test_arity_and_unknown(s)
        test_pipeline(s)
        test_large_value(s)
        test_partial_reads(s)
        test_deny_plugin(s)
        for fn in (test_null_bulk_argv_rejected, test_inline_rejected, test_oversize_array):
            try:
                fn(s)
            except _Reconnect:
                try:
                    s.close()
                except OSError:
                    pass
                time.sleep(0.05)
                s = connect()
                assert call(s, "PING") == "PONG"
        s.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("test_prod_protocol PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
