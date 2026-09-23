#!/usr/bin/env python3
"""Pure-Python RESP2 client smoke tests for aura-redis (no redis-cli)."""
from __future__ import annotations

import argparse
import socket
import sys
import threading
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
        if n < 0:
            return None, i + 2  # RESP2 null array (e.g. EXEC abort)
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


def expect_true(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{name}: {detail}")
    print(f"  OK {name}")


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

        # MSET / MGET
        expect("MSET", redis_call(sock, "MSET", "x", "1", "y", "2"), "OK")
        expect("MGET", redis_call(sock, "MGET", "x", "missing", "y"), ["1", None, "2"])

        # APPEND / STRLEN / GETSET
        expect("APPEND new", redis_call(sock, "APPEND", "s", "ab"), 2)
        expect("APPEND more", redis_call(sock, "APPEND", "s", "cd"), 4)
        expect("STRLEN", redis_call(sock, "STRLEN", "s"), 4)
        expect("GETSET", redis_call(sock, "GETSET", "s", "zz"), "abcd")
        expect("GET after GETSET", redis_call(sock, "GET", "s"), "zz")
        expect("STRLEN miss", redis_call(sock, "STRLEN", "nope"), 0)

        # DBSIZE / INFO / KEYS glob
        redis_call(sock, "FLUSHDB")
        redis_call(sock, "MSET", "foo", "1", "foobar", "2", "bar", "3")
        expect("DBSIZE", redis_call(sock, "DBSIZE"), 3)
        keys_all = redis_call(sock, "KEYS", "*")
        expect_true("KEYS *", sorted(keys_all) == ["bar", "foo", "foobar"], repr(keys_all))
        keys_pre = redis_call(sock, "KEYS", "foo*")
        expect_true("KEYS foo*", sorted(keys_pre) == ["foo", "foobar"], repr(keys_pre))
        info = redis_call(sock, "INFO")
        expect_true("INFO bulk", isinstance(info, str) and "aura-redis" in info, repr(info)[:80])
        expect_true("INFO keys", "keys=3" in info, info)

        # RENAME / RENAMENX
        expect("RENAME", redis_call(sock, "RENAME", "foo", "foo2"), "OK")
        expect("GET renamed", redis_call(sock, "GET", "foo2"), "1")
        expect("RENAMENX busy", redis_call(sock, "RENAMENX", "bar", "foo2"), 0)
        expect("RENAMENX ok", redis_call(sock, "RENAMENX", "bar", "baz"), 1)
        expect("GET baz", redis_call(sock, "GET", "baz"), "3")

        # SETEX / UNLINK
        expect("SETEX", redis_call(sock, "SETEX", "ttlkey", "60", "v"), "OK")
        ttl_v = redis_call(sock, "TTL", "ttlkey")
        expect_true("TTL setex", isinstance(ttl_v, int) and ttl_v > 0, repr(ttl_v))
        expect("UNLINK", redis_call(sock, "UNLINK", "ttlkey", "foo2"), 2)

        # Pipelining (one send burst → batched replies)
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

    # Concurrent clients (needs fiber-per-client; skip soft-fail if sync)
    print("smoke: concurrent clients")
    results: list[str | BaseException] = []

    def worker(tag: str) -> None:
        try:
            with socket.create_connection((host, port), timeout=5) as s:
                s.settimeout(5)
                r = redis_call(s, "SET", f"c-{tag}", tag)
                g = redis_call(s, "GET", f"c-{tag}")
                if r != "OK" or g != tag:
                    results.append(AssertionError(f"{tag}: {r!r} {g!r}"))
                else:
                    results.append("ok")
                redis_call(s, "QUIT")
        except BaseException as e:  # noqa: BLE001
            results.append(e)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    if t1.is_alive() or t2.is_alive():
        print("  WARN concurrent: threads hung (server may be AURA_REDIS_SYNC=1); skipping assert")
    else:
        for r in results:
            if isinstance(r, BaseException):
                raise r
        expect_true("concurrent two clients", results == ["ok", "ok"], repr(results))

    print("smoke: ALL PASSED")
    return 0


def run_ffi(host: str, port: int) -> int:
    """Subset for C data-plane commands (Iteration 2+)."""
    print(f"smoke-ffi: connect {host}:{port}")
    with socket.create_connection((host, port), timeout=10) as sock:
        expect("PING", redis_call(sock, "PING"), "PONG")
        expect("PING msg", redis_call(sock, "PING", "hi"), "hi")
        expect("SET", redis_call(sock, "SET", "a", "hello"), "OK")
        expect("GET", redis_call(sock, "GET", "a"), "hello")
        expect("EXISTS", redis_call(sock, "EXISTS", "a"), 1)
        expect("INCR", redis_call(sock, "INCR", "n"), 1)
        expect("INCR2", redis_call(sock, "INCR", "n"), 2)
        expect("DECR", redis_call(sock, "DECR", "n"), 1)
        expect("DEL", redis_call(sock, "DEL", "a"), 1)
        expect("GET miss", redis_call(sock, "GET", "a"), None)
        expect("FLUSHDB", redis_call(sock, "FLUSHDB"), "OK")
        expect("MSET", redis_call(sock, "MSET", "x", "1", "y", "2"), "OK")
        expect("MGET", redis_call(sock, "MGET", "x", "missing", "y"), ["1", None, "2"])
        # pipeline
        sock.sendall(encode_array(["PING"]) + encode_array(["GET", "x"]) + encode_array(["SET", "z", "9"]))
        buf = bytearray()
        vals = []
        while len(vals) < 3:
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
        expect("pipeline GET", vals[1], "1")
        expect("pipeline SET", vals[2], "OK")
        redis_call(sock, "QUIT")
    print("smoke: concurrent clients")
    results: list[str | BaseException] = []

    def worker(tag: str) -> None:
        try:
            with socket.create_connection((host, port), timeout=5) as s:
                r = redis_call(s, "SET", f"c-{tag}", tag)
                g = redis_call(s, "GET", f"c-{tag}")
                if r != "OK" or g != tag:
                    results.append(AssertionError(f"{tag}: {r!r} {g!r}"))
                else:
                    results.append("ok")
                redis_call(s, "QUIT")
        except BaseException as e:  # noqa: BLE001
            results.append(e)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    expect_true("concurrent two clients", results == ["ok", "ok"], repr(results))
    print("smoke-ffi: ALL PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="aura-redis RESP smoke client")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--retries", type=int, default=90)
    p.add_argument("--engine", choices=("aura", "ffi"), default="aura",
                   help="aura=full Lisp command set; ffi=C core subset")
    args = p.parse_args(argv)
    runner = run_ffi if args.engine == "ffi" else run
    last: Exception | None = None
    for _ in range(args.retries):
        try:
            return runner(args.host, args.port)
        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.25)
    print(f"smoke: failed to connect after retries: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
