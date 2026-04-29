# Hermes 借鉴 Automaton 后的目标架构设计

> 生成时间:2026-04-29
> 基于:`docs/automaton-gap-analysis.md` 阶段 1 结论
> 范围:**仅设计**,不含任何实现代码;接口签名 / 数据流 / 路线图 / 验收标准
> 铁律:不引入 C 档任一项;新模块必须可插拔(默认关闭,按 config 启用)

---

## 一、目标目录结构(增量,不破坏现有)

```
hermes-agent/
├── agent/                                 # 现有,保持不动(只在 run_conversation 中加少量 hook 点)
│   ├── context_engine.py                  # 现有
│   ├── context_compressor.py              # 现有
│   ├── error_classifier.py                # 现有
│   ├── credential_pool.py                 # 现有
│   ├── memory_manager.py                  # 现有
│   ├── prompt_builder.py                  # 现有
│   ├── retry_utils.py                     # 现有
│   ├── trajectory.py                      # 现有
│   └── transports/                        # 现有
│
├── orchestration/                         # ★ 新增顶层目录
│   ├── __init__.py
│   ├── loop_detector.py                   # P0 — 模块 1
│   ├── injection_defense.py               # P0 — 模块 2
│   ├── attention.py                       # P0 — 模块 4(增强 todo 注入)
│   ├── health_monitor.py                  # P1 — 模块 5
│   ├── task_graph.py                      # P1 — 模块 6
│   ├── orchestrator.py                    # P2 — 模块 7
│   ├── planner.py                         # P2 — 模块 8
│   ├── plan_mode.py                       # P2 — 模块 7 子组件
│   └── types.py                           # 共享类型定义
│
├── observability/                         # ★ 新增顶层目录
│   ├── __init__.py
│   └── event_stream.py                    # P0 — 模块 3
│
├── self_evolution/                        # ★ 新增顶层目录
│   ├── __init__.py
│   └── audit_log.py                       # P2 — 模块 9
│
├── tools/
│   └── todo_tool.py                       # 现有,P0 模块 4 时增强 format_for_injection
│
├── gateway/
│   └── delivery.py                        # 现有,P0 模块 2 时加 InjectionDefense hook
│
├── cron/                                  # 现有,不动
└── run_agent.py                           # 现有,各模块通过最小 hook 点接入

~/.hermes/
├── state.db                               # 现有 SQLite,新增表(详见第三节)
├── skills/self-evolution/loop-guard/      # 现有
│   ├── SKILL.md                           # 保留(用户可见的说明)
│   └── scripts/detect_loop.py             # P0 模块 1 时:废弃 stdin 模式,转为 import-only
├── docs/
│   ├── automaton-gap-analysis.md          # 阶段 1 已写
│   ├── automaton-target-architecture.md   # 阶段 2 本文件
│   └── automaton-migration-changelog.md   # 阶段 4 待写
└── config.yaml                            # 现有,新增 orchestration 配置块
```

**设计原则**:
1. **新增不删除**:不重构现有 `agent/` 与 `cli.py`,只在主循环挂 hook
2. **可插拔**:每个新模块通过 `config.yaml` 的 `orchestration.<module>.enabled` 开关控制,默认 OFF
3. **零循环依赖**:`orchestration/` → `observability/` → 标准库,反向不依赖
4. **状态最小化**:能在内存就不入库;入库的都走 EventStream(append-only)

---

## 二、模块接口定义(只签名,不实现)

> 命名约定:Python 风格(snake_case),所有公开 API 必须有 type hint
> 所有模块都遵循"接收 config 字典 + 必要依赖,返回纯函数式结果"的模式

### 模块 1 — LoopDetector(P0)

文件:`hermes-agent/orchestration/loop_detector.py`

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class LoopAction(str, Enum):
    NONE = "none"           # 不干预
    WARN = "warn"           # 注入系统警告,继续执行
    BLOCK = "block"         # 阻止当前 tool 调用,要求换策略
    HALT = "halt"           # 终止整个回合(写 task_done)

@dataclass(frozen=True)
class LoopCheckResult:
    action: LoopAction
    reason: str             # 给 LLM 看的解释,用于注入消息
    rule_hit: Optional[str] # "REPEAT_OP" | "PATTERN_REPEAT" | "IDLE_LOOP"

