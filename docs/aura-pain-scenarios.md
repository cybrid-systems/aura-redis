# Aura ↔ Redis 痛点场景深析

**Status:** authoritative scenario deep-dive（痛点展开 + Aura 解法路径）  
**Date:** 2026-09-23 CST  
**Companions:** [`aura-redis-match.md`](aura-redis-match.md) · [`aura-demand.md`](aura-demand.md) · [`aura-native-control.md`](aura-native-control.md) · [`mutation-gains.md`](mutation-gains.md) · [`workloads.md`](workloads.md) · [`architecture.md`](architecture.md) · [`perf-eval.md`](perf-eval.md)  
**Control plane SSOT:** `src/redis/policy_agent.aura`（DEFAULT；`AURA_REDIS_DENY_PLUGIN=1`）  
**Evidence rule:** 仅引用本仓库已有 bench / demo（`perf-eval.md`、`mutation-gains.md`、`workloads.md`）；**不编造**公司级 % / SLA。

---

## 0. How to read

### 0.1 三类「Aura 解」强度

| 标签 | 含义 | 本文用法 |
|------|------|----------|
| **STRONG（Aura solves）** | VM 强制的 live *code* 变更（sandbox + Mutate effect + Guard-bounded `mutate:rebind` + snapshot/heal + provenance 可引用）**明显优于** `CONFIG SET` / MODULE / Lua·Gears reload / Python sidecar 只拧旋钮 | 大促 regime、eviction storm、ops 对 live experiment 恐惧、poison 恢复、DENY_PLUGIN 对比 |
| **MEDIUM（Aura steers C kernels）** | Aura **选择**命名内核（`EVICT`/`LAYOUT`/`PIN`/`ttl_aware`）；命中质量仍由 **C 数据面内核**主导 | hot-key、TTL pileup、noisy-neighbor 初期、failover 策略代际、soft-goal、controller cost |
| **WEAK / NONE（out of scope）** | 拓扑 / 连接 / 持久化 / ops/s / APM — Aura 不是主因 | § Out of scope |

### 0.2 每节「Aura 如何解」的分层顺序（强制）

每个场景的 **§4 Aura 解法路径** 一律按：

1. **A 原生（Layer A native）** — `sandbox` / `capability` Effect::Mutate·Network / `MutationBoundaryGuard` + `mutate:rebind` / `TypedMutationAudit` / `ast:snapshot|restore` / provenance / workspace / dirty→JIT invalidate / fiber（若相关）。说明**为什么不是「又一种脚本」**。  
2. **B stdlib** — `std/hot-strategy`（register!/swap!/heal!/version）、`std/mutate`（safety-snapshot / boundary-safe?）、手写 fitness / evolve / POLICY（坐在 A 上的库面）。  
3. **C 数据面** — Aura **只选名、不重写**：`lru` / `lfu` / `noop` / `ttl_aware` + `LAYOUT` + `PIN` + samples。

### 0.3 现状标签

相对 `policy_agent.aura` 今日接线：

- **SHIPPED** — demo/bench 绿；可引用  
- **PARTIAL** — 有路径但浅 / 缺原生审计 / 缺隔离  
- **GAP** — 缺或 stretch Δ=0（见 A1–An）

---

## 1. Scenario deep-dives

---

### S1. 大促 / regime shift（zipf ↔ scan / diurnal）

**Fit:** **STRONG** · Cluster **C1** · Backlog **A1**, A13

#### 1. 场景画像

电商 / 内容 feed 平台在大促或改版窗口：白天读多（recency 友好 → LRU 尚可），晚间 flash / 爆款 zipf（频率友好 → LFU+pin），凌晨扫库 / 冷 key 洪水（工作集平移 → 又要 LRU）。流量在 **同一 `maxmemory` 进程寿命内** 换 regime；运维若按「固定一份 runbook」拧 `maxmemory-policy`，总是慢半拍，且下一阶段往往需要**相反**策略。

典型流量：quiet → peak → flash → cool（`diurnal_shift` / `mutation_gain`）；或单寿命 `zipf_hotkey` → `ws_shift` → `hot_protect`（`phase_marathon`）。

#### 2. 症状与信号

