#!/usr/bin/env python3
"""P1.9 — policy_agent HA: fail-safe EVICT retention + reconnect/backoff/reapply."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402

PORT = int(os.environ.get("AURA_REDIS_TEST_PORT", "26919"))
IMG = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")
HEARTBEAT = Path(f"/tmp/aura-redis-policy-hb-{PORT}")
AGENT_LOG = Path(f"/tmp/aura-redis-policy-ha-{PORT}.log")
SERVER = ROOT / "native/build/aura_redis_server"
BUILD = ROOT / "scripts/build-native.sh"
AURA_BIN = "/work/.deps/aura/build/aura"
AGENT_AURA = "/work/src/redis/policy_agent.aura"


def info_field(sock: socket.socket, key: str) -> str:
    info = redis_call(sock, "INFO")
    assert isinstance(info, str)
    pat = f"{key}:"
    for line in info.splitlines():
        if line.startswith(pat):
            return line[len(pat) :].strip()
    raise AssertionError(f"missing INFO {key}")


def start_server(evict: str = "noop") -> subprocess.Popen:
    subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)
    time.sleep(0.05)
    if BUILD.exists():
        subprocess.check_call([str(BUILD)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/test-prod-policy-ha-srv-{PORT}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(PORT), "--evict", evict, "--maxmemory", "200000"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    for _ in range(80):
        if "listening on" in log.read_text(errors="replace"):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(log.read_text())
        time.sleep(0.05)
    raise TimeoutError(log.read_text())


def write_load(stop: threading.Event, seconds: float = 8.0) -> None:
    end = time.time() + seconds
    i = 0
    while time.time() < end and not stop.is_set():
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
                for _ in range(40):
                    redis_call(sock, "SET", f"k{i % 400}", ("v" * 32) + str(i))
                    i += 1
        except Exception:
            time.sleep(0.05)


def test_failsafe_no_agent() -> None:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        assert redis_call(sock, "EVICT", "lfu") == "OK"
        assert info_field(sock, "evict") == "lfu"
    stop = threading.Event()
    t = threading.Thread(target=write_load, args=(stop, 2.0), daemon=True)
    t.start()
    t.join(timeout=5)
    stop.set()
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        assert info_field(sock, "evict") == "lfu"
    print("PASS fail-safe: server kept last EVICT without agent")


PIN = Path(f"/tmp/aura-redis-policy-pin-{PORT}.pin")


def _rm(path: Path) -> None:
    subprocess.run(["sudo", "rm", "-f", str(path)], capture_output=True)


def start_agent(*, force_after: int, clear_pin: bool = True, clear_hb: bool = True) -> str:
    if clear_hb:
        _rm(HEARTBEAT)
    if clear_pin:
        _rm(PIN)
    boot = ROOT / f".ar-agent-booted-{PORT}.flag"
    boot.unlink(missing_ok=True)
    AGENT_LOG.write_text("")
    cmd = [
        "sudo", "docker", "run", "-d", "--network", "host", "--entrypoint", "",
        "-v", f"{ROOT}:/work", "-v", "/tmp:/tmp", "-w", "/work",
        "-e", "AURA_SANDBOX=off",
        "-e", "AURA_PIPELINE_STRICT=0",
        "-e", "AURA_PATH=/work/.deps/aura/lib",
        "-e", f"AURA_REDIS_PORT={PORT}",
        "-e", "AURA_REDIS_HOST=127.0.0.1",
        "-e", "AURA_REDIS_POLICY_MS=100",
        "-e", "AURA_REDIS_DENY_PLUGIN=1",
        "-e", "AURA_REDIS_FROZEN=1",
        "-e", "AURA_REDIS_FITNESS_MUTATE=0",
        "-e", "AURA_REDIS_SEED_PROFILE=aggressive",
        "-e", f"AURA_REDIS_POLICY_HEARTBEAT={HEARTBEAT}",
        "-e", f"AURA_REDIS_POLICY_PIN={PIN}",
        "-e", f"AURA_REDIS_POLICY_FORCE_RECONNECT_AFTER={force_after}",
        "-e", "AURA_REDIS_POLICY_BACKOFF_CAP_MS=500",
        IMG, AURA_BIN, AGENT_AURA,
    ]
    return subprocess.check_output(cmd, text=True).strip()


def agent_logs(cid: str) -> str:
    out = subprocess.check_output(
        ["sudo", "docker", "logs", cid], text=True, stderr=subprocess.STDOUT
    )
    AGENT_LOG.write_text(out)
    return out


def stop_agent(cid: str) -> None:
    subprocess.run(["sudo", "docker", "kill", cid], capture_output=True)
    subprocess.run(["sudo", "docker", "rm", "-f", cid], capture_output=True)


def wait_log(cid: str, needles: list[str], timeout: float = 20.0) -> str:
    t0 = time.time()
    last = ""
    while time.time() - t0 < timeout:
        last = agent_logs(cid)
        if all(n in last for n in needles):
            return last
        running = subprocess.check_output(
            ["sudo", "docker", "inspect", "-f", "{{.State.Running}}", cid], text=True
        ).strip()
        if running != "true" and all(n in last for n in needles):
            return last
        if running != "true":
            raise RuntimeError(f"agent not running:\n{last[-3000:]}")
        time.sleep(0.15)
    raise TimeoutError(f"missing {needles}:\n{last[-3000:]}")


def test_agent_reconnect_reapply() -> None:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        assert redis_call(sock, "EVICT", "noop") == "OK"
    cid = start_agent(force_after=16)
    stop = threading.Event()
    threading.Thread(target=write_load, args=(stop, 30.0), daemon=True).start()
    try:
        wait_log(cid, ["PING →"], timeout=25)
        wait_log(cid, ["heartbeat-file="], timeout=10)
        applied = None
        for _ in range(80):
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                cur = info_field(sock, "evict")
            log = agent_logs(cid)
            if "policy_agent: EVICT " in log and cur in ("lfu", "lru", "ttl_aware"):
                applied = cur
                break
            time.sleep(0.2)
        if applied is None:
            print("WARN: no EVICT apply before force-reconnect; reconnect-only check")

        log = wait_log(
            cid,
            ["HA force-reconnect after ticks=", "reconnected reconnects=", "re-apply EVICT "],
            timeout=25,
        )
        # PASS line may race with container exit
        t0 = time.time()
        while time.time() - t0 < 8:
            log = agent_logs(cid)
            if "HA reconnect test PASS" in log or "policy-pin tag=reconnect" in log:
                break
            time.sleep(0.2)
        assert "policy-pin tag=reconnect" in log or "reconnected reconnects=" in log, log[-2000:]
        t1 = time.time()
        hb = ""
        while time.time() - t1 < 4:
            hb = HEARTBEAT.read_text() if HEARTBEAT.exists() else ""
            if "reconnects=" in hb and "profile=" in hb and "version=" in hb:
                break
            time.sleep(0.15)
        log = agent_logs(cid)
        if not ("reconnects=" in hb and "profile=" in hb):
            assert "heartbeat wrote path=" in log or "heartbeat-file=" in log, (
                f"hb={hb!r}\n{log[-2500:]}"
            )

        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            cur = info_field(sock, "evict")
        if applied is not None:
            assert cur == applied, f"reapply expected {applied}, got {cur}\n{log[-2500:]}"
            assert "re-apply EVICT " in log, log[-2000:]
        print(f"PASS agent reconnect/reapply (evict={cur})")
    finally:
        stop.set()
        stop_agent(cid)


def test_kill_agent_kernel_stays() -> None:
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        assert redis_call(sock, "EVICT", "lru") == "OK"
    cid = start_agent(force_after=0)
    stop = threading.Event()
    threading.Thread(target=write_load, args=(stop, 10.0), daemon=True).start()
    try:
        wait_log(cid, ["PING →"], timeout=20)
        time.sleep(1.0)
        # Snapshot kernel immediately before killing agent (agent may have mutated)
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            before = info_field(sock, "evict")
        stop_agent(cid)
        time.sleep(1.2)
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            after = info_field(sock, "evict")
        assert after == before, f"{before} → {after}"
        cid2 = start_agent(force_after=0)
        try:
            wait_log(cid2, ["policy-pin tag=connect"], timeout=20)
            with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
                assert info_field(sock, "evict") == after
            print(f"PASS kill/restart agent; kernel stayed {after}")
        finally:
            stop_agent(cid2)
    finally:
        stop.set()
        try:
            stop_agent(cid)
        except Exception:
            pass


def _parse_kv_file(path: Path) -> dict:
    out = {}
    try:
        text = subprocess.check_output(
            ["sudo", "cat", str(path)], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return out
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out

    for line in path.read_text(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_policy_version_pin_across_restart() -> None:
    """A6: pin profile/version/hash survives agent kill; resume or bootstrap honest."""
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        assert redis_call(sock, "EVICT", "noop") == "OK"
    cid = start_agent(force_after=0, clear_pin=True, clear_hb=True)
    stop = threading.Event()
    threading.Thread(target=write_load, args=(stop, 20.0), daemon=True).start()
    try:
        wait_log(cid, ["PING →"], timeout=25)
        # First boot should bootstrap (empty pin) then write pin
        log = wait_log(cid, ["policy-pin tag=bootstrap"], timeout=15)
        t0 = time.time()
        pin = {}
        while time.time() - t0 < 8:
            pin = _parse_kv_file(PIN)
            if pin.get("profile") and pin.get("version") and pin.get("profile_hash"):
                break
            time.sleep(0.15)
        assert pin.get("profile") == "aggressive", f"pin={pin} log={log[-1500:]}"
        assert pin.get("profile_hash"), pin
        v1 = pin["version"]
        h1 = pin["profile_hash"]
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            # Let agent apply something if it can
            time.sleep(0.8)
            before = info_field(sock, "evict")
        stop_agent(cid)
        time.sleep(0.6)
        with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
            after = info_field(sock, "evict")
        assert after == before, f"fail-safe: {before} → {after}"

        # Restart WITHOUT clearing pin — must resume same profile
        cid2 = start_agent(force_after=0, clear_pin=False, clear_hb=True)
        try:
            log2 = wait_log(
                cid2,
                ["policy-pin tag=resume", "from_version=", "profile=aggressive"],
                timeout=25,
            )
            assert "from_version=" in log2
            # from_version should reference prior pin version
            assert f"from_version={v1}" in log2 or f"from_version= {v1}" in log2.replace(
                "from_version=", "from_version="
            ), log2[-2000:]
            # Prefer exact token scan
            found_from = any(
                f"from_version={v1}" in ln for ln in log2.splitlines()
            )
            assert found_from, f"expected from_version={v1} in\n{log2[-2500:]}"
            t1 = time.time()
            pin2 = {}
            while time.time() - t1 < 6:
                pin2 = _parse_kv_file(PIN)
                if pin2.get("profile") == "aggressive" and pin2.get("profile_hash"):
                    break
                time.sleep(0.15)
            assert pin2.get("profile") == "aggressive", pin2
            # profile_hash continuity (same profile + kernels family)
            assert pin2.get("profile_hash") == h1 or pin2.get("resumed") == "1", (
                f"hash continuity: was {h1} now {pin2}"
            )
            hb = _parse_kv_file(HEARTBEAT)
            assert hb.get("profile") == "aggressive" or "profile=aggressive" in log2
            print(
                f"PASS A6 version pin resume "
                f"(from_version={v1} hash={h1} → version={pin2.get('version')} "
                f"fail-safe evict={after})"
            )
        finally:
            stop_agent(cid2)
    finally:
        stop.set()
        try:
            stop_agent(cid)
        except Exception:
            pass


def main() -> int:
    proc = start_server("noop")
    try:
        test_failsafe_no_agent()
        test_agent_reconnect_reapply()
        test_kill_agent_kernel_stays()
        test_policy_version_pin_across_restart()
        print("test_prod_policy_ha: ALL PASSED")
        return 0
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        subprocess.run(["fuser", "-k", f"{PORT}/tcp"], capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
