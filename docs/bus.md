# Cross-process bus host contract (issue #14)

The policy agent can host the aura v0.3.1 tool bus on its choose-fn
workspace so external tools (perf, harness/judge, maintainer) observe
policy metrics and propose candidate `choose-fn` bodies through the
closed loop: trial → source-carrier patch → evidence → scoped KEEP →
canary observation → seal/auto-revert.

Bus mode is **opt-in**: `AURA_REDIS_BUS_WORKSPACE=N`. When unset, no bus
code runs and behavior is byte-identical to the bus-off agent.

## Enablement

| Env | Meaning |
|-----|---------|
| `AURA_REDIS_BUS_WORKSPACE=N` | host workspace id (integer > 0); unset/empty = bus off |
| `AURA_BUS_EVIDENCE=1` | KEEP requires the bound five-tuple evidence |
| `AURA_BUS_CANARY_WINDOW=N` | metric ticks a KEEP'd candidate stays under observation |
| `AURA_BUS_CANARY_FLOOR=F` | metric floor; 2 consecutive samples below → auto-revert |
| `AURA_BUS_CANARY_KEY=K` | metric key that drives the window (use `7` = hit-ewma) |

The canary/evidence params are read by the aura host **at `bus:host`
time from the process environment** — Aura env is read-only (`getenv`),
so the launcher/test runner must set them before spawning the agent.
Defaults (all unset) keep v0.3.0 behavior: no evidence gate, no canary.

The existing `AURA_REDIS_CANARY*` env-driven trial logic remains the
fallback when the bus is off.

## Host startup (integration point)

`src/redis/policy_agent.aura`, right after the choose-fn seed
(`set-code` + `eval-current`). `std/bus` / `std/contract` are required
**lazily inside the gated block** (A15 pattern): loading them at top
level shifts bus-off boot timing and exposes the audit
read+truncate+write race, so bus-off boot stays byte-identical.

1. `(bus:host N)` — the agent process owns the coordinator.
2. Self-attach as **maintainer**: `(bus:attach "" N 3 127 255)`
   (role 3, all caps, all channels); session stored in `*bus-session*`.
3. `(bus:install-constraint sid "pure-define-only" "(lambda (ctx) #t)")`
   — candidate bodies must be pure computation (no I/O, net, FFI,
   mutation, eval, bus calls).
4. `std/contract` protection mirror: `contract:protect` +
   `contract:activate!` over the control-plane defines (below). The
   engine mirrors the activated set, so the bus KEEP apply path refuses
   source-carrier patches targeting any protected name.
5. `(bus:project-nodes sid)` — project the live FlatAST into Nodes.
   `(bus:defines sid)` answers the same names with their node ids, so a
   peer addresses a function by name (aura#5397).

The bus-mutable target set is a **family of pure policy functions**
(aura#5397): `choose-fn` picks the eviction kernel, `admit-fn` decides
whether a write is admitted at all, `ttl-fn` picks the TTL policy. A
candidate may rewrite several of them at once — one source-carrier patch
per function, all in one worldline — and the bus applies the set
**atomically**: one broken body anywhere in the set leaves every
function byte-identical. None of the three is in the protection set.

A peer asks the host for the target names and their node ids with
`(bus:defines session)`. The node ids live in the host's own workspace
AST, so a peer must not guess them: the host is the only one that can
answer.

## Protected set (12 defines)

| Area | Defines |
|------|---------|
| RESP / C-plane | `redis-call!`, `resp-encode-cmd`, `resp-encode-bulk`, `resp-parse-one` |
| Reconnect | `ensure-connected!`, `close-sock!`, `reapply-last-policy!` |
| Persistence | `write-policy-pin!`, `resume-from-pin!`, `log-policy-pin!` |
| Audit / heartbeat | `audit-append!`, `write-heartbeat!` |

A KEEP whose patch targets a protected define fails with
`validation-failed` (the refusal names the protection set) and the live
workspace is unchanged.

## Metric keys (Metrics channel = 1, published every policy tick)

| Key | Metric |
|-----|--------|
| 1 | `dgets` — GET delta this tick |
| 2 | `dsets` — SET delta |
| 3 | `dhits` — hit delta |
| 4 | `dmisses` — miss delta |
| 5 | `devicted` — eviction delta |
| 6 | `erate` — eviction rate % (`devicted*100/ops`) |
| 7 | `hit-ewma` — hit% EWMA 0..100 (canary observation metric) |

Each tick also re-projects Nodes (`bus:project-nodes`). Rows read via
`(bus:query sid 1 0 0)` are `(key value)` pairs.

## Grant policy

- **Judge (harness, role 2)** requesting delegation gets
  `(bus:grant sid 24 255 0 0 worldline)` — bits 24 = keep|rollback,
  scoped to **one worldline**. Helper: `(bus-grant-keep! worldline)`
  returns the token (or `#f`). The judge presents it with
  `bus:grant-activate` (session keeps its read/publish bits, gains the
  scoped keep+rollback) before `bus:keep` / `bus:rollback`.
- **Perf (role 1)** may attach read/publish/trial (caps 7) and propose
  trials, but **keep is never granted to perf**.
- Grants are derived from the maintainer session only; the host never
  hands out constraint-write or grant bits.

## Canary observation

With `AURA_BUS_CANARY_WINDOW>0`, a KEEP'd `choose-fn` enters
`WorldlineState::Canary` (4) instead of sealing immediately. Each
published sample of `AURA_BUS_CANARY_KEY` ticks the window:

- window expires at/above floor → seal (Kept), generation advances;
- 2 consecutive samples below `AURA_BUS_CANARY_FLOOR` → auto-revert
  (pre-observation snapshot restored);
- judge may seal early with `bus:canary-confirm` or revert with
  `bus:rollback`.

Audit kinds: CanaryStart=14, CanarySeal=15, CanaryRevert=16.

## Shutdown

On agent exit `bus-shutdown!` detaches the maintainer session (the
Disconnect frame makes the coordinator roll back pending peer trials)
and unhosts the workspace.

## Test

`tests/bus_host_test.py` runs a minimal host harness (same contract:
seed choose-fn + protected define → `bus:host` → self-attach →
pure-define-only → contract protection → project-nodes → metric
publish loop) against `~/code/grok-dev/aura-grok/build/aura`
(`AURA_BIN` overrides). The full agent needs the C data plane, so the
harness stands in for it; the integration point above is the real
entry. Verifies: host active, read-only peer attach, metrics published,
protected define patch refused, impure body refused, pure choose-fn
KEEP applied live.