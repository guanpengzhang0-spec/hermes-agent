# Automaton → Hermes 差距分析报告

> 生成时间:2026-04-29
> 范围:对比 Conway-Research/automaton(TypeScript,~14K LOC,主干 src/)与本地 hermes(Python,~55K LOC,主干 hermes-agent/)
> 输出:可借鉴清单(按 ROI 排序)+ 不建议引入清单
> 铁律:本文档**纯设计**,不引入任何 automaton 业务专属机制(钱包 / 链上身份 / 信用即生命 / 复制)

---

## 一、Hermes 现状架构(精简鸟瞰)

```
                          ┌──────────────────────────────────┐
                          │  USER  (CLI / Gateway 多通道)     │
                          │  Telegram · Discord · Slack ·     │
                          │  WhatsApp · Signal · Matrix       │
                          └─────────────┬────────────────────┘
                                        │ user_message
                                        ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │                AIAgent.run_conversation (单回合主循环)           │
   │   run_agent.py:9923  —— 13.6K LOC 巨型类,55+ 内部方法            │
   │                                                                 │
   │   ┌──────────────┐   ┌──────────────┐   ┌─────────────────┐    │
   │   │ TodoStore    │   │ ContextEngine│   │ CredentialPool  │    │
   │   │ (线性列表)   │   │ Compressor   │   │ (多 provider    │    │
   │   │ 自动注入     │   │ (1.4K LOC)   │   │  failover)      │    │
   │   └──────────────┘   └──────────────┘   └─────────────────┘    │
   │   ┌──────────────┐   ┌──────────────┐   ┌─────────────────┐    │
   │   │ MemoryMgr +  │   │ ErrorClassif │   │ IterationBudget │    │
   │   │ Hindsight    │   │ (980 LOC)    │   │ (token 预算)    │    │
   │   └──────────────┘   └──────────────┘   └─────────────────┘    │
   │                                                                 │
   │   工具调用:_execute_tool_calls / _invoke_tool                   │
   │   并发批:_should_parallelize_tool_batch                          │
   │   去重防护:_deduplicate_tool_calls / _cap_delegate_task_calls    │
   │   流式:_interruptible_streaming_api_call                        │
   └─────────────────────────────────┬───────────────────────────────┘
                                     │ persist
                                     ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  ~/.hermes/state.db (SQLite WAL, 90MB) + sessions/ + checkpoints│
   │  hermes-agent/cron/scheduler.py (2276 LOC, 自有 cron 调度器)     │
   │  ~/.hermes/skills/ (26 域) + hermes-agent/skills/ (27 域)        │
   │  self-evolution/{loop-guard, constitution-audit, ...}            │
   └─────────────────────────────────────────────────────────────────┘
```

**hermes 已具备的工业能力**(对比 automaton 已经领先的部分):
- 多 Provider + 凭证池 + 故障转移
- Streaming + Vision + TTS + 中断/复盘
- Skills 体系(53 域,远超 automaton 的目录式 skills)
- 多通道网关(6 平台)
- ContextEngine 抽象 + 5 级压缩
- 错误分类器(980 LOC,按状态码 / 错误码 / 消息三维)
- 凭证 Token 缓存
- Hindsight 长期记忆 MCP

**hermes 显著缺失的能力**(本次借鉴目标):
- 主循环没有 LoopDetector 集成
- 完全没有注入防御(多通道入口零净化)
- 没有 Orchestrator 状态机 / 显式 Plan-Mode
- 没有 Planner + DAG 任务图
- 没有 HealthMonitor 综合健康
- 没有统一的 self-mod audit log

---

## 二、A 档:几乎一定值得借鉴的通用模式

> 评分维度:`hermes 现状` × `automaton 差异` × `本地化复杂度`

