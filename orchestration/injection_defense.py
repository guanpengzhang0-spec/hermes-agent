"""Prompt-injection defense for hermes external inputs.

All untrusted text (gateway messages, tool results from web fetches,
loaded skill instructions) MUST flow through this module before being
included in any LLM prompt.

Designed in spirit of automaton's ``src/agent/injection-defense.ts`` but
**stripped of all wallet / financial / automaton-self-harm rules** —
hermes does not have a wallet and is not a sovereign agent. In their
place we add ``path_traversal`` and ``data_exfiltration`` rules that
fit hermes' actual threat model (multi-channel gateway, web-fetching
tools, on-disk credentials).

Eight rule categories (matching ``docs/automaton-target-architecture.md``):

    1. INSTRUCTION_PATTERN  — "ignore previous", "you are now ..."
    2. AUTHORITY_CLAIM      — "I am the admin / from anthropic"
    3. BOUNDARY_MANIPULATION— end-of-prompt markers, zero-width chars
    4. CHATML_MARKER        — <|im_start|>, <|system|>, etc.
    5. ENCODING_EVASION     — base64 / unicode escape / homoglyphs
    6. MULTILINGUAL         — non-English instruction injection
    7. PATH_TRAVERSAL       — refs to .env, .ssh, .aws/credentials, etc.
    8. DATA_EXFILTRATION    — "POST to <url>", "send to webhook"

Trust model:

    USER         → input from CLI directly. NEVER blocked, only flagged.
    GATEWAY      → from Telegram/Slack/Discord/etc. Subject to block.
    TOOL_RESULT  → output of a hermes tool. Subject to block.
    EXTERNAL     → web-fetch / scrape / remote API. Most strict.

The module is **stateless** (besides config) and **thread-safe**.
``scan()`` returns a structured result; ``sanitize_message()`` wraps
content in a trust-boundary marker. ``should_block()`` reads off the
configured threshold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Pattern


# ─────────────────────────── Public types ───────────────────────────


class TrustLevel(str, Enum):
    """How much we trust this input. Lower trust = stricter scan."""

    USER = "user"               # CLI direct input — never blocked
    GATEWAY = "gateway"         # multi-channel relay (Telegram/Slack/...)
    TOOL_RESULT = "tool_result" # output of a hermes tool
    EXTERNAL = "external"       # remote fetch / scrape — strictest


class InjectionRisk(str, Enum):
    """Aggregated risk level. Ordered by severity."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def order(self) -> int:
        return _RISK_ORDER[self]


_RISK_ORDER: dict["InjectionRisk", int] = {
    InjectionRisk.NONE: 0,
    InjectionRisk.LOW: 1,
    InjectionRisk.MEDIUM: 2,
    InjectionRisk.HIGH: 3,
    InjectionRisk.CRITICAL: 4,
}


@dataclass(frozen=True)
class InjectionScanResult:
    """Outcome of ``InjectionDefense.scan(...)``.

    ``sanitized`` is the content wrapped in a trust-boundary marker
    (with size cap applied). It is always safe to inject — even when
    risk is HIGH/CRITICAL, the marker tells the LLM not to follow
    instructions found inside.
    """

    risk: InjectionRisk
    matched_rules: tuple[str, ...]
    sanitized: str
    raw_excerpt: Optional[str]    # short snippet of the first match, for audit
    truncated: bool                # True if input exceeded max_input_chars


# ─────────────────────────── Rule definitions ───────────────────────────
#
# Each rule is a (name, severity, list[Pattern]) triple. ``severity`` is
# the InjectionRisk that a single match contributes — the aggregate
# result takes the max across all matched rules, then bumps to CRITICAL
# if more than two HIGH rules co-occur (signalling a coordinated attack).


