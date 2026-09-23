# Commercial fit (who to sell / who to reject early)

**Companion:** [`intake-reject-checklist.md`](intake-reject-checklist.md) · [`prod-profile.md`](prod-profile.md) · cite data [`diff-vs-redis.md`](diff-vs-redis.md)

## Fit (pursue)

| Signal | Why Aura | Cite |
|--------|----------|------|
| Phase-shifting cache (zipf ↔ workspace shift ↔ hot protect) | Live EVICT/PIN via `policy_agent`; Redis is fixed `maxmemory-policy` | E1 marathon adaptive **100%** vs LRU/LFU |
| Poison / unique-SET flood risk | A19 storm → `lfu\|flat\|soft` defensive mutate + explain mid | E2 keep* adaptive_poison_on ≫ Redis **0%**; ≈ aura LFU |
| Wants governed autopilot (shadow→canary, audit, autofreeze) | A17 explain / A18 score-gated promote (default OFF) / A3 audit | E3/E4 in diff-vs-redis |
| Soft sandbox OK + host isolation | Tier 2 accepted profile Soft + `DENY_PLUGIN=1` | prod-profile |

## Early-no (do not stretch Soft into Restricted)

| Need | Why reject | Pointer |
|------|------------|---------|
| Cluster / slot redirects | Not in surface | intake #4 |
| Lua / modules / PLUGIN `.so` | DENY_PLUGIN; no MODULE | intake #8 |
| Streams / bitmaps / geo / HLL / Sets | Not allowlisted | intake #1 |
| Redis ACL users/rules | requirepass only | intake #7 |
| Hard multi-tenant isolation / Restricted sandbox | A11 Soft ≠ Restricted (no TA) | prod-profile non-claims |
| AOF / PSYNC / sentinel auto-failover | Snapshot SAVE / best-effort replica only | intake #5–6 |
| Large unbounded ZSET / SSCAN/ZSCAN | Caps + missing commands | intake #2–3 |

## Honesty rules for sales eng

1. Adaptive hit-quality SSOT = `bench_regret.py phase_marathon` — **never** short `bench_hit_vs_redis` adaptive rows.
2. Memtier = C dataplane parity only — **never** adaptive throughput wins.
3. Soft sandbox ≠ regulated hard isolation — point to intake-reject.
4. Dual scoreboard: keep*/regret/explain ≠ ops/s.

**Quick reject phrases:** Streams · Cluster · Lua · ACL · Restricted-required · AOF · PSYNC · PLUGIN.