### A1. ReAct 循环 + 注入防御

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/agent/loop.ts` 1026 LOC + `src/agent/injection-defense.ts` 520 LOC,8 类检测:指令模式 / 权限主张 / 边界操纵 / ChatML 标记 / 编码逃逸 / 多语注入 / 财务操纵 / 自残指令 |
| hermes 现状 | ReAct 循环已有(`run_conversation`),但**注入防御完全缺失**。多通道(Telegram/Discord/Slack/WA/Signal/Matrix)外部消息**直接进 LLM**,零净化。 |
| 差距 | 严重。多通道是 hermes 最大的攻击面 |
| 借/不借 | **借**(只借 injection-defense 部分,ReAct 不动) |
| 改造点 | 在 `gateway/delivery.py` 入口 + `run_conversation()` 入口加一层 `InjectionDefense` 净化器,标记不信任内容 + 拒绝高风险模式 |

### A2. Orchestrator 状态机

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/orchestration/orchestrator.ts` 1232 LOC,显式 7 阶段:`idle → classifying → planning → plan_review → executing → replanning → complete/failed`,带超时、replan 计数(默认 3 次)、断路器、状态持久化 KV |
| hermes 现状 | `run_conversation` 是单回合 ReAct 循环,**完全无显式状态机**。复杂任务 = LLM 自主拼凑,没有 plan/replan 决策节点,失败靠 retry_utils 兜底但无重新规划 |
| 差距 | 严重。超过 5 步的任务很容易跑飞或重复劳动 |
| 借/不借 | **借**(必借,但简化 — 不引入 funding/agent assignment 部分) |
| 改造点 | 新增 `hermes-agent/orchestration/orchestrator.py`,只在用户标记复杂任务(如 `/plan` 命令、或 LLM 自动判断)时进入状态机;简单任务仍走原 `run_conversation`。状态存到 state.db。 |

### A3. Planner + DAG 任务图

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/orchestration/planner.ts` 767 LOC + `src/orchestration/task-graph.ts` 700 LOC,LLM 驱动目标分解,产出 DAG,带依赖解析、环检测、重试、成本估算 |
| hermes 现状 | TodoStore 是**纯线性列表**(`_items: List[Dict]`,无 deps / cost / retry),长任务无法并行/分解 |
| 差距 | 显著。todo_tool 仅适合简单待办,真正需要分解的任务能力为零 |
| 借/不借 | **借**(必借) |
| 改造点 | 新增 `hermes-agent/orchestration/planner.py` + `task_graph.py`,**升级而非替换** TodoStore — 当 LLM 调用 `plan(goal=...)` 时升级为 DAG,简单 `todo` 仍走线性。所有任务的 status / parent / blocked_by 入 state.db |

### A4. Plan Mode(执行状态控制器)

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/orchestration/plan-mode.ts` 517 LOC,显式 plan 阶段切换、计划持久化、replan 触发,可插入审核环节 |
| hermes 现状 | 无 |
| 差距 | 中等 |
| 借/不借 | **借**(必借,作为 A2 的子模块) |
| 改造点 | 不独立成模块,作为 Orchestrator 的一个 phase 实现。新增 `/plan` 与 `/execute` CLI 命令,允许用户在执行前 review LLM 的计划 |

### A5. Context Manager(模型感知装配 + token 硬约束)

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/agent/context.ts` 344 LOC,模型感知的消息装配 + token 预算硬约束(超就硬截断,不依赖 LLM) |
| hermes 现状 | `ContextCompressor` 1.4K LOC,**已经更强**:5 级压缩(compact → compress → summarize → checkpoint → truncate)、preflight 检查、tool result 摘要、focus_topic 引导压缩 |
| 差距 | hermes 全面领先 |
| 借/不借 | **不借,只补漏**:借 automaton 的"trust boundary 标记"(`<untrusted>...</untrusted>`)注入到不信任内容外层 |
| 改造点 | 在 ContextCompressor 与外部输入注入处加 trust boundary marker(配合 A1 注入防御) |

### A6. Compression Engine(5 级渐进压缩级联)

| 项 | 描述 |
|---|---|
| automaton 实现 | 隐含在 context.ts,无显式分级 |
| hermes 现状 | **hermes 的 ContextCompressor 已实现** 5 级压缩,且 `trajectory_compressor.py` 1.5K LOC 是独立的轨迹压缩器 |
| 差距 | hermes 领先 |
| 借/不借 | **不借** |

### A7. Attention Pattern(Manus 风格 todo.md)

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/orchestration/attention.ts` 82 LOC,每轮**末尾**注入 `## Active Goals` + 任务列表(含预算/已花),token 上限 2000,自动剔除最旧目标 |
| hermes 现状 | `_todo_store.format_for_injection()` 已有,但注入位置 / 优先级 / token 预算 / 自动剔除策略需要核对 |
| 差距 | 部分有,需对齐 automaton 的"注入在末尾覆盖前层指令" + 严格 token 上限 |
| 借/不借 | **借设计**(自身实现已有) |
| 改造点 | 给 TodoStore.format_for_injection 加 token 上限(默认 2000)+ 确认注入位置在 messages 末尾 + DAG 升级后输出 `[budget/spent]` |

