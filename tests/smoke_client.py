#!/usr/bin/env python3
"""Pure-Python RESP2 client smoke tests for aura-redis (no redis-cli)."""
from __future__ import annotations

import argparse
import socket
import sys
import time


class Incomplete(Exception):
    pass


def encode_array(args: list[str]) -> bytes:
    out = f"*{len(args)}\r\n".encode()
    for a in args:
        b = a.encode()
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return out


def decode_one(buf: bytearray):
    if not buf:
        raise Incomplete()
    t = buf[0:1]
    if t in (b"+", b"-", b":"):
        i = buf.find(b"\r\n")
        if i < 0:
            raise Incomplete()
        payload = buf[1:i].decode()
        consumed = i + 2
        if t == b"+":
            return payload, consumed
        if t == b"-":
            return Exception(payload), consumed
        return int(payload), consumed
    if t == b"$":
        i = buf.find(b"\r\n")
        if i < 0:
            raise Incomplete()
        n = int(buf[1:i])
        if n < 0:
            return None, i + 2
        start = i + 2
        end = start + n
        if len(buf) < end + 2:
            raise Incomplete()
        return bytes(buf[start:end]).decode(), end + 2
    if t == b"*":
        i = buf.find(b"\r\n")
        if i < 0:
            raise Incomplete()
        n = int(buf[1:i])
        pos = i + 2
        items = []
        for _ in range(n):
            v, c = decode_one(buf[pos:])
            items.append(v)
            pos += c
        return items, pos
    raise ValueError(f"bad RESP type {t!r}")


def redis_call(sock: socket.socket, *args: str, timeout: float = 10.0):
    sock.settimeout(timeout)
    sock.sendall(encode_array(list(args)))
    buf = bytearray()
    while True:
        try:
            v, c = decode_one(buf)
            del buf[:c]
            if isinstance(v, Exception):
                raise RuntimeError(str(v))
            return v
        except Incomplete:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed during read")
            buf.extend(chunk)


def expect(name: str, got, want) -> None:
    if got != want:
        raise AssertionError(f"{name}: got {got!r} want {want!r}")
    print(f"  OK {name}: {got!r}")


def run(host: str, port: int) -> int:
    print(f"smoke: connect {host}:{port}")
    with socket.create_connection((host, port), timeout=10) as sock:
        expect("PING", redis_call(sock, "PING"), "PONG")
        expect("SET", redis_call(sock, "SET", "a", "hello"), "OK")
        expect("GET", redis_call(sock, "GET", "a"), "hello")
        expect("EXISTS", redis_call(sock, "EXISTS", "a"), 1)
        expect("INCR", redis_call(sock, "INCR", "n"), 1)
        expect("INCR2", redis_call(sock, "INCR", "n"), 2)
        expect("GET n", redis_call(sock, "GET", "n"), "2")
        expect("DEL", redis_call(sock, "DEL", "a"), 1)
        expect("GET miss", redis_call(sock, "GET", "a"), None)
        expect("FLUSHDB", redis_call(sock, "FLUSHDB"), "OK")
        expect("EXISTS after flush", redis_call(sock, "EXISTS", "n"), 0)
        # Pipelining
        sock.sendall(encode_array(["PING"]) + encode_array(["ECHO", "pipe"]))
        buf = bytearray()
        vals = []
        while len(vals) < 2:
            try:
                v, c = decode_one(buf)
                del buf[:c]
                if isinstance(v, Exception):
                    raise RuntimeError(str(v))
                vals.append(v)
            except Incomplete:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("closed in pipeline")
                buf.extend(chunk)
        expect("pipeline PING", vals[0], "PONG")
        expect("pipeline ECHO", vals[1], "pipe")
        redis_call(sock, "QUIT")
    print("smoke: ALL PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="aura-redis RESP smoke client")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--retries", type=int, default=90)
    args = p.parse_args(argv)
    last: Exception | None = None
    for _ in range(args.retries):
        try:
            return run(args.host, args.port)
        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.25)
    print(f"smoke: failed to connect after retries: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