- `keyspace_hits` / 自建 hit% 断崖；`evicted_keys` 与 miss 同向上升。  
- On-call：Dashboard 显示「policy=allkeys-lru」但 flash 阶段热 key 已被冲掉；改 CONFIG 后下一阶段又错。  
- Aura-redis INFO 侧：`gets`/`sets`/`hits`/`misses`/`evicted` 窗口比；agent 侧 hit% EWMA、`phase-hint`∈{write_heavy, miss_spike, read_heavy}。

#### 3. 常规解法与失效点

| 常规 | 为何失效 |
|------|----------|
| 手工 `CONFIG SET maxmemory-policy` | 人延迟；下一 phase 需反向；CONFIG REWRITE 恐惧 → 不敢实验 |
| 双实例蓝绿（一 LRU 一 LFU） | 成本 ×2；切流仍是人；策略非 first-class |
| 模块 / sidecar 拧旋钮 | 能切名，但**无 Guard/heal 的策略代码代际**；坏规则粘住 |
| 固定 Lua / Gears 规则 | reload 单元 ≠ denseness；无 TypedMutation / snapshot |

#### 4. Aura 解法路径（分层）

- **A 原生：** `MutationBoundaryGuard` 下 `mutate:rebind` 换 `choose-fn` **函数体**（非 CONFIG 字符串）；`mutate:safety-snapshot` / `boundary-safe?` 先探再改；失败可 `ast:restore`。Dirty propagation → JIT invalidate 证明「这次是代码代换」。Effect 上策略变更属 **Mutate**（非裸 eval）。这不是「脚本里 if」，而是 **工作区 FlatAST 的受护变异**。  
- **B stdlib：** `hot-strategy:swap!` profile（conservative→aggressive）+ M7 threshold body rebuild + fitness EWMA；可选 evolve（M11）。  
- **C 数据面：** choose 输出 `"lfu|flat|pin"` / `"lru|flat"` 等 → RESP `EVICT`/`LAYOUT`/`PIN`；内核仍是 C `ArEvictOps`。

#### 5. 闭环时序

1. Tick：`INFO` → 解析 deltas + TTL 字段 → 更新 hit% EWMA。  
2. `maybe-fitness-mutate!`：miss_spike / EWMA drop → `boundary-safe?` → `safety-snapshot` → `hot-strategy:swap!`（或 threshold-mutate）。  
3. `choose-fn` 求值 → 解析 `|` 字段 → `EVICT` / `LAYOUT` / `PIN` / samples。  
4. 若 post-swap 仍坏 → `hot-strategy:heal!` 回 last-good。  
5. C 侧立即按新内核采样淘汰；agent 死则 **C 保留 last kernel**（fail-safe）。

#### 6. 可验证出口

- **已有：** `phase_marathon` adaptive **100%** cum / regret_hits=**0** vs LRU 81.8% / LFU 42.9%（`perf-eval.md`）。  
- **缺口出口：** `mutation_gain` mutate−frozen ≥ **+8pp** + fitness-swap 日志（**A1**）。历史曾 +14.8pp（`mutation-gains.md`）；近期 eval Δ=0 → 视为 open demand，非「mutate 无用」。

#### 7. 现状与缺口

**SHIPPED** marathon / diurnal harness；**GAP** 近期 `mutation_gain` Δ=0（A1）。TypedMutation trail / provenance 未接线。

#### 8. 不做承诺

不宣称「比 Redis 更快」；不把单 phase 命中或 memtier ops/s 当 headline；不保证任意业务 diurnal 无需调 seed。

---

### S2. Eviction storm / flash churn / 错误策略下的内存 thrash

**Fit:** **STRONG** · C1 · **A1**, optional **A8**

#### 1. 场景画像

Flash sale：`maxmemory` 极紧；写洪水塞满冷 key；若坚持 LFU+pin，采样与 freq 维护吃 CPU，同时 useful `keep*` 仍被冲掉 → **eviction storm**：`evicted_keys` 飙升、hit 崩溃、p99 抖。

#### 2. 症状与信号

- INFO：`evicted` 窗口 / ops → `erate` 高；CPU 在 dict 采样；`hits` 不升反降。  
- On-call：告警「memory pressure + latency」；调大内存是习惯性止血。