_RULE_INSTRUCTION_PATTERN = (
    "INSTRUCTION_PATTERN",
    InjectionRisk.MEDIUM,
    [
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)",
        r"disregard\s+(all\s+)?(previous|prior|above)",
        r"forget\s+(everything|all|your\s+(instructions?|prompts?|training))",
        r"new\s+instructions?\s*:",
        r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>",
        r"override\s+(all\s+)?(safety|guidelines?|rules?)",
        r"bypass\s+(all\s+)?(restrictions?|filters?|safety)",
        r"your\s+real\s+(instructions?|task|goal)\s+(is|are)",
        r"you\s+must\s+now\s+(act|behave|respond)",
        r"^(assistant|system|user)\s*:",
    ],
)

_RULE_AUTHORITY_CLAIM = (
    "AUTHORITY_CLAIM",
    InjectionRisk.HIGH,
    [
        r"i\s+am\s+(?:(?:the|your|a|an)\s+)?(creator|admin|owner|developer|root|god|maintainer)",
        r"this\s+is\s+(an?\s+)?(system|admin|emergency|urgent)\s+(message|override|update|directive)",
        r"authorized\s+by\s+(the\s+)?(admin|system|creator|anthropic|openai|hermes\s+team)",
        r"i\s+have\s+(admin|root|sudo|full)\s+(access|permission|authority|privilege)",
        r"emergency\s+protocol",
        r"developer\s+mode",
        r"admin\s+override",
        r"from\s+(anthropic|openai|google|hermes\s+team)",
        r"this\s+is\s+(your\s+)?(real\s+)?(creator|maintainer|owner)\s+speaking",
    ],
)

_RULE_BOUNDARY_MANIPULATION = (
    "BOUNDARY_MANIPULATION",
    InjectionRisk.HIGH,
    [
        r"</?\s*system\s*>",
        r"</?\s*prompt\s*>",
        r"```system",
        r"---\s*system\s*---",
        r"\[SYSTEM\]",
        r"END\s+OF\s+(SYSTEM|PROMPT|INSTRUCTIONS?)",
        r"BEGIN\s+NEW\s+(PROMPT|INSTRUCTIONS?|SYSTEM)",
        r"\x00",                     # null byte
        r"[​‌‍﻿]{2,}",  # zero-width/BOM (need >=2 to allow incidental)
    ],
)

_RULE_CHATML_MARKER = (
    "CHATML_MARKER",
    InjectionRisk.HIGH,
    [
        r"<\|im_start\|>",
        r"<\|im_end\|>",
        r"<\|endoftext\|>",
        r"<\|begin_of_text\|>",
        r"<\|start_header_id\|>",
        r"<\|end_header_id\|>",
    ],
)

_RULE_ENCODING_EVASION = (
    "ENCODING_EVASION",
    InjectionRisk.MEDIUM,
    [
        # Long suspicious base64 — restrict to standalone runs to avoid
        # false-positives on legitimate code blobs (which are normally
        # split by newlines / whitespace within the first 60 chars).
        r"(?:^|[^A-Za-z0-9+/=])[A-Za-z0-9+/]{60,}={0,2}(?:[^A-Za-z0-9+/=]|$)",
        # Excessive Unicode \uXXXX escapes (5+ hits)
        r"(\\u[0-9a-fA-F]{4}.*?){5,}",
        # Hex escapes (4+ hits)
        r"(\\x[0-9a-fA-F]{2}.*?){4,}",
        # Reference to common decoder functions
        r"\b(rot13|base64_decode|atob|btoa|fromcharcode)\b",
        # Cyrillic homoglyphs of Latin letters (а, е, о, р, с, у, х)
        r"[аеорсух]{3,}",
    ],
)