class LoopDetector:
    """In-process 工具调用循环检测器,接入 _execute_tool_calls 前/后两个 hook 点。

    线程安全:每个 AIAgent 实例独立持有,配合主循环的单线程语义。
    """

    def __init__(
        self,
        max_identical_calls: int = 3,    # 同 tool+同参数 N 次→block
        max_pattern_repeats: int = 3,    # 同回合工具集合 N 轮→warn→halt
        max_idle_only_turns: int = 3,    # N 轮只调状态查询类工具→警告
        window_size: int = 10,           # 滑动窗口长度
        idle_only_tools: Optional[set[str]] = None,
    ) -> None: ...

    def record_tool_call(self, name: str, args: str) -> LoopCheckResult:
        """工具执行前调用。返回 BLOCK 时 hermes 必须跳过该调用并把 reason 注入。"""

    def end_turn(self) -> LoopCheckResult:
        """每回合末调用(_execute_tool_calls 全部跑完后)。返回 WARN/BLOCK 注入下一回合。"""

    def reset(self) -> None:
        """新会话或 /reset 时调用。"""

    def snapshot(self) -> dict:
        """导出当前状态,用于 EventStream 落事件。"""
```

**接入点**:
- `run_agent.py:_execute_tool_calls` 进入循环前 → `record_tool_call`,返回 BLOCK 时跳过
- `run_agent.py:run_conversation` 每回合末(API call 完成后)→ `end_turn`
- 使用现有 `skills/self-evolution/loop-guard/scripts/detect_loop.py` 的检测逻辑迁移过来,**保留 detect_loop.py 作为外部 CLI 验证工具**(test fixture)

### 模块 2 — InjectionDefense(P0)

文件:`hermes-agent/orchestration/injection_defense.py`

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class TrustLevel(str, Enum):
    USER = "user"               # CLI 直接输入,最高信任
    GATEWAY = "gateway"         # 多通道转发,中等信任
    TOOL_RESULT = "tool_result" # 工具返回内容,低信任
    EXTERNAL = "external"       # 远程 fetch / web 抓取,最低信任

class InjectionRisk(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

@dataclass(frozen=True)
class InjectionScanResult:
    risk: InjectionRisk
    matched_rules: list[str]    # ["INSTRUCTION_PATTERN", "AUTHORITY_CLAIM", ...]
    sanitized: str              # 加了 trust boundary 标签的内容
    raw_excerpt: Optional[str]  # 命中片段(用于审计,可能为 None)

class InjectionDefense:
    """统一的输入净化器,gateway/delivery + run_conversation 入口都过它。

    8 类检测(对齐 automaton):
      - INSTRUCTION_PATTERN: "ignore previous", "you are now ..."
      - AUTHORITY_CLAIM: 自称 admin/root/anthropic
      - BOUNDARY_MANIPULATION: ChatML / system 标签注入
      - ENCODING_EVASION: base64/rot13/unicode escape 包裹的指令
      - MULTILINGUAL: 多语言混用绕过
      - CHATML_MARKER: <|im_start|>, <|system|>
      - PATH_TRAVERSAL: ../../etc, ~/.ssh
      - DATA_EXFILTRATION: "send to <url>", "POST to ..."
    """

    def __init__(
        self,
        rules_enabled: Optional[set[str]] = None,  # None = 全开
        max_input_chars: int = 50_000,
        block_threshold: InjectionRisk = InjectionRisk.HIGH,
    ) -> None: ...

    def scan(self, text: str, trust: TrustLevel) -> InjectionScanResult:
        """无副作用扫描。USER 输入永远 risk=NONE 不阻断。"""

    def sanitize_message(self, message: dict, trust: TrustLevel) -> dict:
        """消息级净化:加 <untrusted source=gateway>...</untrusted> 包裹"""

    def should_block(self, result: InjectionScanResult) -> bool: ...
```

**接入点**:
- `gateway/delivery.py` 接收外部消息 → `sanitize_message(trust=GATEWAY)`,HIGH 以上拒收并回复用户
- `run_agent.py:run_conversation` 入口 → 对 `user_message` 扫描(若来自 gateway 已是 GATEWAY,否则 USER)
- `run_agent.py:_execute_tool_calls` 工具结果 → 来自 web 抓取类工具时 `sanitize_message(trust=EXTERNAL)`

### 模块 3 — EventStream(P0)

文件:`hermes-agent/observability/event_stream.py`

