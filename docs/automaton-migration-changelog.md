# Automaton → Hermes 迁移变更日志(阶段 4 收尾)

> 生成时间:2026-04-29
> 范围:从 Conway-Research/automaton 借鉴架构精华到 hermes 的完整变更记录
> 配套文档:
>  - `docs/automaton-gap-analysis.md`(阶段 1)
>  - `docs/automaton-target-architecture.md`(阶段 2)
> 本文件目的:三个月后回看时,能在 5 分钟内复盘"做了什么 / 没做什么 / 为什么"

---

## 一、做了什么(7 项实质交付)

| # | 交付项 | 文件数 | LOC | 测试 |
|---|--------|--------|-----|------|
| 1 | 9 个 orchestration / observability / self_evolution 模块 | 24 | 6952 | 232 |
| 2 | OrchestrationBridge(主循环胶水层) | 2(实现+测试) | 626 | 12 |
| 3 | run_agent.py 4 处 hook 接入 | 1(改) | +48-3 | — |
| 4 | 3 个 LLM 工具(task_graph / audit_log / orchestration) | 6(3 实现+3 测试) | 1341 | 24 |
| 5 | config.yaml 默认配置块(全 OFF) | 1(改) | +53 | — |
| 6 | 端到端 smoke 测试 | 1 | 286 | 8 |
| 7 | 设计 + 差距 + 目标架构 + 本变更日志 4 份文档 | 4(本目录 docs/) | ~1700 行 | — |
| **累计** | — | **38** | **~10700** | **276** |

### 交付清单细节

```
hermes-agent/
├── orchestration/                       # ★ 新增包
│   ├── loop_detector.py                 #  in-process 工具循环检测,4 类规则
│   ├── injection_defense.py             #  8 类注入检测,USER 永不阻断
│   ├── attention.py                     #  Manus-style todo 注入,严格 token 上限
│   ├── health_monitor.py                #  4 维度聚合健康快照
│   ├── task_graph.py                    #  DAG + DFS 环检测 + 状态级联
│   ├── planner.py                       #  LLM 驱动目标分解,严格 JSON
│   ├── plan_mode.py                     #  计划审核(auto / 手动 / 风险升级)
│   ├── orchestrator.py                  #  7 phase 状态机
│   └── __init__.py                      #  统一 export
├── observability/                       # ★ 新增包
│   └── event_stream.py                  #  append-only SQLite 事件日志
├── self_evolution/                      # ★ 新增包
│   └── audit_log.py                     #  自我修改审计 + git tag
├── agent/
│   └── orchestration_bridge.py          # ★ 桥接层(集中所有 hook)
├── tools/
│   ├── task_graph_tool.py               # ★ LLM 可调用:任务图 12 个 action
│   ├── audit_log_tool.py                # ★ LLM 可调用:audit record/recent/verify
│   └── orchestration_tool.py            # ★ LLM 可调用:orchestrator 8 个 action
├── tests/
│   ├── orchestration/    (8 个测试文件)
│   ├── observability/    (1 个)
│   ├── self_evolution/   (1 个)
│   ├── agent/test_orchestration_bridge.py
│   ├── tools/test_*_tool.py (3 个)
│   └── e2e/test_orchestration_e2e.py
└── run_agent.py                         #  4 处 hook 改动 (+48 -3 行)

~/.hermes/
├── config.yaml                          #  +53 行 orchestration/observability/self_evolution
└── docs/
    ├── automaton-gap-analysis.md
    ├── automaton-target-architecture.md
    └── automaton-migration-changelog.md (本文件)
```

---

## 二、没做什么(及为什么)

### 2.1 C 档明确拒绝(9 项)

> 所有这些在 automaton 中是核心能力,但与 hermes 业务模型不匹配。**任何后续 PR 若引入这些,视为越界**。