#### 3. 常规解法与失效点

加内存 / 改 allkeys-lru / lazyfree — 静态；风暴期内人来不及；LFU 在 flash churn 上可能更贵。

#### 4. Aura 解法路径（分层）

- **A：** 同一 Guard+rebind 路径；策略**代码**可编码「evict-CPU budget」（soft-goal 条件），属 Mutate 边界内的结构编辑。  
- **B：** `choose_*` 内 `erate` 门槛；fitness 在 storm 中 swap 到更保守 body；`heal!` 防毒。  
- **C：** soft 路径拒绝昂贵 `lfu`/+pin → 选 `lru` / `ttl_aware` / `noop`（**选名**）。

#### 5. 闭环时序

观察 `devicted`/`ops` → choose 或 soft 覆盖 → `EVICT lru|…` → 若仍 miss_spike 则 fitness-swap → 坏则 heal。

#### 6. 可验证出口

`flash_churn`：adaptive_soft **~100%** vs LFU **~5–12%**（+87pp 量级，`workloads.md` / `perf-eval.md`）；日志含 `soft-goal refuse expensive (erate=…)`。

#### 7. 现状与缺口

**SHIPPED** M10 soft-goal + `choose_nosoft` A/B。**GAP** 自动 freeze（A8）降低 storm 期 INFO 轮询成本。

#### 8. 不做承诺

不重写 Redis 淘汰算法；不宣称消除所有 eviction CPU（内核仍在 C）。

---

### S3. 无法解释的 hit-rate 下跌（需要 policy explain）

**Fit:** **MEDIUM** · C5 · **A3**, A14

#### 1. 场景画像

凌晨 hit% 掉 15pp；CONFIG 历史空白；多人猜「是不是大 key / 是不是 failover」。缺少 **「哪一代 choose-fn、因何信号、换到何内核」** 的结构化归因。

#### 2. 症状与信号

Dashboard 只有 hits/misses；无 policy reason。Aura 今日：agent stdout 有 `fitness-swap … reason=miss_spike`，但无 durable ring / RESP 查询。

#### 3. 常规解法与失效点

CONFIG 审计、聊天记录、慢查询 — 都不回答「自适应策略为什么翻了」。

#### 4. Aura 解法路径（分层）

- **A：** `mutation_audit_wal` + `TypedMutationAudit` trail + `query:last-mutation-provenance` / `reflect:provenance-blame` + `SecurityEvent` — **原生** forensic，按 mutation_id join。  
- **B：** `mutate:summary`、`hot-strategy:version`、结构化 `policy_explain`（reason enum）。  
- **C：** INFO 已有计数；explain **不**替代内核，只解释「为何选该名」。

#### 5. 闭环时序

每次 swap/heal/apply → 写 audit 记录 {ts, op, from, to, reason, version, signals} → on-call 查 INFO/heartbeat → 对齐 mid。

#### 6. 可验证出口

**A3：** `tests/test_policy_audit.py`；`phase_marathon` 日志可解析 reason ∈ {miss_spike, soft_goal, ttl_pressure, prefix_policy, fitness_swap, evolve_keep, …}。

#### 7. 现状与缺口

**PARTIAL** — 有 inline 日志与 `policy-pin`；**GAP** 无 ring、无原生 trail 导出、无稳定 schema。

#### 8. 不做承诺

不替代 Datadog/慢日志 APM；SecurityEvent ≠ 全站监控。

---

### S4. Live experiment 恐惧 / CONFIG 风险 / 对自修改策略的信任

**Fit:** **STRONG** · C2 · **A4**, **A11**, A3

#### 1. 场景画像

SRE：「生产上改行为 = 发版。」CONFIG 实验被冻结；模块 reload 被视为 crash 面；结果常年跑着次优 policy。需要的是 **可回滚的受护实验**，不是更多权限。

#### 2. 症状与信号

变更日历空白；「不敢动 maxmemory-policy」；事故回顾写「静态策略不适配」。

#### 3. 常规解法与失效点

蓝绿整实例、仅 staging 试 CONFIG、禁用模块 — 实验稀缺 → 策略错误固化。

