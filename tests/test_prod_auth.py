#!/usr/bin/env python3
"""P0.4 — AUTH / requirepass / protected-mode."""
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
from smoke_client import Incomplete, decode_one, encode_array, redis_call  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26904"))


def recv_err(sock: socket.socket, timeout: float = 5.0) -> str:
    sock.settimeout(timeout)
    buf = bytearray()
    while True:
        try:
            v, c = decode_one(buf)
            del buf[:c]
            if isinstance(v, Exception):
                return str(v)
            raise AssertionError(f"expected error, got {v!r}")
        except Incomplete:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf.extend(chunk)


def start_server(
    *,
    requirepass: str | None = None,
    bind: str = "127.0.0.1",
    protected_mode: str = "yes",
    env_pass: str | None = None,
) -> subprocess.Popen:
    kill_tcp_port(PORT)
    time.sleep(0.05)
    subprocess.check_call(
        [str(ROOT / "scripts/build-native.sh")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    bin_path = ROOT / "native/build/aura_redis_server"
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    if env_pass is not None:
        env["AURA_REDIS_REQUIREPASS"] = env_pass
    else:
        env.pop("AURA_REDIS_REQUIREPASS", None)
    log = Path(f"/tmp/test-prod-auth-{PORT}.log")
    args = [
        str(bin_path),
        "--port",
        str(PORT),
        "--evict",
        "lru",
        "--bind",
        bind,
        "--protected-mode",
        protected_mode,
    ]
    if requirepass is not None:
        args += ["--requirepass", requirepass]
    proc = subprocess.Popen(
        args, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env
    )
    for _ in range(80):
        txt = log.read_text(errors="replace")
        if "listening on" in txt:
            return proc
        if proc.poll() is not None:
            raise RuntimeError(txt)
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def test_requirepass_flag() -> None:
    proc = start_server(requirepass="s3cret")
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "PING") == "PONG"
            sock.sendall(encode_array(["GET", "x"]))
            err = recv_err(sock)
            assert "NOAUTH" in err, err
            try:
                redis_call(sock, "AUTH", "wrong")
                raise AssertionError("wrong password should fail")
            except RuntimeError as e:
                assert "WRONGPASS" in str(e), e
            assert redis_call(sock, "AUTH", "s3cret") == "OK"
            assert redis_call(sock, "SET", "k", "v") == "OK"
            assert redis_call(sock, "GET", "k") == "v"
            # ACL-style AUTH user pass
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "AUTH", "default", "s3cret") == "OK"
            assert redis_call(sock, "PING") == "PONG"
        print("requirepass flag OK")
    finally:
        stop(proc)


def test_requirepass_env() -> None:
    proc = start_server(env_pass="envpass")
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            try:
                redis_call(sock, "SET", "a", "1")
                raise AssertionError("SET should NOAUTH")
            except RuntimeError as e:
                assert "NOAUTH" in str(e), e
            assert redis_call(sock, "AUTH", "envpass") == "OK"
            assert redis_call(sock, "SET", "a", "1") == "OK"
        print("requirepass env OK")
    finally:
        stop(proc)


def test_no_password_open() -> None:
    proc = start_server()
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "SET", "a", "1") == "OK"
            try:
                redis_call(sock, "AUTH", "x")
                raise AssertionError("AUTH without requirepass should error")
            except RuntimeError as e:
                assert "AUTH" in str(e) or "password" in str(e).lower(), e
        print("no-password open OK")
    finally:
        stop(proc)


def test_protected_mode_remote() -> None:
    """Bind 0.0.0.0, protected-mode on, no password → non-loopback peer denied."""
    # Pick a non-loopback local address
    host_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    if not host_ip or host_ip.startswith("127."):
        print("protected-mode remote SKIP (no non-loopback IP)")
        return

    proc = start_server(bind="0.0.0.0", protected_mode="yes")
    try:
        # Loopback still works
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            assert redis_call(sock, "PING") == "PONG"
            assert redis_call(sock, "SET", "ok", "1") == "OK"

        # Connect via non-loopback → DENIED then close
        sock = socket.create_connection((host_ip, PORT), timeout=5)
        sock.settimeout(3)
        buf = bytearray()
        try:
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            msg = buf.decode(errors="replace")
            assert "DENIED" in msg or "protected" in msg.lower(), msg
            print(f"protected-mode remote DENIED via {host_ip} OK")
        finally:
            sock.close()
    finally:
        stop(proc)


def test_protected_mode_off_or_password() -> None:
    host_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    if not host_ip or host_ip.startswith("127."):
        print("protected-mode bypass SKIP")
        return

    proc = start_server(bind="0.0.0.0", protected_mode="no")
    try:
        with socket.create_connection((host_ip, PORT), timeout=5) as sock:
            assert redis_call(sock, "PING") == "PONG"
        print("protected-mode no allows remote OK")
    finally:
        stop(proc)

    proc = start_server(bind="0.0.0.0", protected_mode="yes", requirepass="p")
    try:
        with socket.create_connection((host_ip, PORT), timeout=5) as sock:
            assert redis_call(sock, "AUTH", "p") == "OK"
            assert redis_call(sock, "PING") == "PONG"
        print("protected-mode + requirepass allows remote OK")
    finally:
        stop(proc)


def main() -> int:
    test_requirepass_flag()
    test_requirepass_env()
    test_no_password_open()
    test_protected_mode_remote()
    test_protected_mode_off_or_password()
    print("test_prod_auth: ALL PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        kill_tcp_port(PORT)
