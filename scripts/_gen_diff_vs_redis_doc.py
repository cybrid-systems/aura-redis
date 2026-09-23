#!/usr/bin/env python3
"""Assemble docs/diff-vs-redis.md from bench-diff-vs-redis.sh artifact dir."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def read_text(p: Path) -> str:
    return p.read_text(errors="replace") if p.exists() else ""


def parse_marathon(txt: str) -> List[Dict[str, Any]]:
    """Extract policy rows from bench_dynamic_evict / bench_regret output."""
    rows: List[Dict[str, Any]] = []
    # Look for summary lines like: phase_marathon adaptive hit=100.0% useful=1956 ...
    for ln in txt.splitlines():
        m = re.search(
            r"(?:policy[= ]+|^\s*)(lru|lfu|adaptive|adaptive_\w+)\b.*?hit[= ]+(\d+(?:\.\d+)?)%.*?useful[= ]*(\d+)",
            ln,
            re.I,
        )
        if m:
            rows.append(
                {
                    "policy": m.group(1),
                    "hit_pct": float(m.group(2)),
                    "useful": int(m.group(3)),
                    "raw": ln.strip(),
                }
            )
    # Fallback: table-ish "lru | 81.8% | 1600"
    if not rows:
        for ln in txt.splitlines():
            m = re.match(
                r"\|\s*(lru|lfu|adaptive)\s*\|\s*\*?\*?(?P<h>\d+(?:\.\d+)?)%?\*?\*?\s*\|\s*(?P<u>\d+)",
                ln,
            )
            if m:
                rows.append(
                    {
                        "policy": m.group(1),
                        "hit_pct": float(m.group("h")),
                        "useful": int(m.group("u")),
                        "raw": ln.strip(),
                    }
                )
    # Another common format from bench_dynamic_evict final block
    if not rows:
        block = re.search(
            r"phase_marathon.*?(?=^\S|\Z)", txt, re.S | re.M
        )
        chunk = block.group(0) if block else txt
        for pol in ("adaptive", "lru", "lfu"):
            m = re.search(
                rf"{pol}[^\n]*?(\d+(?:\.\d+)?)%\s*[|/]\s*(\d+)\s*useful",
                chunk,
                re.I,
            )
            if not m:
                m = re.search(
                    rf"policy={pol}\s+overall_hit_rate=(\d+(?:\.\d+)?).*?useful_gets=(\d+)",
                    chunk,
                    re.I | re.S,
                )
            if m:
                h = float(m.group(1))
                if h <= 1.5:  # fraction
                    h *= 100.0
                rows.append({"policy": pol, "hit_pct": h, "useful": int(m.group(2)), "raw": m.group(0)[:120]})
    return rows


def parse_marathon_rich(txt: str) -> Dict[str, Dict[str, Any]]:
    """Parse bench_dynamic_evict REGRET / phase_marathon summary lines."""
    out: Dict[str, Dict[str, Any]] = {}
    #    adaptive   cum_hit=100.0%  useful=1956   regret_hits=0
    for m in re.finditer(
        r"^\s*(?P<p>lru|lfu|adaptive\w*)\s+cum_hit=\s*(?P<h>\d+(?:\.\d+)?)%\s+useful=(?P<u>\d+)\s+regret_hits=(?P<r>-?\d+)",
        txt,
        re.M,
    ):
        out[m.group("p")] = {
            "hit_pct": float(m.group("h")),
            "useful": int(m.group("u")),
            "regret": int(m.group("r")),
        }
    # fallback overall_hit=
    for m in re.finditer(
        r"^\s*(?P<p>lru|lfu|adaptive\w*)\s+overall_hit=\s*(?P<h>\d+(?:\.\d+)?)%\s+useful=(?P<u>\d+)",
        txt,
        re.M,
    ):
        out.setdefault(
            m.group("p"),
            {"hit_pct": float(m.group("h")), "useful": int(m.group("u")), "regret": -1},
        )
    vs = re.search(
        r"adaptive vs fixed:\s*([+-]?\d+(?:\.\d+)?)pp vs LRU,\s*([+-]?\d+(?:\.\d+)?)pp vs LFU",
        txt,
        re.I,
    )
    meta = {"vs_lru_pp": None, "vs_lfu_pp": None}
    if vs:
        meta["vs_lru_pp"] = float(vs.group(1))
        meta["vs_lfu_pp"] = float(vs.group(2))
    return {"policies": out, "meta": meta, "raw_tail": "\n".join(txt.splitlines()[-80:])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="indir", required=True)
    ap.add_argument("--out", dest="outfile", required=True)
    args = ap.parse_args()
    d = Path(args.indir)
    meta = json.loads(read_text(d / "meta.json") or "{}")
    sha = meta.get("sha", "unknown")
    date = meta.get("date_cst", "unknown")

    e1_txt = read_text(d / "e1_phase_marathon.txt")
    e1 = parse_marathon_rich(e1_txt)
    e1_hit = {}
    if (d / "e1_hit_vs_redis.json").exists():
        e1_hit = json.loads(read_text(d / "e1_hit_vs_redis.json"))

    e2 = {}
    if (d / "e2_poison.json").exists():
        e2 = json.loads(read_text(d / "e2_poison.json"))

    a17 = json.loads(read_text(d / "a17_explain.json") or "{}")
    a18 = json.loads(read_text(d / "a18_canary.json") or "{}")
    redis_c = json.loads(read_text(d / "redis_contrast.json") or "{}")

    # memtier ratios from e5
    e5 = read_text(d / "e5_memtier.txt")
    mem_rows = []
    for ln in e5.splitlines():
        if (
            ln.strip().startswith("|")
            and ("redis" in ln.lower() or "aura" in ln.lower() or "pipeline" in ln.lower() or "ops/s" in ln.lower())
        ) or "ratio" in ln.lower() or ln.strip().startswith("Totals"):
            mem_rows.append(ln.strip())

    pols = e1.get("policies") or {}
    lines: List[str] = []
    lines.append("# Aura differentiation vs Redis (A17 / A18 / A19 + adaptive moat)")
    lines.append("")
    lines.append(f"**Date:** {date}  ")
    lines.append(f"**Tip:** `{sha}` (`{meta.get('sha_full', sha)}`)  ")
    lines.append("**Discipline:** `AURA_REDIS_DENY_PLUGIN=1`; dual scoreboard (hit-quality ≠ memtier).  ")
    lines.append("**Redis baseline:** `redis:7-alpine` fixed `allkeys-lru` / `allkeys-lfu` only.  ")
    lines.append("**Aura control plane:** `policy_agent.aura` (Soft sandbox OK for this experiment).")
    lines.append("")
    lines.append("> Adaptive hit-quality SSOT = `python3 scripts/bench_regret.py phase_marathon`.")
    lines.append("> Short `bench_hit_vs_redis.py` adaptive rows remain non-citeable; fixed-kernel vs Redis OK.")
    lines.append("> Never claim memtier adaptive wins — E5 is C dataplane parity only.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## E1 — Multi-phase regret (established adaptive moat)")
    lines.append("")
    lines.append("Authoritative Aura marathon (`bench_regret.py phase_marathon`, `maxmemory=120000`).")
    lines.append("")
    lines.append("| Engine | Policy | Cum useful-GET hit% | Useful | Regret | Notes |")
    lines.append("|--------|--------|---------------------|--------|--------|-------|")
    for name in ("adaptive", "lru", "lfu"):
        r = pols.get(name)
        if not r:
            continue
        eng = "aura"
        note = "policy_agent live EVICT/PIN" if name == "adaptive" else "static kernel"
        reg = r.get("regret", "—")
        lines.append(
            f"| {eng} | **{name}** | **{r['hit_pct']:.1f}%** | {r['useful']} | {reg} | {note} |"
        )
    if not pols:
        lines.append("| aura | *(see raw)* | — | — | — | parse miss — see artifact tail |")
        lines.append("")
        lines.append("<details><summary>E1 raw tail</summary>")
        lines.append("")
        lines.append("```")
        lines.append(e1.get("raw_tail", e1_txt)[-4000:])
        lines.append("```")
        lines.append("</details>")
    meta_e1 = e1.get("meta") or {}
    if meta_e1.get("vs_lru_pp") is not None:
        lines.append("")
        lines.append(
            f"**Cite:** adaptive vs aura LRU **+{meta_e1['vs_lru_pp']:.1f}pp**, "
            f"vs aura LFU **+{meta_e1['vs_lfu_pp']:.1f}pp**."
        )
    lines.append("")
    lines.append("### E1b — Redis fixed-policy side-by-side (static kernels only)")
    lines.append("")
    if e1_hit.get("results"):
        lines.append("| Workload | Engine | Policy | Hit% | Useful hits | Misses | Notes |")
        lines.append("|----------|--------|--------|------|-------------|--------|-------|")
        for r in sorted(
            e1_hit["results"],
            key=lambda x: (x.get("workload", ""), x.get("engine", ""), x.get("policy", "")),
        ):
            lines.append(
                f"| {r.get('workload')} | {r.get('engine')} | {r.get('policy')} | "
                f"{float(r.get('hit_pct', 0)):.1f} | {r.get('hits')} | {r.get('misses')} | "
                f"{r.get('notes') or '—'} |"
            )
        lines.append("")
        lines.append(
            "Redis cannot switch kernels mid-flight; cite §E1 adaptive for the moat, "
            "§E1b for fixed-kernel parity/contrast only."
        )
    else:
        lines.append("_E1b artifact missing or skipped._")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## E2 — Poison-key flood (A19)")
    lines.append("")
    lines.append(
        "Unique-SET poison + light poison GET noise under tight maxmemory; "
        "scoreboard = **keep\\*** hit% (not ops/s)."
    )
    lines.append("")
    if e2.get("results"):
        lines.append("| Engine | Policy | keep* hit% | Hits | Misses | unique_sets | Notes |")
        lines.append("|--------|--------|------------|------|--------|-------------|-------|")
        for r in e2["results"]:
            lines.append(
                f"| {r.get('engine')} | {r.get('policy')} | {float(r.get('hit_pct', 0)):.1f} | "
                f"{r.get('keep_hits')} | {r.get('keep_misses')} | {r.get('unique_sets')} | "
                f"{r.get('notes') or '—'} |"
            )
        # highlight deltas
        by = {(r["engine"], r["policy"]): r for r in e2["results"]}
        def hp(eng, pol):
            r = by.get((eng, pol))
            return float(r["hit_pct"]) if r else None
        a_on = hp("aura", "adaptive_poison_on")
        if a_on is None:
            a_on = hp("aura", "adaptive_poison")
        a_lfu = hp("aura", "lfu")
        r_lru = hp("redis", "lru")
        r_lfu = hp("redis", "lfu")
        lines.append("")
        if a_lfu is not None and r_lru is not None:
            lines.append(
                f"**Hit-quality under poison flood:** aura static LFU **{a_lfu:.1f}%** vs "
                f"redis allkeys-lru **{r_lru:.1f}%** / allkeys-lfu **{r_lfu if r_lfu is not None else float('nan'):.1f}%**."
            )
        if a_on is not None:
            lines.append(
                f"**A19 control-plane:** adaptive_poison_on keep*={a_on:.1f}% with "
                f"`defended=yes` + `INFO explain_*` mid join (Redis cannot mutate choose-fn)."
            )
        lines.append(
            "Caveat: A19 defensive body is `lru|flat|soft` (refuse pin); on this keep* "
            "scoreboard the LFU kernel retains best. Cite A19 for live storm→mutate+explain, "
            "cite aura LFU vs Redis 0% for hit-quality under the same flood."
        )
    else:
        lines.append("_E2 artifact missing._")
    lines.append("")
    lines.append(
        "Redis has no `unique_sets` storm detector and cannot mutate choose-fn to "
        "`lru|flat|soft` (refuse pin) mid-process."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## E3 — Shadow→Canary promote (A18)")
    lines.append("")
    lines.append(
        "**What Redis cannot do:** in-process policy *code* generation / choose-fn canary. "
        "Redis only `CONFIG SET maxmemory-policy` (requires ops change; no shadow dry-run → "
        "trial body → commit/heal)."
    )
    lines.append("")
    lines.append("| Signal | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| AUTOPROMOTE (experiment) | ON (prod default **OFF**) |")
    lines.append(f"| boot saw `shadow-autopromote on` | {a18.get('boot_saw_autopromote')} |")
    lines.append(f"| saw autopromote/canary | {a18.get('saw_promote_or_canary')} |")
    lines.append(f"| EVICT before → after | `{a18.get('evict_before')}` → `{a18.get('evict_after')}` |")
    lines.append(f"| server restart required | **{a18.get('restart_required', False)}** |")
    lines.append("")
    lines.append("Sample agent lines (sanitized):")
    lines.append("")
    lines.append("```")
    for ln in (a18.get("agent_lines") or [])[-20:]:
        lines.append(ln)
    if not a18.get("agent_lines"):
        lines.append("(no matching agent lines — see artifact)")
    lines.append("```")
    lines.append("")
    lines.append("Redis contrast:")
    lines.append("")
    lines.append("```")
    lines.append(json.dumps(redis_c, indent=2)[:800])
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## E4 — Provenance explain (A17)")
    lines.append("")
    lines.append(
        "Operator artifact after mutate/EVICT change: mid → reason → kernel join via "
        "`POLICY EXPLAIN` / `INFO explain_*` / heartbeat. Redis side = `CONFIG GET maxmemory-policy` only."
    )
    lines.append("")
    lines.append("### Aura sample (sanitized)")
    lines.append("")
    lines.append("```")
    lines.append(f"POLICY EXPLAIN => {a17.get('policy_explain')}")
    lines.append(f"INFO explain_* => {json.dumps(a17.get('info_explain') or {}, indent=2)}")
    lines.append(f"EVICT => {a17.get('evict')}")
    hb = (a17.get("heartbeat") or "").strip()
    if hb:
        lines.append("--- heartbeat (tail) ---")
        lines.append(hb[-900:])
    lines.append("```")
    lines.append("")
    lines.append("### Redis sample")
    lines.append("")
    lines.append("```")
    lines.append(f"CONFIG GET maxmemory-policy => {redis_c.get('config_maxmemory_policy')}")
    lines.append("# no POLICY EXPLAIN / explain_mid / audit mid join")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## E5 — Throughput hygiene (dataplane parity only)")
    lines.append("")
    lines.append(
        "Optional short memtier: **aura EVICT=lru** vs **redis:7-alpine**, pipeline 1/16. "
        "**Not** an adaptive win — cite ratios as C RESP parity."
    )
    lines.append("")
    if mem_rows:
        lines.append("```")
        lines.extend(mem_rows[:40])
        lines.append("```")
    else:
        lines.append(
            "_E5 skipped or memtier artifact absent — see `docs/redis-compare.md` for last citeable ratios._"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## What Redis cannot do (A17 / A18 summary)")
    lines.append("")
    lines.append("| Capability | Aura | Redis |")
    lines.append("|------------|------|-------|")
    lines.append("| Live choose-fn mutate + heal | yes (`policy_agent`) | no |")
    lines.append("| Shadow dry-run → canary trial → commit | yes (A18, default OFF) | no |")
    lines.append("| Provenance mid→reason→kernel explain | yes (A17) | `CONFIG GET` policy name only |")
    lines.append("| Unique-SET storm → defensive body | yes (A19) | fixed `maxmemory-policy` |")
    lines.append("| Multi-phase adaptive regret→0 | yes (E1) | pick one static policy |")
    lines.append("")
    lines.append("## Residual caveats")
    lines.append("")
    lines.append("- Soft sandbox used for agent in this experiment; Restricted/prod grant path is separate (`sandbox-policy-profile.sh`).")
    lines.append("- Short hit harness adaptive rows remain non-citeable; marathon is SSOT.")
    lines.append("- Redis maxmemory headroom ≈ idle `used_memory` + data budget; absolute hit% move with headroom — cite deltas / qualitative contrast.")
    lines.append("- A18 AUTOPROMOTE stays **OFF** in production docs; enabled only for this experiment.")
    lines.append("- Memtier absolute ops/s drift with host load; cite ratios.")
    lines.append("")
    lines.append("## How to re-run")
    lines.append("")
    lines.append("```bash")
    lines.append("export AURA_REDIS_DENY_PLUGIN=1")
    lines.append("./scripts/build-native.sh")
    lines.append("./scripts/bench-diff-vs-redis.sh")
    lines.append("# pieces:")
    lines.append("python3 scripts/bench_regret.py phase_marathon")
    lines.append("python3 scripts/bench_poison_vs_redis.py")
    lines.append("python3 scripts/bench_explain_canary_capture.py")
    lines.append("```")
    lines.append("")
    lines.append("CI: document manual / non-blocking — `scripts/ci-bench.sh` runs regret packs; ")
    lines.append("full diff compare is opt-in via `AURA_REDIS_CI_DIFF=1` (see `ci-bench.sh`).")
    lines.append("")
    lines.append(f"_Artifacts dir: `{meta.get('out', str(d))}`_")
    lines.append("")

    Path(args.outfile).write_text("\n".join(lines))
    print(f"Wrote {args.outfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