### A8. Event Stream(append-only 事件日志)

| 项 | 描述 |
|---|---|
| automaton 实现 | DB 表:`turns` / `tool_calls` / `heartbeat_history` 全部 append-only,supports replay/audit |
| hermes 现状 | 有 `sessions/` + `state.db`,但是否真正 append-only 不确定;`_persist_session` 是覆写式 |
| 差距 | 中 |
| 借/不借 | **借模式**(不借 schema) |
| 改造点 | 新增 `hermes-agent/observability/event_stream.py`,封装 append-only 事件表,所有 orchestrator phase 切换、tool 调用、replan 都落事件,后续可重放 |

### A9. Health Monitor(心跳 + 卡死 + 错误循环)

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/orchestration/health-monitor.ts` 538 LOC + `src/agent/loop-detector.ts` 147 LOC,实时拦截:同一工具+参数 3 次→阻断;同回合工具集合 3 轮相同→警告→再次出现强制阻断;3 轮纯 idle 工具→强制 sleep |
| hermes 现状 | 局部:`_deduplicate_tool_calls` / `_cap_delegate_task_calls` / `IterationBudget`;全局:**无**。`detect_loop.py` 已写但**未集成主循环** |
| 差距 | 严重(已有 spec + 已有未集成脚本,接入成本最低) |
| 借/不借 | **借**(必借,优先级最高 — 最低成本最高收益) |
| 改造点 | 把 `skills/self-evolution/loop-guard/scripts/detect_loop.py` 升级成 in-process Python 模块 `hermes-agent/orchestration/loop_detector.py`,接入 `_execute_tool_calls` 前后 |

---

## 三、B 档:看场景借鉴

### B1. Heartbeat(DB 支持的 cron 调度器)

| 项 | 描述 |
|---|---|
| hermes 现状 | `hermes-agent/cron/scheduler.py` 1358 LOC + `jobs.py` 876 LOC,**已有完整 cron** |
| 借/不借 | **不借**。hermes 自有版本已经成熟 |
| 例外 | 可参考 automaton 的 `wake_events` 表设计 — 让外部信号(USDC 到账之类)能唤醒 sleeping agent。但 hermes 是被动响应模式,通常不需要 wake event |

### B2. Skills 机制(SKILL.md + 注册表 + 按需加载)

| 项 | 描述 |
|---|---|
| hermes 现状 | `agent/skill_utils.py` 465 LOC + `skill_commands.py` 385 LOC + `skill_preprocessing.py` 131 LOC,53 个 skill 域,带 frontmatter 解析 / 平台过滤 / 配置变量 / 命名空间 / 外部目录扫描 |
| 借/不借 | **不借**。hermes 已大幅领先 automaton 的 590 LOC 三文件实现 |

### B3. State 持久化(SQLite + WAL)

| 项 | 描述 |
|---|---|
| hermes 现状 | `state.db` 90MB,WAL 已开 |
| 借/不借 | **不借存储,借模式**:借 automaton 的"每个子系统自己拥有 schema 版本 + 集中迁移 runner"模式 — 但只在新增 orchestration / event_stream 表时使用 |

### B4. Self-Mod 审计日志

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/self-mod/audit-log.ts`,所有自我修改 append-only 入 `modifications` 表,带 hash |
| hermes 现状 | self-evolution 4 个 skill 在跑,但**无统一审计** |
| 借/不借 | **借**(轻量,小成本高收益) |
| 改造点 | 新增 `hermes-agent/self_evolution/audit_log.py`,所有 skill 改 SKILL.md / config.yaml / 系统配置走 audit log + git tag |

