#!/usr/bin/env python3
"""Ops — CLIENT LIST / ID / SETNAME (+ optional KILL)."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26929"))
BIN = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"


def build() -> None:
    if os.environ.get("AURA_REDIS_SKIP_BUILD") == "1":
        return
    subprocess.check_call(
        [str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
    )


def start_server() -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    build()
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    env["AURA_REDIS_CONFIG"] = ""
    log = Path(f"/tmp/test-prod-client-list-{PORT}.log")
    proc = subprocess.Popen(
        [
            str(BIN),
            "--port",
            str(PORT),
            "--evict",
            "noop",
            "--maxmemory",
            "100000",
            "--config",
            "",
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(100):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    kill_tcp_port(PORT)


def parse_client_lines(bulk: str) -> list[dict[str, str]]:
    rows = []
    for line in bulk.splitlines():
        if not line.strip():
            continue
        d: dict[str, str] = {}
        for part in line.split():
            if "=" in part:
                k, v = part.split("=", 1)
                d[k] = v
        rows.append(d)
    return rows


def main() -> int:
    proc = start_server()
    try:
        s1 = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        s2 = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        try:
            assert redis_call(s1, "PING") == "PONG"
            cid1 = redis_call(s1, "CLIENT", "ID")
            assert isinstance(cid1, int) and cid1 >= 1, cid1
            assert redis_call(s1, "CLIENT", "SETNAME", "ops-a") == "OK"
            assert redis_call(s2, "PING") == "PONG"
            cid2 = redis_call(s2, "CLIENT", "ID")
            assert isinstance(cid2, int) and cid2 != cid1, (cid1, cid2)
            assert redis_call(s2, "CLIENT", "SETNAME", "ops-b") == "OK"

            raw = redis_call(s1, "CLIENT", "LIST")
            assert isinstance(raw, str) and raw, raw
            rows = parse_client_lines(raw)
            assert len(rows) >= 2, rows
            by_id = {int(r["id"]): r for r in rows if "id" in r}
            assert cid1 in by_id and cid2 in by_id
            assert "addr" in by_id[cid1] and ":" in by_id[cid1]["addr"]
            assert by_id[cid1].get("name") == "ops-a"
            assert by_id[cid2].get("name") == "ops-b"
            assert "fd" in by_id[cid1]
            assert "flags" in by_id[cid1]
            assert by_id[cid1].get("db") == "0"
            # last cmd on s1 was CLIENT LIST
            assert by_id[cid1].get("cmd") in ("client", "list") or "client" in (
                by_id[cid1].get("cmd") or ""
            )

            # KILL by id — close s2 from s1
            assert redis_call(s1, "CLIENT", "KILL", "ID", str(cid2)) == "OK"
            time.sleep(0.15)
            s2.settimeout(1)
            dead = False
            try:
                s2.sendall(b"*1\r\n$4\r\nPING\r\n")
                buf = b""
                while True:
                    chunk = s2.recv(256)
                    if not chunk:
                        dead = True
                        break
                    buf += chunk
                    if b"+PONG" in buf:
                        break
            except (ConnectionError, OSError, socket.timeout):
                dead = True
            assert dead, "killed client should be closed"

            raw2 = redis_call(s1, "CLIENT", "LIST")
            rows2 = parse_client_lines(raw2)
            ids2 = {int(r["id"]) for r in rows2 if "id" in r}
            assert cid2 not in ids2
            assert cid1 in ids2
            print("PASS CLIENT LIST/ID/SETNAME/KILL")
        finally:
            try:
                s1.close()
            except Exception:
                pass
            try:
                s2.close()
            except Exception:
                pass
    finally:
        stop(proc)
    print("test_prod_client_list: ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