#### 4. Aura 解法路径（分层）

- **A：** `SandboxMode` Restricted/Strict；`grant-effect!(Mutate)` / Network（**无 Ffi**）；`MutationBoundaryGuard`；`ast:snapshot`/`restore`；TypedMutation 硬闸；可选 FailOnStale provenance。**DENY_PLUGIN** 产品不变量：适应 ≠ dlopen。  
- **B：** `boundary-safe?` → snapshot → `swap!`；失败/`heal!`；未来 canary（试 N tick 再 commit）。  
- **C：** agent 挂了 C **保持 last EVICT**（不回 noop）— 实验失败的底线在数据面。

#### 5. 闭环时序

Restricted + network grant → tick 决策 → snapshot → swap → 观察 fitness → 差则 heal → audit。Canary（A4）：trial body → 窗口差 → 自动 heal。

#### 6. 可验证出口

`poison_heal` Δ=+100pp（已有）；**A4** `test_policy_canary.py`；**A11** 无 `AURA_SANDBOX=off` 的 prod-shaped demo。

#### 7. 现状与缺口

**SHIPPED** heal + DENY_PLUGIN + HA reconnect。**PARTIAL** 多数 demo `AURA_SANDBOX=off`。**GAP** canary 窗口、Restricted 默认、原生 audit。

#### 8. 不做承诺

不声称「零风险自修改」；无 grant/canary 时不把 Soft sandbox 当生产信任故事。

---

### S5. Poison / inverted / broken choose-fn 恢复

**Fit:** **STRONG** · C2 · CI gate

#### 1. 场景画像

误部署 / 演示 invert / 损坏 body：策略系统性选错内核（该 LFU 时选 LRU），hit → 0。CONFIG 坏策略会一直粘到人改；Aura 要 **last-good 自动拉回**。

#### 2. 症状与信号

hit%→0；`fitness-heal!` / `hot-strategy:heal!` 日志；profile=`inverted`|`broken`。

#### 3. 常规解法与失效点

回滚发版、手工 CONFIG — 慢；sidecar「if bad then reset」无 AST snapshot 语义。

#### 4. Aura 解法路径（分层）

- **A：** `ast:snapshot` + Guard rollback；Mutation epoch；非「再 eval 一段修复脚本」。  
- **B：** `hot-strategy:heal!` → last registered good body；fitness 识别 poison_profile。  
- **C：** heal 后重新 `EVICT` 正确名；C 不解释 poison。

#### 5. 闭环时序

Seed inverted → 低 EWMA / poison 检测 → `fitness-heal!` → `heal!` → 恢复 `*last-good-profile*` → apply。

#### 6. 可验证出口

`poison_heal`：mutate **100%** vs frozen **0%**（Δ=+100pp，`mutation-gains.md` / `perf-eval.md`）。保持 CI gate。

#### 7. 现状与缺口

**SHIPPED**。**GAP** 更富 `agent:recover-from-error` / TypedMutation recover（A4 之后可选）。

#### 8. 不做承诺

heal 不是任意语义修复；只回到 **snapshot 的 last-good**，不保证推断「正确业务策略」。

---

### S6. Noisy neighbor / 多租户共享 maxmemory-policy

**Fit:** **MEDIUM** · C4 · **A5**

#### 1. 场景画像

Redis-as-a-Service：租户 A session（要 `ttl_aware`），租户 B zipf 热 key（要 `lfu`+pin），共用一个 `maxmemory-policy`。A 的短 TTL 洪水挤掉 B 的热集（或相反）。

#### 2. 症状与信号

一租户 `evicted` 涨、另一租户 hit 掉；ACL 只能限命令不能限**淘汰策略代码**。

#### 3. 常规解法与失效点

拆实例（$$）；proxy 配额；更多 pod — 成本 ∝ 隔离；仍无 per-prefix *mutable* choose。

#### 4. Aura 解法路径（分层）

- **A：** `workspace_isolation` + `set-tenant-principal!` / `grant-cross-tenant!` deny；未来 per-tenant Mutate grant。  
- **B：** M12 `POLICY` hints → 多 `hot-strategy:register!` 名；**A5** 深化 per-prefix body / param bag。  
- **C：** 仍单一进程内核，但 apply 可带 prefix PIN / 不同 EVICT 名的**时间片选择**（深隔离前是启发式）。