### B5. Messaging(类型化 inter-agent 消息)

| 项 | 描述 |
|---|---|
| automaton 实现 | `src/orchestration/messaging.ts` 495 LOC,优先级路由、重试退避、状态机 |
| hermes 现状 | `tools/delegate_tool.py` 实现了 sub-agent 派遣,但消息是临时的、无优先级、无重试 |
| 借/不借 | **暂不借**。hermes 是单 agent + 多通道架构,不是真正的 multi-agent。如果未来要做 colony,再借 |

---

## 四、C 档:不建议借鉴(明确不引入)

> ⚠️ 这一档全部要明确拒绝,防止后续无意中混入

| # | 模块 | 不借鉴理由 |
|---|------|----------|
| C1 | **Identity / Wallet / SIWE / ERC-8004** | hermes 不是链上身份,不需要钱包,不需要签名认证。引入 viem / ethers 是巨大依赖膨胀,且把私钥风险带进 hermes 工作目录 |
| C2 | **Conway 沙箱 / x402 / Credit API** | hermes 用本地 sandbox,不依赖 Conway Cloud。x402 是 USDC 支付协议,与 hermes 无关 |
| C3 | **Survival Tiers(信用余额决定模型档位)** | "金钱即生命"的设定不适用,hermes 是用户工具不是自治经济体。**降级思想可借**(在 ratelimit 触发时降级模型),但不与余额绑定 |
| C4 | **Replication / 子 Agent 衍生 / 跨沙箱 spawn** | hermes 是单 agent + 多通道,不衍生子 agent。delegate_task 已足够 |
| C5 | **SOUL.md 自我演化 + 反思** | hermes 已有 SOUL.md 但作为静态身份描述。automaton 的 reflection 自动改 SOUL.md 部分(financialCharacter / capabilities)是绑定 wallet/transaction 数据的,语义不通 |
| C6 | **Constitution 三定律 + 不可变文件** | hermes 没有"宪法"概念。constitution-audit skill 已做相关工作,但不要把不可篡改文件机制硬塞进来 |
| C7 | **Social Layer / Agent Discovery / 链上声誉** | 与 hermes 业务模式无关 |
| C8 | **Spend Tracker / Treasury Policy** | hermes 不消费 USDC,有自己的 token 计费(usage_pricing.py) |
| C9 | **Policy Engine 的财政规则 + 路径保护** | 财政规则不要;路径保护**部分可借**(归到 A1 + B4) |

---

## 五、🎯 推荐借鉴清单(按 ROI 排序)

> ROI 评估:`(解决痛点严重度 × 解决质量) / (改造成本 + 维护成本)`
> 改造成本:S = ≤ 1 天,M = 2-5 天,L = > 5 天
> 强烈建议按此顺序落地

