#!/usr/bin/env python3
"""
Smart Router — DAG-driven model routing for AI Agents.
Standalone version extracted from Hermes Agent plugin.

Usage:
    python smart-router.py

    This is a reference implementation. Integrate into your own
    AI Agent by calling classify_and_route(message, session_id)
    before each LLM call.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Tier definitions ─────────────────────────────────────────
TIER_ORDER = ("c0", "c1", "c2", "c3")
DEFAULT_TIER = "c1"

# ── Cost estimates ($/1M tokens) ─────────────────────────────
COST_ESTIMATES: Dict[str, float] = {
    "deepseek-v4-flash": 0.0,     # free
    "deepseek-v4-pro": 0.15,      # low-cost
    "qwen3.7-plus": 0.50,         # moderate
    "kimi-k2.6": 1.20,            # high-end
}

# ── Default tier config ──────────────────────────────────────
DEFAULT_TIERS = {
    "c0": {"provider": "opencode-go", "model": "deepseek-v4-flash"},
    "c1": {"provider": "opencode-go", "model": "deepseek-v4-pro"},
    "c2": {"provider": "opencode-go", "model": "qwen3.7-plus"},
    "c3": {"provider": "opencode-go", "model": "kimi-k2.6"},
}

# ── Reasoning keywords (CJK + English) ───────────────────────
REASONING_KEYWORDS = [
    "分析", "设计", "架构", "审查", "审计", "安全", "决策",
    "对比", "评估", "方案", "策略", "规划", "优化", "重构",
    "调试", "诊断", "排查", "根因", "原理", "推导",
    "analyze", "design", "architecture", "review", "audit", "security",
    "decision", "compare", "evaluate", "strategy", "plan", "optimize",
    "refactor", "debug", "diagnose", "root cause",
]

SIMPLE_KEYWORDS = [
    "你好", "hi", "hello", "谢谢", "thanks", "好的", "ok",
    "继续", "continue", "接着", "下一个", "嗯", "对",
]


class SmartRouter:
    """DAG-driven model router for AI Agent LLM calls."""

    def __init__(self, tiers: Optional[Dict] = None,
                 hold_turns: int = 5,
                 fallback_order: Optional[List[str]] = None):
        self.tiers = tiers or DEFAULT_TIERS
        self.hold_turns = hold_turns
        self.fallback_order = fallback_order or ["c3", "c2", "c1", "c0"]
        self._session_state: Dict[str, dict] = {}

    # ── Signal extraction ──────────────────────────────────

    def _signal_message_length(self, msg: str) -> int:
        return len(msg)

    def _signal_tool_count(self, msg: str) -> int:
        patterns = [r"run\s+\w+", r"exec(?:ute)?\s+",
                     r"查(?:看|询|找|一下)", r"读(?:取|文件)",
                     r"写(?:入|文件)", r"修(?:改|复|建)",
                     r"creat(?:e|ing)", r"deploy", r"git\s+"]
        count = sum(len(re.findall(p, msg, re.IGNORECASE))
                    for p in patterns)
        return min(count, 10)

    def _signal_has_code(self, msg: str) -> bool:
        if re.search(r"```[\w]*\n", msg):
            return True
        if re.search(r"`[^`]{3,}`", msg):
            return True
        code_patterns = [
            r"import\s+\w+", r"def\s+\w+\s*\(", r"class\s+\w+",
            r"function\s+\w+", r"const\s+\w+\s*=", r"let\s+\w+\s*=",
            r"print\s*\(", r"return\s+", r"npm\s+", r"pip\s+", r"git\s+",
        ]
        for p in code_patterns:
            if re.search(p, msg):
                return True
        code_request = [
            r"(写|编|创建|实现|开发)\s*(一?个|一?段|一?份)?\s*(Python|脚本|程序|函数|类|模块|API|接口|工具|代码)",
            r"(编写|编码|编程|重构|refactor|迁移)",
        ]
        for p in code_request:
            if re.search(p, msg):
                return True
        return False

    def _signal_reasoning_depth(self, msg: str) -> int:
        msg_lower = msg.lower()
        score = 0
        for kw in REASONING_KEYWORDS:
            if kw in msg_lower:
                score += 1
        if re.search(r"\b(why|how|what if|比较|区别|差异)\b", msg_lower):
            score += 1
        if len(msg) > 500:
            score += 1
        if len(msg) > 2000:
            score += 2
        question_count = msg.count("?") + msg.count("？")
        if question_count > 3:
            score += 1
        return min(score, 2)

    def _signal_is_simple_chat(self, msg: str) -> bool:
        m = msg.lower().strip()
        for kw in SIMPLE_KEYWORDS:
            if m == kw or m.startswith(kw):
                return True
        return len(msg) < 10

    def _extract_signals(self, msg: str) -> Dict:
        return {
            "message_length": self._signal_message_length(msg),
            "tool_count": self._signal_tool_count(msg),
            "has_code": self._signal_has_code(msg),
            "reasoning_depth": self._signal_reasoning_depth(msg),
            "is_simple_chat": self._signal_is_simple_chat(msg),
        }

    # ── Routing logic ──────────────────────────────────────

    def _classify_turn(self, msg: str, session_id: str) -> Tuple[str, Dict]:
        signals = self._extract_signals(msg)
        state = self._session_state.get(session_id, {})
        hold_remaining = state.get("hold_remaining", 0)
        last_tier = state.get("last_tier")
        last_error = state.get("last_error", False)

        # Hold check
        if hold_remaining > 0 and last_tier and not last_error:
            self._session_state[session_id]["hold_remaining"] = hold_remaining - 1
            return last_tier, signals

        # Error recovery → escalate
        if last_error:
            return ("c3" if signals["reasoning_depth"] >= 1 else "c2"), signals

        # Reasoning-driven routing
        if signals["reasoning_depth"] >= 2:
            return "c3", signals
        if signals["reasoning_depth"] >= 1:
            return "c2", signals

        # Code requests
        if signals["has_code"]:
            return "c2", signals

        # Simple chat → free tier
        if signals["is_simple_chat"] or (
            signals["message_length"] < 200 and signals["tool_count"] <= 1
        ):
            return "c0", signals

        # Default
        if signals["message_length"] < 1000 and signals["tool_count"] <= 3:
            return "c1", signals

        return "c1", signals

    def _resolve_fallback(self, tier: str, error_type: str) -> Optional[Dict]:
        fallback_triggers = {"timeout", "rate_limit", "server_error", "quota_exceeded"}
        if error_type not in fallback_triggers:
            return None

        current_idx = self.fallback_order.index(tier) if tier in self.fallback_order else 0
        for idx in range(current_idx + 1, len(self.fallback_order)):
            ft = self.fallback_order[idx]
            config = self.tiers.get(ft)
            if config:
                return {
                    "tier": ft,
                    "model": config["model"],
                    "provider": config["provider"],
                    "fallback_reason": f"{tier}→{ft} ({error_type})",
                }
        return None

    def _apply_hold(self, tier: str, session_id: str) -> None:
        if tier in ("c2", "c3"):
            turns = self.hold_turns + (2 if tier == "c3" else 0)
            if session_id not in self._session_state:
                self._session_state[session_id] = {}
            self._session_state[session_id]["hold_remaining"] = turns
            self._session_state[session_id]["hold_set_at"] = time.time()

    # ── Main entry point ───────────────────────────────────

    def classify_and_route(self, message: str, session_id: str
                            ) -> Tuple[str, str, Dict]:
        """Classify message and return (provider, model, routing_info)."""
        if session_id not in self._session_state:
            self._session_state[session_id] = {
                "last_tier": DEFAULT_TIER, "hold_remaining": 0,
                "last_error": False, "total_routed": 0,
                "tier_counts": {t: 0 for t in TIER_ORDER},
            }

        tier, signals = self._classify_turn(message, session_id)
        config = self.tiers.get(tier, self.tiers[DEFAULT_TIER])
        self._apply_hold(tier, session_id)

        state = self._session_state[session_id]
        state["last_tier"] = tier
        state["last_error"] = False
        state["total_routed"] += 1
        state["tier_counts"][tier] = state["tier_counts"].get(tier, 0) + 1

        routing_info = {
            "tier": tier.upper(),
            "model": config["model"],
            "provider": config["provider"],
            "signals": signals,
            "session_id": session_id[:12],
            "turn": state["total_routed"],
        }

        print(f"[smart-router] 🧭 {tier.upper()} → "
              f"{config['provider']}/{config['model']} "
              f"(len:{signals['message_length']}, "
              f"code:{signals['has_code']}, "
              f"reason:{signals['reasoning_depth']})")

        return config["provider"], config["model"], routing_info

    def report_error(self, session_id: str, error_type: str) -> Optional[Dict]:
        """Call after a failed LLM call. Returns fallback config if available."""
        if session_id not in self._session_state:
            return None

        tier = self._session_state[session_id].get("last_tier", DEFAULT_TIER)
        self._session_state[session_id]["last_error"] = True

        fallback = self._resolve_fallback(tier, error_type)
        if fallback:
            print(f"[smart-router] ⚠️  Fallback: {fallback['fallback_reason']}")
            self._session_state[session_id]["last_tier"] = fallback["tier"]
        return fallback

    def get_stats(self, session_id: str) -> Dict:
        """Get routing statistics for a session."""
        state = self._session_state.get(session_id, {})
        return {
            "total_routed": state.get("total_routed", 0),
            "tier_counts": state.get("tier_counts", {}),
            "hold_remaining": state.get("hold_remaining", 0),
        }


def main():
    """Demo: simulate a conversation with the smart router."""
    router = SmartRouter()
    session = "demo-session-001"

    messages = [
        "你好",                                         # simple chat
        "帮我写一个 Python 脚本来分析 CSV 数据",         # code request
        "好的，谢谢",                                    # acknowledgment
        "继续，再加一个可视化功能",                      # code continuation
        "分析一下这个架构方案的安全性",                   # deep reasoning
        "对比一下 K8s 和 Nomad 的优劣",                  # evaluation
        "嗯，明白了",                                    # simple
        "设计一个微服务网关的架构",                       # architecture design
    ]

    print("=" * 60)
    print("Smart Router Demo — DAG-driven Model Routing")
    print("=" * 60)

    for msg in messages:
        provider, model, info = router.classify_and_route(msg, session)
        print(f"   → {provider}/{model}")
        print()

    print("─" * 40)
    print("Session Statistics:")
    stats = router.get_stats(session)
    print(f"   Total turns: {stats['total_routed']}")
    print(f"   Tier distribution: {stats['tier_counts']}")

    # Simulate a fallback
    print()
    print("📡 Simulating LLM timeout...")
    fallback = router.report_error(session, "timeout")
    if fallback:
        print(f"   Fallback activated: {fallback['fallback_reason']}")

    print()
    print("✅ Demo complete!")


if __name__ == "__main__":
    main()