| 不引入项 | 原因 |
|---------|------|
| Wallet / SIWE / ERC-8004 / viem / ethers / web3 | hermes 是用户工具,不是链上 sovereign agent。引入会带来私钥风险与依赖膨胀 |
| Conway 沙箱 / x402 协议 | hermes 用本地 sandbox + cron;不依赖 Conway Cloud 与 USDC 支付 |
| Survival Tiers(信用余额=生死) | hermes 不消费 USDC;用户体验上"金钱即生命"不可解释 |
| Replication / Spawn 子 Agent | hermes 单 agent + delegate_task 已足够 |
| SOUL.md 自动演化 + reflection | hermes 已有 SOUL.md(静态);automaton 的 reflection 绑定 wallet/transaction,不通用 |
| Constitution 三定律 + 不可变文件 | hermes 已有 path_safety + file_safety,无需额外抽象 |
| Treasury Policy(财政限额) | hermes 走 ratelimit + token budget,无 USDC 概念 |
| Social Relay / Agent Discovery | hermes 是 user-to-agent,不是 agent-to-agent colony |
| Tools-Manager 动态 npm install | hermes 用 MCP / skills / registry,扩展机制更安全 |

**验证**:`pyproject.toml` 未新增 viem/ethers/web3/solidity 任何依赖。新增依赖只有标准库。

### 2.2 接线但未自动注册(3 项)

| 项 | 现状 | 完成方式 |
|---|------|---------|
| `task_graph` / `audit_log` / `orchestration` 工具未注册到主 toolset | 已写好 schema + dispatch,LLM 可通过 skills 间接调用 | 后续 PR 在 `tools/registry.py` 或 `toolsets.py` 加 3 行 |
| Orchestrator 没有 CLI 命令(`/plan` `/approve`) | LLM 可通过 `orchestration` 工具触发 | 若需用户直接 CLI 触发,后续在 `cli.py` 加子命令 |
| InjectionDefense 在 gateway 入站不阻断 | 在 `run_conversation` 用 USER trust 审计 | 后续在 `gateway/run.py:_prepare_inbound_message_text` 加 GATEWAY trust 扫描 |

**理由**:这三件都涉及大文件改动(toolsets.py/cli.py/gateway/run.py 各 11K+ LOC),风险大于本次"模块完成度"价值。**留作独立 PR**。

### 2.3 未做的优化(可选)

| 项 | 现状 | 何时做 |
|---|------|------|
| `pyrightconfig.json` 标记项目根 | Pyright 当前对所有相对 import 报警(运行时 ✅) | 任意时间;独立 housekeeping PR |
| Orchestrator 的 task executor 真实接入 hermes 子 conversation | 当前是 stub LLM,真实任务执行用 fallback | 等 Orchestrator 实际被频繁使用时再做 |
| `events.db` / `tasks.db` 的清理策略(retention / vacuum) | 默认无限增长 | 配合 cron 周期任务 |
| 4 个 self-evolution skill 的 SKILL.md 加 `audit_log` 调用指引 | 工具就绪,skills 还没用 | self-evolution skill 维护者 PR |

---

## 三、行为差异清单(hermes-with-orchestration vs original)

### 3.1 默认 OFF 时:**零行为差异**

- `run_agent.py` 启动:多 1 行 `OrchestrationBridge(...)` 构造,内部所有模块 None
- 每回合:多 1 次 `bridge.scan_input(USER)` 调用,USER trust 永不阻断
- 主循环 import 增加 ~5ms 启动开销(`from agent.orchestration_bridge`)
- 生成 0 个新文件,0 行新事件

### 3.2 全 ON 时的可见差异