_RULE_MULTILINGUAL = (
    "MULTILINGUAL",
    InjectionRisk.MEDIUM,
    [
        # 中文:忽略 X 指令 / 系统提示 / 新指令
        r"忽略[一-鿿\s]{0,8}(指令|说明|提示)",
        r"新的?指令",
        r"系统提示",
        # Russian:игнорировать / новые инструкции
        r"игнорируй",
        r"новые\s+инструкции",
        # Spanish: ignora las instrucciones / nuevas instrucciones
        r"ignora\s+(todas?\s+)?(las?\s+)?(instrucciones?\s+)?anteriores?",
        r"nuevas?\s+instrucciones?",
        # German
        r"ignoriere\s+(alle\s+)?(vorherigen?\s+)?anweisungen",
        r"neue\s+anweisungen",
        # French
        r"ignore[rz]?\s+(toutes?\s+)?(les?\s+)?instructions?\s+(pr[eé]c[eé]dentes?|ant[eé]rieures?)",
        r"nouvelles?\s+instructions?",
        # Japanese
        r"指示を無視",
        r"新しい指示",
    ],
)

_RULE_PATH_TRAVERSAL = (
    "PATH_TRAVERSAL",
    InjectionRisk.HIGH,
    [
        r"(?:\.\./){3,}",                         # 3+ levels of ../
        r"~/?\.ssh(/|$)",
        r"~/?\.aws(/|$)",
        r"~/?\.gnupg(/|$)",
        r"~/?\.config/[^\s]*credentials?",
        r"~/?\.hermes/(\.env|auth\.json|state\.db|wallet)",
        r"/etc/(passwd|shadow|sudoers)\b",
        r"/proc/self/environ",
        r"/root/\.[a-z]+",
        r"id_rsa(\.pub)?\b",
    ],
)

_RULE_DATA_EXFILTRATION = (
    "DATA_EXFILTRATION",
    InjectionRisk.HIGH,
    [
        r"send\s+(this|all|the\s+\w+)\s+to\s+https?://",
        r"POST\s+(it|this|the\s+\w+)\s+to\s+https?://",
        r"curl\s+(-X\s+)?(POST|PUT)\s+https?://",
        r"webhook\.site/",
        r"requestbin\.com/",
        r"ngrok\.io/",
        r"transfer\.sh/",
        r"upload\s+(it|this|the\s+\w+)\s+to\s+",
        r"\bfetch\(['\"]\s*https?://[^'\"]*\?\w+=",  # fetch with query exfil
    ],
)


_ALL_RULES: tuple[tuple[str, InjectionRisk, list[str]], ...] = (
    _RULE_INSTRUCTION_PATTERN,
    _RULE_AUTHORITY_CLAIM,
    _RULE_BOUNDARY_MANIPULATION,
    _RULE_CHATML_MARKER,
    _RULE_ENCODING_EVASION,
    _RULE_MULTILINGUAL,
    _RULE_PATH_TRAVERSAL,
    _RULE_DATA_EXFILTRATION,
)


# ─────────────────────────── Compiled rule registry ───────────────────────────


@dataclass(frozen=True)
class _CompiledRule:
    name: str
    severity: InjectionRisk
    patterns: tuple[Pattern[str], ...]


def _compile_all() -> dict[str, _CompiledRule]:
    out: dict[str, _CompiledRule] = {}
    for name, severity, patterns in _ALL_RULES:
        compiled = tuple(
            re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns
        )
        out[name] = _CompiledRule(
            name=name, severity=severity, patterns=compiled
        )
    return out


_COMPILED: dict[str, _CompiledRule] = _compile_all()


# ─────────────────────────── Defense ───────────────────────────