#### 5. 闭环时序

INFO `policy_hints` → `parse-policy-hints!` → 冲突时按 hint 偏向 session/zipf → apply；A5 后各 prefix 独立 swap。

#### 6. 可验证出口

已有 `prefix_mix` adaptive_prefix **100%** vs global **28.6%**（+71.4pp）。**A5：** `prefix_mix_v2` 冲突最优 ≥ +20pp on victim。

#### 7. 现状与缺口

**PARTIAL** hints（非隔离 choose-fn / budget）。**GAP** 原生 MT isolation 接线。

#### 8. 不做承诺

今天的 POLICY **不是** hard multi-tenant sandbox；不宣称 ACL 级隔离已由 Aura 完成。

---

### S7. Eviction 压力下的 hot-key

**Fit:** **MEDIUM** · （内核主导）

#### 1. 场景画像

爆款 SKU / 首页配置：极少数 key 占 GET 绝大部分；冷写入过 `maxmemory` 时 LRU 先干掉热 key（`hot_protect` / `zipf_hotkey`）。

#### 2. 症状与信号

热 key GET miss 飙升；`evicted_keys` 高但 CPU 未必；业务「缓存没了」。

#### 3. 常规解法与失效点

本地缓存、拆 shard、读写分离副本 — 不修复**同实例**内其他有用冷 key 的误杀；CONFIG 改 LFU 又伤 ws_shift。

#### 4. Aura 解法路径（分层）

- **A：** mutate 只改变 **何时选 PIN/LFU** 的代码。  
- **B：** miss 尖峰 → `|pin`；aggressive profile。  
- **C：** **主导** — `PIN` 跳过淘汰；`lfu` 保频；samples→64。

#### 5. 闭环时序

写洪水 + miss → choose `lfu|…|pin` → `PIN` 热前缀 + `EVICT lfu` → 测热 hit。

#### 6. 可验证出口

`hot_protect` / `zipf_hotkey`：adaptive **100%**，LRU **0%**（`perf-eval.md`）。

#### 7. 现状与缺口

**SHIPPED** auto-pin。W-TinyLFU/SLRU 名核 **GAP（A12）** — Aura 仍只 select。

#### 8. 不做承诺

Aura 不实现热 key 发现算法本身；不解决跨分片热 key。

---

### S8. TTL pileup / expire-aware 压力

**Fit:** **MEDIUM**

#### 1. 场景画像

大量短 TTL session + 少量 durable `keep*`；LRU/LFU 死抱即将过期的 session，durable 被逐出（`ttl_wave` / `session_churn`）。

#### 2. 症状与信号

`expired` 与 `evicted` 双高；latency 在过期风暴；hit 在 keep 集上崩。

#### 3. 常规解法与失效点

lazyfree、打散 TTL — 不改「淘汰谁」的语义；allkeys-lru 仍盲。

#### 4. Aura 解法路径（分层）

- **A：** rebind 使 choose 在 TTL 信号下切核。  
- **B：** INFO `keys_with_ttl` / `avg_ttl_ms` / `expired` → body 选 `ttl_aware`。  
- **C：** **`ttl_aware` 内核**（C）按 sooner `expire_at` 优先。

#### 5. 闭环时序

TTL 压力标志 → choose `ttl_aware|flat` → EVICT → keep* 保留。

#### 6. 可验证出口

`ttl_wave`：adaptive/ttl_aware **~100%** vs LRU **0%** / LFU **~31%**。

#### 7. 现状与缺口

**SHIPPED** M9。Typed 压力（HASH/ZSET）**GAP A7**。

#### 8. 不做承诺

不把 expire 扫描改成 Aura；不宣称替代应用层 TTL 治理。

---

### S9. Failover / promote 时 *策略* 变冷（不只是缓存冷）

**Fit:** **MEDIUM** · C6 · **A6**

#### 1. 场景画像

主挂 → 从提升：数据 RDB/复制可跟，但 **mutable policy 代际**（`hot-strategy:version`、profile hash）在 agent 进程内，新主上 agent 冷启动可能从 conservative seed 再摸索一轮，大促中二次受伤。