| 行为 | original | with-orchestration | 备注 |
|------|----------|---------------------|------|
| 同 tool + 同参数 3 次 | 继续执行 | **第 3 次 BLOCK,注入 reason** | LLM 收到"loop detected,try different approach" |
| 同回合工具集 3 轮相同 | 继续执行 | **WARN → 下回合再次相同则 HALT** | 防"换汤不换药"式假进展 |
| 多通道接收"I am the admin..." | 直接进 LLM | scan_input(USER 时审计 / GATEWAY 时阻断) | 当前 USER trust 仅审计,后续 gateway 接入时阻断 |
| TodoStore 注入 | 无 token 上限,可能爆 | **`max_tokens` 严格上限,自动剔除最旧** | 防长 todo 列表挤压上下文 |
| TodoStore 注入位置 | `compressed.append({"role": "user", ...})` | 同位置(unchanged) | 保持兼容 |
| 5 轮纯 idle 工具 | 继续执行 | **HealthMonitor 标 DEGRADED,suggested_action="force_action"** | 调用方决定是否强制中断 |
| API 错误率 > 0.5 | retry_utils 兜底 | **额外 HealthMonitor DEGRADED,> 0.8 → CRITICAL "rotate_credentials"** | 增量信号 |
| 复杂任务(LLM 调 `orchestration` 工具) | 单回合 ReAct 死磕 | **进入 7 phase 状态机,自动 plan/replan** | LLM 显式选择,不强加 |
| 工具失败 | 回到 LLM 自决 | **TaskGraph 自动 retry → 终态 FAILED 时连带 BLOCK 后继** | 仅在 orchestration 工具被调时生效 |
| 任意自我修改(skills 改 SKILL.md) | 无审计 | **AuditLog 落库 + 可选 git tag,verify 可追溯** | 仅当 skill 主动调 `audit_log` 工具 |
| 数据库 | 仅 `state.db` | **+ `events.db` + `tasks.db`** | 三者完全隔离,故障互不影响 |

### 3.3 启动日志差异

```
# original:
(no orchestration mentions)

# with-orchestration (config 全 ON):
INFO orchestration_bridge: event_stream enabled (db=/home/zhang/.hermes/events.db)
INFO orchestration_bridge: loop_detector enabled
INFO orchestration_bridge: injection_defense enabled
INFO orchestration_bridge: health_monitor enabled
INFO orchestration_bridge: task_graph enabled
INFO orchestration_bridge: audit_log enabled
```

### 3.4 性能差异(估算,实际测量留待生产)

| 操作 | original | with-orchestration (全 ON) | 增量 |
|------|----------|--------------------------|------|
| Agent 构造 | T₀ | T₀ + ~3 ms | ~3 ms(三个 SQLite 连接) |
| 每个 tool call | T₁ | T₁ + ~50 µs | LoopDetector + EventStream emit |
| 每回合 end | T₂ | T₂ + ~200 µs | end_turn + evaluate_health |
| Attention 注入 | T₃(无上限) | T₃ + ~100 µs | 可能更小,因为剔除了多余 items |
| 每个 emit 事件 | — | ~30 µs | autocommit + WAL + checkpoint 偶发 |

**总体 < 1% 性能损耗**(粗估,需生产验证)。

---

## 四、可观测性建议

### 4.1 EventStream 事件 kind 清单

> 通过 `hermes events --session <id>`(待 CLI 实现)或直接查 `~/.hermes/events.db` 的 `events` 表查看