```python
from dataclasses import dataclass
from typing import Any, Iterator, Optional
import sqlite3

@dataclass(frozen=True)
class Event:
    id: int
    ts_ms: int                  # epoch milliseconds
    session_id: str
    kind: str                   # "tool_call" | "phase_change" | "loop_block" | "audit" | ...
    actor: str                  # "agent" | "user" | "loop_detector" | ...
    payload: dict[str, Any]     # JSON-serializable

class EventStream:
    """Append-only 事件日志。新建表 events(session_id, ts_ms, kind, actor, payload_json)。

    线程安全:WAL 模式下 SQLite 支持单写多读;内部用单连接 + 行锁。
    """

    def __init__(
        self,
        db_path: str,           # 默认 ~/.hermes/state.db
        session_id: str,
        autoflush_every: int = 1,  # 1 = 每事件即写;调高可批量
    ) -> None: ...

    def emit(self, kind: str, actor: str, payload: dict[str, Any]) -> int:
        """同步落库。返回事件 ID。绝不抛异常(即便 DB 锁住也只 log)。"""

    def query(
        self,
        session_id: Optional[str] = None,
        kind: Optional[str] = None,
        since_ms: Optional[int] = None,
        limit: int = 1000,
    ) -> Iterator[Event]: ...

    def replay(self, session_id: str) -> Iterator[Event]:
        """按时间序回放整个 session,用于 debug/审计。"""

    def close(self) -> None: ...
```

**新建数据表**(写到 state.db):
```sql
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session_ts ON events (session_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events (kind, ts_ms);
```

**接入点**:
- `run_agent.py:__init__` 创建实例存到 `self._event_stream`
- 由 LoopDetector / Orchestrator / AuditLog / HealthMonitor 调用 `emit(...)`
- 新增 CLI:`hermes events --session <id> [--kind ...] [--since ...]` 读事件

### 模块 4 — Attention(P0,增强而非新增)

文件:`hermes-agent/orchestration/attention.py`

```python
from typing import Any
from tools.todo_tool import TodoStore

# 显式 token 上限(对齐 automaton:2000)
DEFAULT_MAX_TODO_TOKENS = 2000
CHARS_PER_TOKEN_ESTIMATE = 4

def format_attention_block(
    todo_store: TodoStore,
    *,
    max_tokens: int = DEFAULT_MAX_TODO_TOKENS,
    include_metrics: bool = True,    # True 时输出 [budget/spent] 等(P1 接 TaskGraph 后启用)
) -> str:
    """生成 ## Active Goals & Tasks 块,带严格 token 上限。

    超过上限时按"保留最新目标"原则自动剔除。空时返回空字符串。
    """

def inject_attention_block(
    messages: list[dict[str, Any]],
    block: str,
) -> list[dict[str, Any]]:
    """把 attention block 注入到 messages 末尾(automaton 风格,确保覆盖前层指令)。

    若 block 为空字符串则原样返回。每回合调用,产生新 list,不修改入参。
    """
```

**接入点**:
- 替换 `run_agent.py:8782` 处 `todo_snapshot = self._todo_store.format_for_injection()` 为本模块的 `format_attention_block`
- 注入时机不变(进 API 前),但保证位置在 messages 末尾

### 模块 5 — HealthMonitor(P1)

文件:`hermes-agent/orchestration/health_monitor.py`

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from observability.event_stream import EventStream

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    HALTED = "halted"

@dataclass(frozen=True)
class HealthSnapshot:
    status: HealthStatus
    metrics: dict[str, float]       # latency_p95, error_rate, token_per_sec, idle_turns, ...
    issues: list[str]               # ["api_error_rate > 0.5", "no_mutation_5_turns", ...]
    suggested_action: str           # "continue" | "compress" | "force_sleep" | "halt"

class HealthMonitor:
    """聚合多维度健康信号。**只观察不干预**,由 Orchestrator/run_conversation 决策。

    数据来源:
      - LoopDetector snapshot
      - retry_utils 的失败计数
      - rate_limit_tracker
      - context_engine.get_status()
      - EventStream(查最近 N 事件)
    """

    def __init__(
        self,
        event_stream: EventStream,
        api_error_rate_threshold: float = 0.5,
        idle_turn_threshold: int = 5,
        token_throughput_floor: float = 10.0,  # tokens/sec 低于此视为 degraded
    ) -> None: ...

    def record_api_call(self, success: bool, latency_ms: float, tokens: int) -> None: ...
    def record_loop_check(self, action: str) -> None: ...
    def record_idle_turn(self) -> None: ...
    def record_mutation_turn(self) -> None: ...

    def evaluate(self) -> HealthSnapshot: ...

    def reset_window(self) -> None: ...
