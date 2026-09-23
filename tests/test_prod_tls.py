#!/usr/bin/env python3
"""P2.15 — native TLS accept: SET/GET over ssl.SSLContext."""
from __future__ import annotations

import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26915"))
TLS_PORT = PORT + 1
BIN = ROOT / "native/build/aura_redis_server"


def build() -> None:
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def gen_certs(td: Path) -> tuple[Path, Path]:
    cert = td / "server.crt"
    key = td / "server.key"
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert, key


def start(port: int, tls_port: int, cert: Path, key: Path) -> tuple[subprocess.Popen, Path]:
    kill_tcp_port(port)
    kill_tcp_port(tls_port)
    time.sleep(0.05)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-tls-{port}.log")
    proc = subprocess.Popen(
        [
            str(BIN),
            "--port",
            str(port),
            "--tls-port",
            str(tls_port),
            "--tls-cert-file",
            str(cert),
            "--tls-key-file",
            str(key),
            "--evict",
            "lru",
        ],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(100):
        text = log.read_text(errors="replace")
        if "listening on" in text and "TLS listening" in text:
            return proc, log
        if proc.poll() is not None:
            raise RuntimeError(text)
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def tls_connect(port: int) -> ssl.SSLSocket:
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(raw, server_hostname="localhost")


def info_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    build()
    # Soft skip if binary has no TLS (OpenSSL missing at build)
    with tempfile.TemporaryDirectory(prefix="aura-tls-") as td:
        td_path = Path(td)
        cert, key = gen_certs(td_path)
        proc, log = start(PORT, TLS_PORT, cert, key)
        try:
            # Cleartext still works (policy_agent path)
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "PING") == "PONG"
                inf = info_map(redis_call(sock, "INFO"))
                assert inf.get("tls_enabled") == "yes", inf
                assert inf.get("tls_port") == str(TLS_PORT), inf

            # TLS SET/GET
            with tls_connect(TLS_PORT) as sock:
                assert redis_call(sock, "PING") == "PONG"
                assert redis_call(sock, "SET", "tls-k", "tls-v") == "OK"
                assert redis_call(sock, "GET", "tls-k") == "tls-v"

            # Visible on cleartext too (same keyspace)
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert redis_call(sock, "GET", "tls-k") == "tls-v"

            print("test_prod_tls: OK")
            return 0
        finally:
            stop(proc)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print("test_prod_tls FAIL:", e, file=sys.stderr)
        raise