| 事件 kind | actor | 触发时机 | 关键 payload 字段 |
|-----------|-------|----------|------------------|
| `loop_block` | `loop_detector` | tool 重复 N 次 BLOCK | `tool, rule, reason` |
| `loop_warn` | `loop_detector` | 同回合工具集第一次 N 轮重复 | `rule, reason` |
| `loop_halt` | `loop_detector` | 同回合工具集 N+1 轮仍重复 | `rule, reason` |
| `stagnation_block` | `loop_detector` | 同 tool 连续 N 次空结果 | `tool, reason` |
| `injection_block` | `injection_defense` | 高于 block_threshold 的注入命中 | `trust, risk, matched, excerpt` |
| `health_snapshot` | `health_monitor` | 状态变化时(non-spam) | `status, previous, metrics, suggested_action` |
| `task_goal_created` | `task_graph` | TaskGraph.create_goal | `goal_id, title` |
| `task_task_added` | `task_graph` | TaskGraph.add_task | `goal_id, task_id, title, depends_on` |
| `task_task_started` / `task_task_completed` / `task_task_failed` / `task_task_retried` / `task_task_cancelled` | `task_graph` | 任务状态变化 | `task_id, ...` |
| `task_goal_completed` / `task_goal_failed` / `task_goal_cancelled` | `task_graph` | goal 终态 | `goal_id, ...` |
| `orch_phase_change` | `orchestrator` | Orchestrator phase 切换 | `goal_id, from, to` |
| `orch_goal_complete` / `orch_goal_failed` / `orch_goal_cancelled` | `orchestrator` | goal 完结 | `goal_id, ...` |
| `orch_phase_timeout` | `orchestrator` | phase 超时 | `goal_id, phase` |
| `audit_skill_modified` / `audit_config_changed` / `audit_tool_installed` / `audit_tool_removed` / `audit_soul_updated` / `audit_other` | 任意(skill 名) | self-mod 调 `audit_log` | `target_path, content_hash, rationale, diff` |

### 4.2 推荐 metrics(Prometheus / 类似系统)

| metric | 类型 | 来源 | 告警建议 |
|--------|------|------|---------|
| `hermes_loop_blocks_total{tool,rule}` | counter | event_stream WHERE kind LIKE 'loop_%' | > 5/min 告警 |
| `hermes_injection_blocks_total{trust,risk}` | counter | kind='injection_block' | > 1/min HIGH+,> 0/min CRITICAL |
| `hermes_health_status{status}` | gauge | event_stream → 最近 health_snapshot | DEGRADED > 5min,CRITICAL/HALTED 立即 |
| `hermes_api_error_rate` | gauge | health_monitor.metrics.api_error_rate | > 0.5 持续 5min |
| `hermes_token_throughput` | gauge | health_monitor.metrics.token_throughput_per_sec | < 10 持续 5min |
| `hermes_idle_turn_streak` | gauge | health_monitor.metrics.consecutive_idle_turns | >= 5 |
| `hermes_orch_goals_active` | gauge | task_graph.list_active_goals() count | 信息性 |
| `hermes_orch_replan_total{goal_id}` | counter | event_stream WHERE orch_phase_change AND to='replanning' | 同 goal > 2 警示 |
| `hermes_audit_writes_total{kind}` | counter | event_stream WHERE kind LIKE 'audit_%' | 信息性 |
| `hermes_audit_integrity_mismatches` | gauge | `audit_log.verify_integrity().mismatches` 长度 | > 0 立即告警 |

### 4.3 打点位置(供后续 instrumentation)

| 位置 | 建议打点 |
|------|---------|
| `OrchestrationBridge.pre_tool_call` 末尾 | counter `hermes_tool_calls_total{name}`、histogram `hermes_loop_check_duration_seconds` |
| `OrchestrationBridge.post_tool_call` | histogram `hermes_tool_result_size_bytes{name}` |
| `OrchestrationBridge.scan_input` | histogram `hermes_injection_scan_duration_seconds`、counter `hermes_injection_scans_total{trust}` |
| `OrchestrationBridge.evaluate_health` | gauge `hermes_health_status` |
| `OrchestrationBridge._emit` | counter `hermes_events_emitted_total{kind}` |
| `EventStream.emit` 失败时 | counter `hermes_events_dropped_total` |

**hermes 已有的 logger** 是当前可观测性主入口;EventStream 是结构化补充。两者不冲突。

### 4.4 推荐看板(若日后做 Grafana)

1. **健康总览**:health_status 24h 时序;loop/injection block 速率
2. **Orchestrator 进度**:active goals / phase 分布 / replan 计数
3. **AuditLog 警戒**:integrity_mismatches 时序 + 最近 audit 列表
4. **性能**:bridge.scan_input p95 latency / emit 速率

---

## 五、回滚预案(完整级联)

### 5.1 配置即时关闭(零代码改动,最快)