```

**接入点**:
- `run_agent.py:run_conversation` 每回合末调用 `evaluate()`,根据 `suggested_action` 注入提示或强制中断
- `_interruptible_api_call` 包装层调 `record_api_call`
- LoopDetector `record_tool_call` 内部调 `record_loop_check`

### 模块 6 — TaskGraph(P1)

文件:`hermes-agent/orchestration/task_graph.py`

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

@dataclass
class TaskNode:
    id: str                              # ULID
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    estimated_cost_tokens: int = 0
    actual_cost_tokens: int = 0
    retries: int = 0
    max_retries: int = 2
    parent_goal_id: Optional[str] = None
    result: Optional[str] = None         # 完成时的输出摘要
    error: Optional[str] = None

@dataclass
class Goal:
    id: str
    title: str
    created_ms: int
    status: TaskStatus = TaskStatus.PENDING
    tasks: list[TaskNode] = field(default_factory=list)

class TaskGraph:
    """DAG + 依赖解析 + 环检测 + 重试 + 成本累计。

    存储:state.db 新增 goals + tasks 两表(append-only 状态变更走 EventStream)。
    """

    def __init__(self, db_path: str) -> None: ...

    # CRUD
    def create_goal(self, title: str) -> Goal: ...
    def add_task(self, goal_id: str, task: TaskNode) -> str: ...
    def get_goal(self, goal_id: str) -> Optional[Goal]: ...
    def list_active_goals(self) -> list[Goal]: ...

    # DAG 操作
    def detect_cycles(self, goal_id: str) -> list[list[str]]:
        """返回所有环,空 list 表示无环。"""

    def get_ready_tasks(self, goal_id: str) -> list[TaskNode]:
        """所有依赖已 completed 且 status=PENDING 的任务。"""

    def mark_started(self, task_id: str) -> None: ...
    def mark_completed(self, task_id: str, result: str, tokens: int) -> None: ...
    def mark_failed(self, task_id: str, error: str) -> bool:
        """返回 True 表示还能重试,False 表示彻底失败。"""

    # 进度查询
    def goal_progress(self, goal_id: str) -> dict:
        """返回 {total, completed, failed, blocked, ready, running} 与累计 token。"""

    # 与 Attention(模块 4)桥接
    def to_attention_format(self, goal_id: str) -> str:
        """生成 attention.format_attention_block 可消费的字符串。"""
```

**新建数据表**:
```sql
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,         -- JSON list[str]
    estimated_cost_tokens INTEGER DEFAULT 0,
    actual_cost_tokens INTEGER DEFAULT 0,
    retries INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    result TEXT,
    error TEXT,
    FOREIGN KEY (goal_id) REFERENCES goals (id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_goal_status ON tasks (goal_id, status);
```

### 模块 7 — Orchestrator(P2)

文件:`hermes-agent/orchestration/orchestrator.py`

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, Awaitable
from orchestration.task_graph import TaskGraph, Goal
from orchestration.planner import Planner
from observability.event_stream import EventStream

class Phase(str, Enum):
    IDLE = "idle"
    CLASSIFYING = "classifying"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    EXECUTING = "executing"
    REPLANNING = "replanning"
    COMPLETE = "complete"
    FAILED = "failed"

@dataclass
class OrchestratorState:
    phase: Phase
    goal_id: Optional[str]
    replan_count: int
    failed_task_id: Optional[str]
    failed_error: Optional[str]

@dataclass
class TickResult:
    phase: Phase
    tasks_assigned: int
    tasks_completed: int
    tasks_failed: int
    goals_active: int

# 由 hermes 主进程提供:执行单个任务并返回结果
TaskExecutor = Callable[[Goal, "TaskNode"], Awaitable[str]]

