#!/usr/bin/env python3
"""SET/GET ops/sec bench against local aura-redis (non-failing CI helper)."""
from __future__ import annotations

import argparse
import socket
import sys
import time


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


class Incomplete(Exception):
    pass


def redis_call(sock: socket.socket, *args: str, timeout: float = 120.0):
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


def pipeline_roundtrip(sock: socket.socket, cmds: list[list[str]], timeout: float = 300.0):
    sock.settimeout(timeout)
    payload = b"".join(encode_array(c) for c in cmds)
    sock.sendall(payload)
    buf = bytearray()
    vals = []
    while len(vals) < len(cmds):
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
    return vals


def bench(host: str, port: int, n: int, pipeline: int) -> None:
    with socket.create_connection((host, port), timeout=10) as sock:
        redis_call(sock, "PING")
        redis_call(sock, "FLUSHDB")
        # sequential SET
        t0 = time.perf_counter()
        for i in range(n):
            redis_call(sock, "SET", f"k{i}", f"v{i}")
        t1 = time.perf_counter()
        set_ops = n / (t1 - t0)
        # sequential GET
        t0 = time.perf_counter()
        for i in range(n):
            redis_call(sock, "GET", f"k{i}")
        t1 = time.perf_counter()
        get_ops = n / (t1 - t0)
        print(f"bench: N={n} pipeline={pipeline}")
        print(f"  SET sequential: {set_ops:.1f} ops/sec")
        print(f"  GET sequential: {get_ops:.1f} ops/sec")
        if pipeline > 0:
            redis_call(sock, "FLUSHDB")
            batches = max(1, n // pipeline)
            total = batches * pipeline
            t0 = time.perf_counter()
            for b in range(batches):
                cmds = []
                for j in range(pipeline):
                    idx = b * pipeline + j
                    cmds.append(["SET", f"p{idx}", f"v{idx}"])
                pipeline_roundtrip(sock, cmds)
            t1 = time.perf_counter()
            set_pipe = total / (t1 - t0)
            t0 = time.perf_counter()
            for b in range(batches):
                cmds = []
                for j in range(pipeline):
                    idx = b * pipeline + j
                    cmds.append(["GET", f"p{idx}"])
                pipeline_roundtrip(sock, cmds)
            t1 = time.perf_counter()
            get_pipe = total / (t1 - t0)
            print(f"  SET pipelined:  {set_pipe:.1f} ops/sec")
            print(f"  GET pipelined:  {get_pipe:.1f} ops/sec")
        redis_call(sock, "FLUSHDB")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="aura-redis SET/GET bench")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("-n", type=int, default=2000, help="ops per phase")
    p.add_argument("--pipeline", type=int, default=50, help="pipeline batch size (0=off)")
    args = p.parse_args(argv)
    try:
        bench(args.host, args.port, args.n, args.pipeline)
        return 0
    except Exception as e:
        print(f"bench: FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