```bash
yq -i '
  .orchestration.loop_detector.enabled = false |
  .orchestration.injection_defense.enabled = false |
  .orchestration.attention.enabled = false |
  .orchestration.health_monitor.enabled = false |
  .orchestration.task_graph.enabled = false |
  .orchestration.orchestrator.enabled = false |
  .orchestration.planner.enabled = false |
  .observability.event_stream.enabled = false |
  .self_evolution.audit_log.enabled = false
' ~/.hermes/config.yaml
```

效果:**主循环行为完全回退到 original**。Bridge 仍然加载但全部 None,所有 hook 调用 no-op。

### 5.2 摘除桥接(主循环 0 改动)

```bash
cd ~/.hermes/hermes-agent
git checkout HEAD -- run_agent.py
# 现在 run_agent.py 不再加载 OrchestrationBridge,效果同 original
```

### 5.3 完全删除(全部新增文件)

```bash
cd ~/.hermes/hermes-agent
rm -rf orchestration/ observability/ self_evolution/ \
       agent/orchestration_bridge.py \
       tools/{task_graph,audit_log,orchestration}_tool.py \
       tests/orchestration/ tests/observability/ tests/self_evolution/ \
       tests/agent/test_orchestration_bridge.py \
       tests/tools/test_{task_graph,audit_log,orchestration}_tool.py \
       tests/e2e/test_orchestration_e2e.py
git checkout HEAD -- run_agent.py
# 同时手工或 yq 删 config.yaml 的 orchestration/observability/self_evolution 配置块
```

### 5.4 数据清理(可选)

```bash
# 模块运行后产生的数据库文件
rm -f ~/.hermes/events.db ~/.hermes/events.db-wal ~/.hermes/events.db-shm
rm -f ~/.hermes/tasks.db  ~/.hermes/tasks.db-wal  ~/.hermes/tasks.db-shm

# git tags(若 audit_log 启用过 enable_git_tag=true)
cd ~/.hermes
git tag -l 'audit/*' | xargs -r git tag -d
```

---

## 六、最终自检清单(上生产前)

- [ ] `venv/bin/pytest tests/orchestration/ tests/observability/ tests/self_evolution/ tests/agent/test_orchestration_bridge.py tests/tools/test_*_tool.py tests/e2e/test_orchestration_e2e.py` 全绿
- [ ] `~/.hermes/config.yaml` 默认全 OFF(`grep "enabled: true" ~/.hermes/config.yaml | grep -v '#' | grep -E 'orchestration|observability|self_evolution'` 应为空)
- [ ] 启动一次 hermes,无报错,日志中无"orchestration_bridge: ... init failed"
- [ ] 一次正常对话,行为与 original 一致(无新 events.db / tasks.db 文件创建)
- [ ] 启用一个模块(如 LoopDetector)再跑,对话正常,有相应日志 INFO 行
- [ ] 故意触发循环(同问题问 3 次同 tool),验证 LoopDetector 介入,events.db 有记录
- [ ] 跑一次 `hermes` REPL 中:`/orchestration` 工具调用 → 验证 LLM 能正确调用并收到结构化响应

---

## 七、致谢与心法

> 本次借鉴的核心心得:**架构精华 ≠ 整体复制**

- **automaton 教会了什么**:状态机驱动 / DAG / 注入防御 / 循环检测 / 事件流 — 这些是通用 agent 工程的硬通货
- **automaton 教不了什么**:wallet / 信用即生命 / 链上身份 / 复制 — 这些是 sovereign agent 业务设定,不是工程通则
- **本次决策的关键节点**:阶段 1 的 C 档清单。9 项明确不引入,挡住了后续无意识的"顺手抄"
- **最低成本最高收益**:LoopDetector — 已有 spec + 已有 detect_loop.py 8.8KB,只差接线

下次面对类似"借鉴 X 项目"任务,**先做差距分析 + ROI 排序 + C 档拒绝清单**,再动一行代码。