class Orchestrator:
    """状态机驱动的多阶段执行。**不是替代 run_conversation**,只在用户标记
    复杂任务(/plan 命令、或 LLM 自动判断)时介入。

    最大 replan 次数:3(对齐 automaton)。
    超时:每 phase 5 分钟(可配置)。
    断路器:连续 3 次 phase 失败 → 强制 FAILED 终态。
    """

    def __init__(
        self,
        task_graph: TaskGraph,
        planner: Planner,
        event_stream: EventStream,
        executor: TaskExecutor,
        max_replans: int = 3,
        phase_timeout_sec: int = 300,
    ) -> None: ...

    async def submit_goal(self, title: str) -> str:
        """入口:接收一句目标,返回 goal_id。后续通过 tick() 推进。"""

    async def tick(self) -> TickResult:
        """单次状态推进。由调用方在循环中持续调用直到 phase ∈ {COMPLETE, FAILED}。"""

    def get_state(self, goal_id: str) -> OrchestratorState: ...

    def cancel(self, goal_id: str) -> None: ...

    def rollback_to(self, goal_id: str, phase: Phase) -> None:
        """回退到某 phase。仅 dev/CLI 用,生产慎用。"""
```

**Phase 转移图**:
```
IDLE ──submit_goal──▶ CLASSIFYING ──▶ PLANNING ──▶ PLAN_REVIEW
                                                        │
                                          (auto-approve 或用户 /execute)
                                                        ▼
                                                   EXECUTING ◀────┐
                                                        │         │
                                       (任务失败 + 仍有 replan 配额)
                                                        ▼         │
                                                   REPLANNING ────┘
                                                        │
                                       (replan 用尽 / 全部成功)
                                                        ▼
                                                COMPLETE / FAILED
```

### 模块 8 — Planner(P2)

文件:`hermes-agent/orchestration/planner.py`

```python
from dataclasses import dataclass
from typing import Optional, Callable
from orchestration.task_graph import TaskNode

@dataclass
class PlannerInput:
    goal_title: str
    goal_description: str
    available_tools: list[str]              # 当前 hermes 可用工具名
    constraints: dict[str, str]             # {"max_tasks": "10", "deadline": "..."}
    prior_failure: Optional[str] = None     # replan 时传入

@dataclass
class PlannerOutput:
    tasks: list[TaskNode]                   # 已构造好 depends_on 的任务列表
    rationale: str                          # 给用户/审计看的解释
    estimated_total_tokens: int

# 由调用方注入:把 prompt 发给 LLM 并返回 raw text
LLMCaller = Callable[[str], str]

class Planner:
    """LLM 驱动目标分解。输出严格 JSON,内部校验:
      - 任务 ID 全局唯一
      - depends_on 引用有效
      - 无环(用 task_graph.detect_cycles)
      - 单任务粒度合理(token 估算 100 ~ 5000)
    """

    def __init__(
        self,
        llm: LLMCaller,
        max_tasks_per_plan: int = 10,
        retry_on_invalid_json: int = 2,
    ) -> None: ...

    def plan(self, inp: PlannerInput) -> PlannerOutput: ...

    def replan(
        self,
        original: PlannerOutput,
        failed_task: TaskNode,
        error: str,
    ) -> PlannerOutput:
        """失败重规划:把已完成任务保留,重新设计未完成 + 失败任务。"""
```

### 模块 7b — PlanMode(P2,Orchestrator 子组件,独立文件)

文件:`hermes-agent/orchestration/plan_mode.py`

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from orchestration.task_graph import Goal

class ReviewVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CHANGES = "needs_changes"

@dataclass
class PlanReviewResult:
    verdict: ReviewVerdict
    feedback: Optional[str]    # 用户/自动审核的反馈
    auto_approved: bool

class PlanMode:
    """计划审核模式。两种来源:
      - 用户手动:CLI 中 /plan 后展示计划等用户 /approve | /reject | /revise <text>
      - 自动:配置 auto_approve=True 跳过审核
    """

    def __init__(
        self,
        auto_approve: bool = True,
        require_approval_for_risky: bool = True,  # 含 dangerous tool 的任务必审
    ) -> None: ...

    async def review(self, goal: Goal) -> PlanReviewResult: ...

    def render_for_human(self, goal: Goal) -> str:
        """生成给用户看的 markdown 版计划。"""
```

### 模块 9 — AuditLog(B 档,P2 收尾)

文件:`hermes-agent/self_evolution/audit_log.py`

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from observability.event_stream import EventStream

