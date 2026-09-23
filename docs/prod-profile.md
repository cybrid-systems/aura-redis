# Production profile (Tier 2) — A11 risk acceptance

**Status:** accepted staging/canary profile while Tenant Admin (TA) is unavailable.  
**Date:** 2026-09-23 CST  
**Pointer from:** [`strong-narrative-plan.md`](strong-narrative-plan.md) A11 / SN5

---

## Production profile (current)

| Knob | Value | Notes |
|------|-------|-------|
| Sandbox | **Soft / `AURA_SANDBOX=off`** | Via `scripts/sandbox-policy-profile.sh` default `PROFILE=off` |
| Plugin | **`AURA_REDIS_DENY_PLUGIN=1`** | Adaptation stays Aura choose-fn, not `.so` |
| Audit | **ON** (policy audit ring / heartbeat) | A3 |
| Canary choose-fn | **Available; default-off in agent** until explicitly enabled | A4 |
| Autofreeze | Available per strong-narrative defaults | A8 |
| Data plane | `aura_redis_server` C RESP | maxmemory + named kernels |
| Persistence | Cache-only **or** aura-rdb v2 SAVE | [`persistence.md`](persistence.md) |

```bash
export AURA_REDIS_DENY_PLUGIN=1
source scripts/sandbox-policy-profile.sh   # AURA_SANDBOX=off
# then start aura_redis_server + policy_agent.aura
```

---

## Residual risk (accepted for Tier 2)

1. **Soft sandbox** is weaker isolation than Restricted: mutate/network grants are not TA-gated. Mitigations: DENY_PLUGIN, host OS isolation, no untrusted agent code deploy, audit logs.  
2. **Restricted path remains PARTIAL** — `grant-effect!` returns `#f` without Tenant Admin on the current Aura pin (`AURA_REF`). Live demos/CI keep Soft until TA.  
3. **Single-node** — no Cluster; replica is best-effort async.  
4. **Durability** — SAVE is snapshot, not AOF; typed keys now round-trip in aura-rdb **v2**, but crash between SAVEs loses recent writes.

---

## Restricted sandbox (target — blocked on TA)

| Item | State |
|------|-------|
| `AURA_REDIS_SANDBOX_PROFILE=restricted` | Unsets `AURA_SANDBOX` → Aura Restricted |
| `grant-effect!` network + mutate | Required for agent TCP + hot-strategy |
| Tenant Admin | **Unavailable in this workspace** → grants fail |
| Smoke | `./scripts/smoke-sandbox-profile.sh` documents off=true / restricted grants false |

When TA is available: re-run smoke; demo agent without `AURA_SANDBOX=off`; update this doc + A11 status to DONE.

---

## Explicit non-claims

- Not “Restricted production hardened.”  
- Not multi-tenant isolation (A5 prefix bags ≠ full tenant sandbox).  
- Not Redis security parity (no ACL users, no Redis AUTH variants beyond requirepass).

---

## Commercial / regulated buyers — Soft ≠ Restricted (A11)

**Soft sandbox does NOT unlock Restricted / Tenant Admin isolation.**

| Buyer need | Aura Tier 2 Soft today | Status |
|------------|------------------------|--------|
| Phase-shifting cache + governed autopilot | **Fit** (policy_agent Soft + DENY_PLUGIN) | OK |
| Poison / unique-SET flood defense (A19) | **Fit** | OK |
| Multi-tenant hard isolation (Restricted) | **Not claimed** — TA unavailable; grants fail | early-no → Redis / wait TA |
| Redis ACL users / Cluster / Lua / Streams | **Not claimed** | early-no → see [`intake-reject-checklist.md`](intake-reject-checklist.md) / [`commercial-fit.md`](commercial-fit.md) |

### Explicit non-claims (regulated / hard-isolation RFPs)

- **Not** “Restricted production hardened.” Soft/`AURA_SANDBOX=off` is weaker isolation than Restricted.
- **Not** a substitute for Tenant Admin–gated `grant-effect!` (network/mutate).
- **Not** Redis ACL, Cluster slot redirects, or multi-tenant sandbox parity.
- Customers who **require** Restricted/ACL/Cluster: **intake-reject** — do not sell Soft as Restricted.

When TA lands: re-run `smoke-sandbox-profile.sh`, drop Soft requirement for agent demos, flip A11 → DONE.
