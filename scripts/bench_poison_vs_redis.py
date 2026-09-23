#!/usr/bin/env python3
"""E2 / A19 — poison-key (unique-SET) flood: keep* hit% vs Redis fixed policies.

Compares:
  - redis allkeys-lru / allkeys-lfu
  - aura static EVICT=lru / lfu
  - aura + policy_agent with A19 poison defense ON (and OFF control)

Metric: keep* useful-GET hit% after unique-SET poison + light poison GET noise.
Never claims memtier/adaptive throughput wins.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from smoke_client import redis_call  # noqa: E402
from _agentutil import start_agent, stop_agent, agent_logs  # noqa: E402
from _portutil import kill_tcp_port  # noqa: E402

IMG_REDIS = os.environ.get("REDIS_IMAGE", "redis:7-alpine")
IMG_DEV = os.environ.get("AURA_DEV_IMAGE", "ghcr.io/cybrid-systems/dev:v1.0.7")
SERVER = ROOT / "native/build/aura_redis_server"


@dataclass
class Row:
    engine: str
    policy: str
    keep_hits: int
    keep_misses: int
    unique_sets: int = 0
    notes: str = ""

    @property
    def hit_pct(self) -> float:
        t = self.keep_hits + self.keep_misses
        return (100.0 * self.keep_hits / t) if t else 0.0


def wait_listen(port: int, timeout: float = 10.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), 0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} not listening")


def info_field(info: str, key: str) -> str:
    for ln in info.splitlines():
        if ln.startswith(key + ":"):
            return ln.split(":", 1)[1]
    return ""


def set_ok(s: socket.socket, key: str, val: str) -> bool:
    try:
        redis_call(s, "SET", key, val)
        return True
    except RuntimeError as e:
        if "OOM" not in str(e):
            raise
        try:
            redis_call(s, "SET", key, val)
            return True
        except RuntimeError as e2:
            if "OOM" not in str(e2):
                raise
            return False


def run_poison_workload(
    s: socket.socket,
    *,
    nkeep: int,
    waves: int,
    per_wave: int,
    poison_get_pct: float,
    settle: float,
    reseat_headroom: int | None = None,
) -> Tuple[int, int, int]:
    """Seed keep*, flood unique poison (+ light GET noise), measure keep hits."""
    try:
        redis_call(s, "FLUSHDB")
    except RuntimeError:
        pass
    if reseat_headroom is not None:
        try:
            info = str(redis_call(s, "INFO", "memory"))
            base = 0
            for ln in info.splitlines():
                if ln.startswith("used_memory:"):
                    base = int(ln.split(":")[1])
                    break
            redis_call(s, "CONFIG", "SET", "maxmemory", str(base + reseat_headroom))
        except Exception:
            pass
    keep_val = "K" * 400
    poison_val = "p" * 200
    for i in range(nkeep):
        set_ok(s, f"keep{i:04d}", keep_val)
        for _ in range(3):
            redis_call(s, "GET", f"keep{i:04d}")
    for w in range(waves):
        for i in range(per_wave):
            k = f"poison{w}_{i}"
            set_ok(s, k, poison_val)
            # light scanner GET (~poison_get_pct of keys once)
            if (i % 10) < int(max(poison_get_pct, 0.1) * 10):
                redis_call(s, "GET", k)
        time.sleep(0.5)
    time.sleep(settle)
    hits = misses = 0
    for i in range(nkeep):
        v = redis_call(s, "GET", f"keep{i:04d}")
        if v is None or v == "" or v == b"":
            misses += 1
        else:
            hits += 1
    uniq = 0
    try:
        uniq = int(info_field(str(redis_call(s, "INFO")), "unique_sets") or "0")
    except Exception:
        pass
    return hits, misses, uniq


def start_redis(port: int, policy: str, headroom: int) -> str:
    kill_tcp_port(port)
    name = f"ar-poison-redis-{port}-{os.getpid()}"
    cid = subprocess.check_output(
        [
            "sudo", "docker", "run", "-d", "--network", "host", "--name", name,
            IMG_REDIS,
            "redis-server",
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--maxmemory-policy", policy,
        ],
        text=True,
    ).strip()
    wait_listen(port)
    s = socket.create_connection(("127.0.0.1", port), 5)
    try:
        info = redis_call(s, "INFO", "memory")
        base = 0
        for ln in str(info).splitlines():
            if ln.startswith("used_memory:"):
                base = int(ln.split(":")[1])
                break
        cap = base + headroom
        assert redis_call(s, "CONFIG", "SET", "maxmemory", str(cap)) == "OK"
        assert redis_call(s, "CONFIG", "SET", "maxmemory-policy", policy) == "OK"
        print(f"   redis baseline_used={base} maxmemory={cap} policy={policy}")
    finally:
        s.close()
    return cid


def start_aura(port: int, maxmemory: int, evict: str = "lru") -> subprocess.Popen:
    kill_tcp_port(port)
    if not SERVER.exists():
        subprocess.check_call([str(ROOT / "scripts/build-native.sh")])
    env = os.environ.copy()
    env["AURA_REDIS_DENY_PLUGIN"] = "1"
    log = Path(f"/tmp/ar-poison-aura-{port}.log")
    proc = subprocess.Popen(
        [str(SERVER), "--port", str(port), "--evict", evict, "--maxmemory", str(maxmemory)],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    wait_listen(port)
    return proc


def start_aura_agent(
    port: int,
    *,
    poison_defense: bool,
    seed: str = "normal",
) -> Tuple[str, Path, Path]:
    hb = ROOT / f".ar-poison-bench-hb-{port}"
    audit = ROOT / f".ar-poison-bench-audit-{port}.log"
    boot = ROOT / f".ar-agent-booted-{port}.flag"
    pin = Path(str(hb) + ".pin")
    for p in (hb, audit, boot, pin):
        p.unlink(missing_ok=True)
    log = Path(f"/tmp/ar-poison-agent-{port}.log")
    env = {
        "AURA_REDIS_PORT": str(port),
        "AURA_REDIS_HOST": "127.0.0.1",
        "AURA_REDIS_POLICY_MS": "80",
        "AURA_REDIS_DENY_PLUGIN": "1",
        # fitness mutate OFF — threshold mutate reloads file and kills the loop
        "AURA_REDIS_FITNESS_MUTATE": "0",
        "AURA_REDIS_SEED_PROFILE": seed,
        "AURA_REDIS_POISON_DEFENSE": "1" if poison_defense else "0",
        "AURA_REDIS_POISON_UNIQUE_RATE": "10",
    }
    cid = start_agent(
        env=env,
        log_path=log,
        path_env={
            "AURA_REDIS_POLICY_HEARTBEAT": hb,
            "AURA_REDIS_POLICY_AUDIT": audit,
        },
    )
    t0 = time.time()
    while time.time() - t0 < 20:
        text = agent_logs(cid, log)
        if "PING" in text or boot.exists():
            break
        time.sleep(0.1)
    time.sleep(0.5)
    return cid, hb, audit


def run_case(
    engine: str,
    policy: str,
    port: int,
    maxmemory: int,
    headroom: int,
    wl_kwargs: dict,
) -> Row:
    print(f">> {engine} {policy} ...", flush=True)
    if engine == "redis":
        full = "allkeys-lru" if policy == "lru" else "allkeys-lfu"
        cid = start_redis(port, full, headroom)
        try:
            s = socket.create_connection(("127.0.0.1", port), 5)
            try:
                h, m, u = run_poison_workload(
                    s, reseat_headroom=headroom, **wl_kwargs
                )
            finally:
                try:
                    redis_call(s, "QUIT")
                except Exception:
                    pass
                s.close()
            return Row("redis", policy, h, m, u, f"maxmemory-policy={full}")
        finally:
            subprocess.run(["sudo", "docker", "rm", "-f", cid], capture_output=True)
            kill_tcp_port(port)
    # aura
    adaptive = policy.startswith("adaptive")
    # explicit suffixes: adaptive_poison_on | adaptive_poison_off
    defense_off = policy.endswith("_off") or policy.endswith("disabled")
    defense_on = (not defense_off) and (
        policy.endswith("_on") or policy in ("adaptive_poison", "adaptive_poison_on")
    )
    evict = "lfu" if policy == "lfu" else "lru"
    proc = start_aura(port, maxmemory, evict=evict)
    agent = None
    notes = f"EVICT={evict}"
    try:
        if adaptive:
            use_defense = not defense_off
            seed = "normal" if use_defense else "aggressive"
            agent, hb, audit = start_aura_agent(
                port, poison_defense=use_defense, seed=seed
            )
            notes = (
                f"policy_agent poison_defense={'ON' if use_defense else 'OFF'} "
                f"seed={seed} fitness=off"
            )
        s = socket.create_connection(("127.0.0.1", port), 5)
        try:
            # A19: arm defense BEFORE seeding keep* so LFU is live under the flood.
            # Otherwise LRU wipe during wave-1 races the 80ms policy tick.
            if adaptive and use_defense:
                for i in range(80):
                    set_ok(s, f"arm{i}", "a" * 32)
                time.sleep(1.2)
                text0 = agent_logs(agent, Path(f"/tmp/ar-poison-agent-{port}.log"))
                # drop arm keys; keep* seed follows under (hopefully) LFU
                try:
                    redis_call(s, "FLUSHDB")
                except RuntimeError:
                    pass
                time.sleep(0.2)
            h, m, u = run_poison_workload(s, **wl_kwargs)
            if adaptive and agent:
                text = agent_logs(agent, Path(f"/tmp/ar-poison-agent-{port}.log"))
                audit_txt = ""
                ap = ROOT / f".ar-poison-bench-audit-{port}.log"
                if ap.exists():
                    audit_txt = ap.read_text(errors="replace")
                defended = (
                    "poison-storm" in text
                    or "unique_set_storm" in audit_txt
                    or "poison-defense" in text
                    or "to=defensive" in audit_txt
                )
                if not defense_off:
                    notes += f" defended={'yes' if defended else 'no'}"
                else:
                    notes += " defended=n/a(off)"
                # capture explain if present
                try:
                    info = str(redis_call(s, "INFO"))
                    mid = info_field(info, "explain_mid")
                    reason = info_field(info, "explain_reason")
                    if mid or reason:
                        notes += f" explain_mid={mid} reason={reason}"
                except Exception:
                    pass
        finally:
            try:
                redis_call(s, "QUIT")
            except Exception:
                pass
            s.close()
        label = policy
        return Row("aura", label, h, m, u, notes)
    finally:
        if agent:
            stop_agent(agent)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        kill_tcp_port(port)


def md_table(rows: List[Row]) -> str:
    lines = [
        "| Engine | Policy | keep* hit% | Hits | Misses | unique_sets | Notes |",
        "|--------|--------|------------|------|--------|-------------|-------|",
    ]
    for r in rows:
        lines.append(
            f"| {r.engine} | {r.policy} | {r.hit_pct:.1f} | {r.keep_hits} | "
            f"{r.keep_misses} | {r.unique_sets} | {r.notes or '—'} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aura-port", type=int, default=26580)
    ap.add_argument("--redis-port", type=int, default=26579)
    ap.add_argument("--maxmemory", type=int, default=120_000)
    ap.add_argument("--headroom", type=int, default=int(os.environ.get("AURA_REDIS_REDIS_HEADROOM", "45000")))
    ap.add_argument("--nkeep", type=int, default=20)
    ap.add_argument("--waves", type=int, default=8)
    ap.add_argument("--per-wave", type=int, default=100)
    ap.add_argument("--poison-get-pct", type=float, default=0.3)
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--json-out", type=str, default="")
    ap.add_argument("--md-out", type=str, default="")
    args = ap.parse_args()

    os.environ["AURA_REDIS_DENY_PLUGIN"] = "1"
    wl = dict(
        nkeep=args.nkeep,
        waves=args.waves,
        per_wave=args.per_wave,
        poison_get_pct=args.poison_get_pct,
        settle=args.settle,
    )

    cases = [
        ("redis", "lru", args.redis_port),
        ("redis", "lfu", args.redis_port),
        ("aura", "lru", args.aura_port),
        ("aura", "lfu", args.aura_port),
        ("aura", "adaptive_poison_off", args.aura_port),
        ("aura", "adaptive_poison_on", args.aura_port),
    ]
    rows: List[Row] = []
    for eng, pol, port in cases:
        mm = args.maxmemory
        hr = args.headroom
        r = run_case(eng, pol, port, mm, hr, wl)
        print(f"   keep_hit={r.hit_pct:.1f}% {r.keep_hits}/{r.keep_hits+r.keep_misses} {r.notes}")
        rows.append(r)

    md = md_table(rows)
    print("\n# A19 poison-key keep* scoreboard\n")
    print(md)
    print(
        "\nRedis = fixed allkeys-lru/lfu only. Aura adaptive_poison uses "
        "policy_agent A19 defensive mutate (lfu|flat|soft, refuse pin).\n"
    )

    payload = {
        "kind": "poison_vs_redis",
        "maxmemory": args.maxmemory,
        "headroom": args.headroom,
        "workload": wl,
        "results": [asdict(r) | {"hit_pct": r.hit_pct} for r in rows],
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2))
        print(f"Wrote {args.json_out}")
    if args.md_out:
        Path(args.md_out).write_text("# A19 poison-key keep* scoreboard\n\n" + md + "\n")
        print(f"Wrote {args.md_out}")
    print(json.dumps({"summary": "ok", "n": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