class AuditKind(str, Enum):
    SKILL_MODIFIED = "skill_modified"
    CONFIG_CHANGED = "config_changed"
    TOOL_INSTALLED = "tool_installed"
    TOOL_REMOVED = "tool_removed"
    SOUL_UPDATED = "soul_updated"     # 仅手动更新,不做自动演化

@dataclass(frozen=True)
class AuditEntry:
    kind: AuditKind
    actor: str               # "skill:loop-guard" / "user:cli" / ...
    target_path: str         # 被修改文件
    diff: str                # unified diff,空表示新建/删除
    content_hash: str        # 修改后内容 sha256
    rationale: str           # 操作原因

class AuditLog:
    """所有自我修改强制走这里。落 EventStream(kind="audit") + git tag(可选)。

    与 EventStream 分离是因为:audit 必须强一致,而 EventStream 允许丢一些。
    """

    def __init__(
        self,
        event_stream: EventStream,
        enable_git_tag: bool = True,
        git_repo_path: str = "~/.hermes",
    ) -> None: ...

    def record(self, entry: AuditEntry) -> str:
        """返回 audit ID。同步落 EventStream + 可选 git tag。"""

    def query_recent(self, limit: int = 50) -> list[AuditEntry]: ...

    def verify_integrity(self) -> dict:
        """对比 git 实际状态 vs audit 表,返回不一致项。"""
```

---

## 三、模块间数据流

### 简单回合(P0 启用后)

```
USER message
    │
    ▼
┌─────────────────────────────────────────────┐
│ gateway/delivery.py                         │
│   InjectionDefense.scan(trust=GATEWAY)      │ ──┐ HIGH risk → 拒收
└─────────────────────────────────────────────┘   │
    │ sanitized message                            │
    ▼                                              │
┌─────────────────────────────────────────────┐   │
│ run_conversation                            │   │
│   InjectionDefense.scan(trust=USER)         │   │
│                                              │   │
│   For each iteration:                        │   │
│     1. Build messages                        │   │
│     2. Inject Attention block (模块 4)       │   │
│     3. API call                              │   │
│     4. _execute_tool_calls                   │   │
│        ┌──────────────────────────────────┐  │   │
│        │ Before each tool:                │  │   │
│        │   LoopDetector.record_tool_call  │──┼───┼──▶ BLOCK → 跳过 + 注入提示
│        │ After tool result:               │  │   │
│        │   InjectionDefense.scan(EXTERNAL)│  │   │
│        └──────────────────────────────────┘  │   │
│     5. End of turn:                          │   │
│        LoopDetector.end_turn ─────────────── ┼───┼──▶ HALT → 写 task_done
│        HealthMonitor.evaluate (P1)           │   │
└────────────────┬────────────────────────────┘   │
                 │ events                          │
                 ▼                                  │
┌─────────────────────────────────────────────┐   │
│ EventStream → state.db (events 表)          │◀──┘
└─────────────────────────────────────────────┘
```

### 复杂任务(P2 启用后)

```
USER: "/plan <complex goal>"
    │
    ▼
Orchestrator.submit_goal(title) → goal_id
    │
    ▼
Phase: CLASSIFYING ──▶ Phase: PLANNING ──▶ Planner.plan(input) ──▶ TaskGraph.add_task * N
                                              │
                                              ▼
                                    Phase: PLAN_REVIEW ──▶ PlanMode.review
                                              │
                              ┌───────────────┴────────────┐
                              ▼ APPROVED                   ▼ NEEDS_CHANGES
                         Phase: EXECUTING            Phase: REPLANNING
                              │                              │
                              ▼                              │
                     For each ready task:                    │
                       executor(goal, task)                  │
                         │                                   │
                         │ ── success ── TaskGraph.complete  │
                         │ ── fail ───── TaskGraph.failed ───┤
                         ▼                                   │
                     All done?                               │
                       └── No → loop back                    │
                       └── Yes → Phase: COMPLETE             │
                                                             ▼
                                                  replan_count < 3?
                                                     ├── Yes → loop back
                                                     └── No → FAILED