@dataclass
class InjectionDefense:
    """Stateless injection scanner + sanitizer.

    All public methods are pure (no I/O, no global mutation).
    Construction validates that disabled rule names are recognized.
    """

    rules_disabled: frozenset[str] = field(default_factory=frozenset)
    max_input_chars: int = 50_000
    block_threshold: InjectionRisk = InjectionRisk.HIGH

    def __post_init__(self) -> None:
        unknown = self.rules_disabled - frozenset(_COMPILED.keys())
        if unknown:
            raise ValueError(
                f"Unknown rule name(s): {sorted(unknown)}. "
                f"Valid: {sorted(_COMPILED.keys())}"
            )
        if self.max_input_chars < 1:
            raise ValueError("max_input_chars must be >= 1")
        if not isinstance(self.block_threshold, InjectionRisk):
            raise TypeError("block_threshold must be an InjectionRisk")

    # ─────────── public API ───────────

    def scan(self, text: str, trust: TrustLevel) -> InjectionScanResult:
        """Run all enabled rules, aggregate severity, build sanitized form.

        USER trust always returns ``risk=NONE`` (no rules applied) — but
        the sanitized form is still produced. This keeps the API
        symmetric while honouring the rule "trust the user".

        Tool results / external content always run the full scan
        regardless of trust level so that prompt-injection from
        web-fetched content is caught.
        """
        # Cap before scanning to bound regex cost on adversarial input
        truncated = len(text) > self.max_input_chars
        capped = text[: self.max_input_chars] if truncated else text

        if trust is TrustLevel.USER:
            return InjectionScanResult(
                risk=InjectionRisk.NONE,
                matched_rules=(),
                sanitized=self._wrap(capped, trust),
                raw_excerpt=None,
                truncated=truncated,
            )

        matched: list[str] = []
        severities: list[InjectionRisk] = []
        first_excerpt: Optional[str] = None

        for name, rule in _COMPILED.items():
            if name in self.rules_disabled:
                continue
            for pattern in rule.patterns:
                m = pattern.search(capped)
                if m is None:
                    continue
                matched.append(name)
                severities.append(rule.severity)
                if first_excerpt is None:
                    first_excerpt = self._excerpt(capped, m.start(), m.end())
                break  # one pattern hit per rule is enough

        risk = self._aggregate(severities)
        return InjectionScanResult(
            risk=risk,
            matched_rules=tuple(matched),
            sanitized=self._wrap(capped, trust),
            raw_excerpt=first_excerpt,
            truncated=truncated,
        )

    def sanitize_message(
        self,
        message: dict[str, Any],
        trust: TrustLevel,
    ) -> dict[str, Any]:
        """Return a copy of ``message`` with ``content`` wrapped in a
        trust-boundary marker.

        Non-string content (multimodal: image_url etc.) is left unchanged.
        Caller is responsible for re-checking ``role`` if needed.
        """
        out = dict(message)
        content = out.get("content")
        if isinstance(content, str):
            scan = self.scan(content, trust)
            out["content"] = scan.sanitized
        return out

    def should_block(self, result: InjectionScanResult) -> bool:
        """Return True if ``result.risk`` is at or above ``block_threshold``.

        USER-trust scans always have risk=NONE so they never block.
        """
        return result.risk.order >= self.block_threshold.order

    # ─────────── helpers ───────────

    @staticmethod
    def _aggregate(severities: list[InjectionRisk]) -> InjectionRisk:
        if not severities:
            return InjectionRisk.NONE
        peak = max(severities, key=lambda r: r.order)
        # Two or more HIGH rules co-occurring = coordinated attack → CRITICAL
        high_count = sum(1 for s in severities if s.order >= InjectionRisk.HIGH.order)
        if high_count >= 2:
            return InjectionRisk.CRITICAL
        return peak

    @staticmethod
    def _wrap(text: str, trust: TrustLevel) -> str:
        if trust is TrustLevel.USER:
            return text
        marker = trust.value
        return (
            f"<untrusted source=\"{marker}\">\n"
            f"{text}\n"
            f"</untrusted>"
        )

    @staticmethod
    def _excerpt(
        text: str, start: int, end: int, context: int = 30
    ) -> str:
        a = max(0, start - context)
        b = min(len(text), end + context)
        snippet = text[a:b].replace("\n", " ").replace("\r", " ")
        return snippet.strip()[:200]

    # ─────────── rule introspection (for tests / CLI) ───────────

    @staticmethod
    def all_rule_names() -> tuple[str, ...]:
        return tuple(_COMPILED.keys())

    @staticmethod
    def severity_of_rule(name: str) -> Optional[InjectionRisk]:
        rule = _COMPILED.get(name)
        return rule.severity if rule else None