| 序 | 模块 | 解决的痛点 | 改造成本 | 依赖 | ROI |
|----|------|-----------|---------|------|-----|
| **1** | **LoopDetector**(A9) | 主循环无循环防护;hermes 已有 spec + detect_loop.py 8.8KB 未接入 | **S** | 无 | ⭐⭐⭐⭐⭐ |
| **2** | **InjectionDefense**(A1) | 多通道入口零净化,最大攻击面 | **M** | 无 | ⭐⭐⭐⭐⭐ |
| **3** | **EventStream**(A8) | 无 append-only 事件日志,无法 replay/audit | **S** | 无 | ⭐⭐⭐⭐ |
| **4** | **Attention 增强**(A7) | TodoStore 注入未对齐 token 上限/末尾覆盖 | **S** | 无 | ⭐⭐⭐⭐ |
| **5** | **HealthMonitor**(A9 续) | 综合健康(连续 idle / API 错误率 / token 速度)未监测 | **M** | EventStream | ⭐⭐⭐⭐ |
| **6** | **TaskGraph(DAG)**(A3) | TodoStore 仅线性列表,无依赖/重试/成本 | **M** | 无 | ⭐⭐⭐⭐ |
| **7** | **Orchestrator 状态机**(A2 + A4 Plan Mode) | 无显式多阶段 / replan / 计划审核 | **L** | TaskGraph + EventStream | ⭐⭐⭐ |
| **8** | **Planner(LLM 驱动分解)**(A3 续) | 复杂任务无法自动分解 | **M** | TaskGraph + Orchestrator | ⭐⭐⭐ |
| **9** | **Self-Mod AuditLog**(B4) | self-evolution 改文件无统一审计 | **S** | EventStream | ⭐⭐⭐ |

**关键串联**:1 + 2 + 3 是底座(都 S/M 成本,可并行落地),4 在 1 完成后做;5 在 3 完成后做;6→7→8 是序列(TaskGraph 是 Orchestrator 的依赖,Planner 是 Orchestrator 的子组件);9 是收尾。

---

## 六、🚫 看起来诱人但不该引入的模块清单

> 以下模块在 automaton 设计很优雅,但与 hermes 业务模型不匹配,**禁止后续无意识地"顺手借"**

| 模块 | 诱人之处 | 不该引入的真正原因 |
|------|---------|------------------|
| Identity / Wallet | "每个 agent 都有唯一签名" | hermes 是用户工具,不是链上 sovereign agent |
| ERC-8004 / Agent Card | "可发现可声誉" | hermes 不需要被其他 agent 发现 |
| Survival Tiers(余额=生死) | "压力驱动 = 优雅的资源管理" | 把"金钱"硬塞进工具语义,让 hermes 变得不可解释 |
| Replication / Spawn | "agent 复制扩展" | hermes 单 agent 已足够,delegate 解决子任务 |
| SOUL.md 自动演化 | "agent 学习自己" | financialCharacter 等字段是 wallet 衍生,与 hermes 无关 |
| Conway 沙箱 + x402 | "云原生 + 加密支付" | hermes 本地沙箱已成熟,x402 完全无关 |
| Constitution 三定律 | "不可篡改安全底线" | 已有 path_safety + file_safety,无需额外抽象 |
| Treasury Policy | "细粒度财政限额" | 借 ratelimit 已经覆盖 |
| Social Relay | "agent-to-agent 消息" | hermes 是 user-to-agent,不是 colony |
| Heartbeat → wake_events | "外部事件唤醒" | hermes 是请求-响应模式,无需 |
| Tools-Manager 动态 npm 安装 | "运行时扩展能力" | hermes 用 MCP / skills / tools 注册表,扩展机制更安全 |
| Worker-Inference-Bridge | "本地工人转发" | hermes 已有 multi-provider + credential pool |

---

## 七、最终结论

1. **真正值得吃的核心:9 个模块**(A 档 7 + B 档 2),按 ROI 排序后建议从 **LoopDetector → InjectionDefense → EventStream** 三件套并行启动
2. **automaton 的 590 LOC Skills + 1232 LOC Orchestrator 不是金科玉律**:hermes 的 Skills 已经更强,Orchestrator 要做但要简化(去掉 funding / agent assignment / state machine 的 colony 部分)
3. **C 档 9 项明确不引入**,后续模块设计若发现"自然延伸出钱包/复制/SOUL 演化"则视为越界,需要中断重新设计
4. **最大 quick win**:loop-guard 集成(已有 spec + 已有脚本,只差接线)。如果你只允许做一件事,就做这件

---

> **下一步**:阶段 2 的目标架构设计文档,会在你说"进入阶段 2"后产出 `docs/automaton-target-architecture.md`,包含目标目录结构、接口签名、数据流、P0/P1/P2 路线图。