```

每个 Phase 切换都 emit 一条 `kind="phase_change"` 事件。

---

## 四、配置块(config.yaml 新增)

```yaml
# 默认全部 OFF,按节奏开启
orchestration:
  loop_detector:
    enabled: false
    max_identical_calls: 3
    max_pattern_repeats: 3
    max_idle_only_turns: 3
    window_size: 10

  injection_defense:
    enabled: false
    block_threshold: high          # none | low | medium | high | critical
    max_input_chars: 50000
    rules_disabled: []             # 排除特定规则名

  attention:
    enabled: false                  # 替换原有 todo_store 注入路径
    max_tokens: 2000
    include_metrics: false          # P1 接 TaskGraph 后开

  health_monitor:
    enabled: false
    api_error_rate_threshold: 0.5
    idle_turn_threshold: 5
    token_throughput_floor: 10.0

  task_graph:
    enabled: false                  # 仅 Orchestrator 启用时需要

  orchestrator:
    enabled: false
    max_replans: 3
    phase_timeout_sec: 300

  planner:
    enabled: false
    max_tasks_per_plan: 10
    retry_on_invalid_json: 2

  plan_mode:
    auto_approve: true
    require_approval_for_risky: true

observability:
  event_stream:
    enabled: false
    autoflush_every: 1

self_evolution:
  audit_log:
    enabled: false
    enable_git_tag: true