#### 2. 症状与信号

Promote 后短暂 hit 差；policy-pin 日志 version 重置；C 侧可能仍持 **last EVICT 名**（P1.9 fail-safe）但 choose 代际丢失。

#### 3. 常规解法与失效点

手工再 CONFIG；runbook「promote 后改 policy」— 易漏；副本不复制「策略代码」。

#### 4. Aura 解法路径（分层）

- **A：** 进程内 version/provenance；跨机需 **针**（heartbeat / note）— 原生不自动跨进程复制 workspace。  
- **B：** `hot-strategy:version` + `log-policy-pin!`；reconnect 重申 last EVICT/LAYOUT；**A6** promote 后 reseed 同代。  
- **C：** 保留 last kernel → 策略冷时数据面不立刻 noop。

#### 5. 闭环时序

Agent 挂 → C 保持内核 → 新 agent connect → 读 pin/heartbeat → swap 到 pin 的 profile → 再闭环。

#### 6. 可验证出口

已有 `tests/test_prod_policy_ha.py`（reconnect）。**A6：** promote 后同 version 或文档化 empty→INFO bootstrap。

#### 7. 现状与缺口

**SHIPPED** HA reconnect/reapply。**GAP** 跨 replica 的 version pin。

#### 8. 不做承诺

不声称策略随 Redis 复制协议传播；不把 Cluster failover 纳入 v1。

---

### S10. Module / Lua / Gears 作「自适应」路径 vs DENY_PLUGIN + 纯 Aura rebind

**Fit:** **STRONG**（对比/反例）· C2

#### 1. 场景画像

团队想用 RedisGears 或自研 MODULE 在流量中「热更新淘汰逻辑」。运维拒绝：dlopen / 脚本引擎 = 进程风险；Lua BUSY；Gears 部署重。

#### 2. 症状与信号

模块加载变更评审极严；Lua `BUSY`；Gears 集群部署成本。

#### 3. 常规解法与失效点

| 路径 | 失效 |
|------|------|
| MODULE / `PLUGIN.so` | 进程级风险；无 AST heal |
| Lua | 超时/阻塞；非 Guard typed mutate |
| Gears | 重；reload ≠ denseness |
| `aot:reload` / hot-update | 与 **DENY_PLUGIN** 产品故事冲突（反 moat） |

#### 4. Aura 解法路径（分层）

- **A：** `Effect::Ffi` **不授予** agent；产品 `AURA_REDIS_DENY_PLUGIN=1`；适应面 = workspace **mutate:rebind**，不是 host vtable dlopen。  
- **B：** 纯 `hot-strategy` denseness；明确 **不**把 `std/hot-update` 当 moat。  
- **C：** 命名内核内建；PLUGIN 仅 escape hatch（demo 默认拒绝）。

#### 5. 闭环时序

同 S1；任何「加载 .so」在 DENY 下失败 → 迫使走 Aura swap。

#### 6. 可验证出口

`tests/test_aura_native.py` DENY_PLUGIN；`demo-aura-native.sh`；文档对比表（`aura-native-control.md`）。

#### 7. 现状与缺口

**SHIPPED** DENY + 分进程 agent。永不把 PLUGIN/AOT reload 重新抬成 moat。

#### 8. 不做承诺

不否认 escape hatch 存在；不说 Lua「做不到 if」——说的是 **缺 VM 级 Mutate/heal/provenance**。

---

### S11. Soft-goal：hit 质量 vs evict CPU 预算

**Fit:** **MEDIUM** · C7 相邻 · M10

#### 1. 场景画像

平台成本主：允许略低一点的理论最优 hit，也要卡住 eviction CPU（`erate`）。纯追 LFU 在 flash 上「又贵又烂」。

#### 2. 症状与信号

高 `evicted`/ops、CPU、同时 keep 集 miss；`soft-fires` 计数。

#### 3. 常规解法与失效点

静态调 samples / 换政策 — 无「代码内约束」；人难以实时预算。

#### 4. Aura 解法路径（分层）

- **A：** soft 约束写在 **可 mutate 的 choose 体**里（可演进预算阈值）。  
- **B：** `erate≥20` 拒 lfu/+pin；`≥50`→noop；`choose_nosoft` 对照。  
- **C：** 执行被选的便宜核。

#### 5. 闭环时序

算 erate → soft 覆盖昂贵选择 → 记 `soft-goal refuse` → 仍可 fitness 上层 swap。

#### 6. 可验证出口

`flash_churn` soft vs nosoft / LFU（见 S2）。

#### 7. 现状与缺口

**SHIPPED**。与 **A8** meta-freeze 互补（成本门在 controller，soft 在 choose）。

#### 8. 不做承诺

预算阈值非万能；极端流量仍可能要加内存。

---

### S12. Controller 成本 / 何时冻结 mutation

**Fit:** **MEDIUM** · C7 · **A8**

#### 1. 场景画像

单 regime 稳定长尾：agent 仍每 `AURA_REDIS_POLICY_MS` INFO 轮询 + 偶发 swap，controller 开销 > 边际 hit 收益。需要 **策略把自己 mutate 成冻结态**（仍可 heal）。

#### 2. 症状与信号

稳定 hit；高 applies/sec；INFO 频率无下降。

#### 3. 常规解法与失效点

人工 `AURA_REDIS_FROZEN=1` / `FITNESS_MUTATE=0` — 非闭环；易忘开回。

#### 4. Aura 解法路径（分层）

- **A：** 可选 `resource:quota` / workspace memory-limit 束缚长生 agent；Mutate 换到「不再 swap」的 body 仍是受护编辑。  
- **B：** meta-policy：`swap!` → conservative + fitness-off 或拉长 tick；**A8**。  
- **C：** 冻结后仅执行 last 名核。

#### 5. 闭环时序

估 EWMA gain < 阈值 → mutate 冻结 → INFO 率↓ → 若 regime 变（miss_spike）再解冻/heal。

#### 6. 可验证出口

**A8：** 稳定负载 INFO/apply 率 ↓ ≥5×，hit% 在 always-on 的 2pp 内。

#### 7. 现状与缺口

**GAP**（仅手动 env）。依赖 A1 有可引用 gain 后再做成本门。

#### 8. 不做承诺

不宣称零 controller；分进程 TCP INFO 是架构成本（FFI+closure 惰性约束）。

---

### S13.（扩展）Stuck thresholds / evolve Δ=0 — 搜索式改进

**Fit:** **MEDIUM** · C3 · **A2**, **A15**, A13  
*高价值痛点：即使矩阵标 MEDIUM，产品叙事需要「mutate 可搜索」证据。*

#### 1–3. 画像 / 症状 / 常规

坏 seed（过高 `min-ops`/`miss-pin`）使 choose 长期空串 → 不切核；手调阈值像调参地狱。sidecar 网格搜索无 AST heal/revert。

#### 4. Aura 解法

- **A：** 每代 trial 仍走 Guard+rebind；revert = heal 路径。  
- **B：** M11 窗口 fitness keep/revert；**A15** `std/swarm`/FSS/PSO → `swap!`。  
- **C：** 仅执行搜索结果选中的名。

#### 5–6. 时序 / 出口

baseline 窗口 → propose 更低阈值 → trial → keep 或 revert → **A2** `evolve_gain` ≥ +8pp（近期 Δ=0 FAIL）。

#### 7–8. 现状 / 不做承诺

**SHIPPED** 循环，**GAP** 引用 Δ；不把 `std/evolve` intend-analytics 误标为已接线 Redis fitness。

---

## § Out of scope scenarios

下列痛苦真实，但 **Aura 不是主修复面**（WEAK/NONE）。各用一段说明「为何」与「该谁管」。

### Cluster / slot migration / CROSSSLOT

拓扑与客户端路由问题；v1 **反目标**。Aura mutate 工作在单节点策略空间，不调度 slot。主修复：Cluster 运维手册 / proxy。

### Bigkey 发现与拆分

`MEMORY USAGE`、慢 DEL、大 value 拆 key — 数据模型与 C/ops。Aura 最多在未来吃 type-mix **信号**（A7）选 layout，**不**做发现引擎。