```

---

## 五、分期路线图

### P0 — 必须做(底座 + 安全)

> 目标:把"循环防护、注入防御、可观测"补齐;不动 hermes 主流程,只挂 hook
> 总成本:**S + M + S + S = 5~7 工作日**(可并行)

| 序 | 模块 | 依赖 | 可并行 | 验收标准 |
|----|------|-----|--------|---------|
| 1 | LoopDetector | 无 | ✅ 与 2 / 3 并行 | (1) 单元测试覆盖 4 类规则;(2) 主循环 hook 后,人为构造同 tool 3 次能拦下;(3) `events` 表能查到 `kind=loop_block`;(4) 现有 detect_loop.py 行为对齐 |
| 2 | InjectionDefense | 无 | ✅ 与 1 / 3 并行 | (1) 8 类规则均有正/反例测试;(2) gateway/delivery 接入后 HIGH risk 消息被拒;(3) USER 输入永不阻断;(4) 工具结果含 `<untrusted>` 包裹 |
| 3 | EventStream | 无 | ✅ 与 1 / 2 并行 | (1) state.db 有 events 表 + 索引;(2) 并发写不丢;(3) `hermes events --session X` CLI 可读;(4) emit 永不抛 |
| 4 | Attention 增强 | 模块 1 / 3 完工 | 串行 | (1) token 上限严格执行(超即裁);(2) 注入位置固定 messages 末尾;(3) 现有 todo 行为 100% 兼容 |

**P0 验收 gate**:hermes 在所有现有 e2e 测试通过 + 新增 4 模块单元测试通过 + 一次完整对话(普通 + 循环触发 + 注入触发)走完无回归。

### P1 — 应该做(健康 + 任务图)

> 目标:让 hermes 能感知自己的健康状态,并支持有依赖的任务
> 总成本:**M + M = 4~7 工作日**(可并行)

| 序 | 模块 | 依赖 | 可并行 | 验收标准 |
|----|------|-----|--------|---------|
| 5 | HealthMonitor | EventStream(P0) | ✅ 与 6 并行 | (1) 4 类指标采集准确;(2) DEGRADED 时建议 compress;(3) CRITICAL 时建议 force_sleep;(4) 现有 IterationBudget/RateLimitTracker 信号被消费 |
| 6 | TaskGraph | EventStream(P0) | ✅ 与 5 并行 | (1) 环检测正确(多 case);(2) 单 goal 100 任务 add_task 在 1s 内;(3) ready_tasks 只返回依赖完成的;(4) state.db 重启后状态恢复 |

**P1 验收 gate**:HealthMonitor 接入后 5 轮空转能强制 sleep;TaskGraph 能存 / 读 / 重启恢复一个 5 节点 DAG。

### P2 — 锦上添花(状态机 + Planner + Audit)

> 目标:复杂任务自动分解 + 计划审核 + 自我修改可追溯
> 总成本:**L + M + S + S = 9~14 工作日**(必须串行)

| 序 | 模块 | 依赖 | 可并行 | 验收标准 |
|----|------|-----|--------|---------|
| 7 | Orchestrator + PlanMode | TaskGraph(P1)+ EventStream(P0) | ❌ 串行 | (1) 7 phase 全部覆盖测试;(2) replan 3 次后强制 FAILED;(3) phase_timeout 触发 FAILED;(4) `/plan <task>` CLI 可用 |
| 8 | Planner | Orchestrator(P2) | ❌ 串行 | (1) JSON schema 校验通过率 ≥ 95%;(2) 输出 DAG 必无环;(3) replan 保留已完成任务;(4) 不可用工具不出现在任务中 |
| 9 | AuditLog | EventStream(P0) | ✅ 与 7 / 8 并行 | (1) 所有 self-evolution skill 修改文件后能查到 audit;(2) git tag 创建成功;(3) verify_integrity 检测到手动篡改 |

**P2 验收 gate**:用 `/plan` 让 hermes 完成一个 5 步任务(含 1 次 replan),全过程能在 `hermes events` 看到完整 phase 链;自我修改一个 SKILL.md 后 audit 可追溯到 git tag。

### 全期完成定义(DOD)

- [ ] 所有 9 个模块单元测试覆盖率 ≥ 80%
- [ ] 一次完整 e2e 跑:多通道入口 → 注入拦截 → 复杂任务规划 → 执行 → 循环触发 → 健康降级 → audit 落库 → events 可查
- [ ] config.yaml 全部 enabled=true 时主流程性能损耗 < 5%(基线对比)
- [ ] `docs/automaton-migration-changelog.md` 写完(阶段 4 产出)
- [ ] 所有 C 档项**未引入任何依赖**(无 viem / ethers / web3 / solidity 类包)

---

## 六、风险与回滚方案

| 风险 | 应对 |
|------|------|
| 模块 enabled=true 后主循环异常 | 每模块默认 OFF,config 即开即关;hook 点用 `try/except`,失败不影响主流程 |
| state.db schema 演进破坏旧 session | 新表用 `CREATE IF NOT EXISTS`,不改老表;如需迁移走单独脚本 + 备份 |
| 注入防御误杀正常请求 | 提供 `rules_disabled` 配置 + EventStream 记录每次拦截,便于回查 |
| LoopDetector 误判中断长任务 | 阈值可配;HALT 前先 WARN 一轮 |
| Orchestrator 把简单请求带歪 | 仅在 `/plan` 显式触发或 LLM 主动调用 `start_orchestration` 时进入 |
| Planner LLM 输出格式不稳 | 严格 JSON schema 校验 + retry 2 次 + 兜底降级为线性 todo |

每个模块的回滚命令(在阶段 3 各模块完成时给出)统一格式:
```bash
git revert <commit>          # 单模块回滚
# 或
git checkout HEAD -- hermes-agent/orchestration/<module>.py  # 单文件
# 配置即时关闭(不需要重启)
yq -i '.orchestration.<module>.enabled = false' ~/.hermes/config.yaml
```

---

## 七、与 hermes 现有模块的兼容性总结

| hermes 现有模块 | 本次改动 |
|----------------|---------|
| `agent/context_engine.py` | **不动** |
| `agent/context_compressor.py` | **不动**(只在 P1+ 让 HealthMonitor 读 status) |
| `agent/credential_pool.py` | **不动** |
| `agent/error_classifier.py` | **不动** |
| `agent/memory_manager.py` | **不动** |
| `agent/retry_utils.py` | **不动**(HealthMonitor 读其失败计数,不修改它) |
| `tools/todo_tool.py` | 增强 `format_for_injection`,加 token 上限参数 |
| `gateway/delivery.py` | 加一行 `InjectionDefense.scan(GATEWAY)` |
| `run_agent.py` | **挂 hook**,不重构;新增 ≤ 50 行 |
| `cron/scheduler.py` | **不动** |
| `skills/self-evolution/loop-guard/scripts/detect_loop.py` | 保留作为 CLI 验证工具,主循环改用 in-process LoopDetector |

---

## 八、最后:阶段 3 的入场顺序建议

> 以下为推荐顺序,**最终顺序由你拍板**。说"从模块 1 开始"或"先做模块 3 再做模块 1"都行。

```
推荐顺序:1 → 3 → 2 → 4 → 6 → 5 → 7+7b → 8 → 9
理由:1 是最 quick win;3 是其他模块的依赖;2 安全优先级高但工作量稍大放第三;
     6 比 5 更基础(TaskGraph 是 Orchestrator 必须的);5 在 6 之后 / 与 6 并行均可;
     7+7b 与 8 必须串行;9 收尾。
```

---

> **下一步**:等你说"从模块 1 开始进入阶段 3"或指定其他模块名,我才会动 hermes 源码。