### Connection storms / blocked clients

连接池、proxy、`client-output-buffer` — 网络与会话面。与 choose-fn 无关；NONE。

### RDB fork / COW 抖动

持久化与 fork 毛刺；diskless、rewrite 调优。正交于 policy mutate；NONE。

### Client retry amplification（惊群 miss）

应用锁、early expire、请求合并 — **应用层**。缓存策略静态与否都不替代；WEAK，不营销。

### ops/s 竞速 / memtier 冠军

C 数据面卫生指标（本仓库约 **1.02–1.14×** Redis，`perf-eval.md`）。Lisp RESP 路径故意慢 ~70–110×。**禁止**当 Aura moat。主修复：C 实现质量，而非 mutate。

---

## 2. Cross-scenario pattern

跨 S1–S12 的重复结构（产品一句话）：

> **Guard-bounded mutate of `choose-fn` + C dumb kernels + citeable multi-phase regret.**

展开为固定管道：

```text
  INFO / EWMA / TTL / hints
           │
           ▼
  ┌─ A: Sandbox + Effect(Mutate,Network) + MutationBoundaryGuard ─┐
  │    mutate:rebind / TypedMutation / snapshot·provenance         │
  └──────────────────────┬────────────────────────────────────────┘
                         ▼
  ┌─ B: hot-strategy swap! / heal! / version + fitness|evolve ────┐
  │    （stdlib 坐在 A 上；不是「Lua 式脚本 moat」）                │
  └──────────────────────┬────────────────────────────────────────┘
                         ▼
            choose 字符串 → "lfu|flat|pin" …
                         │
                         ▼
  ┌─ C: EVICT / LAYOUT / PIN / ttl_aware （哑内核，Aura 不重写） ──┐
  └──────────────────────┬────────────────────────────────────────┘
                         ▼
        多阶段 useful-GET hit% + regret vs per-phase oracle
        （headline：phase_marathon；非 memtier ops/s）
```

信任增量（相对 CONFIG/MODULE）：**可回滚的代码代际** + **fail-safe 数据面保留 last kernel** + **DENY_PLUGIN**。可引用性增量：marathon 0 regret、poison +100pp；待补 A1/A2 Δ 与 A3 原生 audit。

---

## 3. Priority map

| Scenario | Fit | Priority | Backlog | Next proof |
|----------|-----|----------|---------|------------|
| S1 大促 / regime shift | STRONG | P0 | **A1**, A13 | `mutation_gain` ≥ +8pp + swap 日志 |
| S2 Eviction storm / flash | STRONG | P0 | A1, **A8** | 保持 `flash_churn`；A8 成本门 |
| S4 Live experiment 信任 | STRONG | P0 | **A4**, **A11**, A3 | canary 测试；Restricted demo |
| S5 Poison / heal | STRONG | P0 | CI | 保持 `poison_heal` gate |
| S10 vs Module/Lua/Gears | STRONG | P0 | — | 永不抬 PLUGIN 为 moat |
| S3 Hit% 为何下降 | MEDIUM | P1 | **A3**, A14 | audit schema + parseable reasons |
| S6 Noisy neighbor | MEDIUM | P1 | **A5** | `prefix_mix_v2` ≥ +20pp victim |
| S9 Failover 策略冷 | MEDIUM | P1 | **A6** | HA promote version continuity |
| S7 Hot-key | MEDIUM | — | A12 optional | 已有 zipf/hot_protect |
| S8 TTL pileup | MEDIUM | — | A7 typed | 已有 `ttl_wave` |
| S11 Soft-goal | MEDIUM | P2 | A8 互补 | 已有 `flash_churn` |
| S12 Controller freeze | MEDIUM | P2 | **A8** | INFO 率 ↓≥5× |
| S13 Evolve stuck | MEDIUM | P0/P1 | **A2**, **A15**, A13 | `evolve_gain` ≥ +8pp |

**Start next（与 demand 一致）：** A1 → A2 → A3。

---

## Changelog

| Date (CST) | Notes |
|------------|-------|
| 2026-09-23 | Initial authoritative scenario deep-dive：S1–S13 + out-of-scope + cross pattern + priority map；链自 match/demand。 |
