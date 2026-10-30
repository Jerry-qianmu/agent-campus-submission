"""LangGraph 编排：ReAct 循环。

    START → prepare → agent ⇄ act → finalize → END

· prepare  预设骨架：把自然语言请求解析成结构化意图（这一步不做业务判定）。
· agent    「模型」决策：看已有观测，选择一个工具，或给出终局。由 policy.py 实现。
· act      执行被选中的工具，把结果写成 observation 回灌给 agent。
· finalize 只做汇总，不再产生新的业务结论。

限步：工具调用次数达到 max_steps 时立即收尾，返回 step_limit_reached（明确的未完成状态），
不允许「再试一次」无限循环。

与上一版（固定七节点）的区别：那时「下一步走哪条边」是代码写死的条件分支；现在是策略每轮
重新读观测再决定，换一个 policy 实现（比如真实模型）就能换掉整体决策行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .clients import ChatError
from .llm import LLMClient, get_llm as build_llm
from .policy import Context, Policy, get_policy as build_policy
from .registry import ToolContext, execute_tool
from .rules import FIELD_LABELS, RULES_VERSION
from .store import JsonDatabase
from .tools import Faults, notify_push
from .util import parse_datetime

MAX_STEPS_DEFAULT = 10

BASE_STATUSES = (
    "completed",
    "needs_confirmation",
    "needs_input",
    "blocked",
    "not_found",
    "tool_error",
    "step_limit_reached",
)

NOTIFY_STATUSES = ("completed", "needs_confirmation", "needs_input", "blocked")


class AgentState(TypedDict, total=False):
    request: dict[str, Any]
    intent: dict[str, Any]
    observations: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    next: dict[str, Any]
    llm_usage: dict[str, int]
    result: dict[str, Any]
    trace: list[str]


@dataclass
class Deps:
    db: JsonDatabase
    llm: LLMClient
    policy: Policy
    now: datetime
    max_steps: int = MAX_STEPS_DEFAULT
    rules_version: str = RULES_VERSION
    tool_context: ToolContext | None = None

    def tools(self, request: dict[str, Any]) -> ToolContext:
        """一次运行共用同一个上下文：调用计数与「已读到的元数据」要跨轮累积。"""
        if self.tool_context is None:
            self.tool_context = ToolContext(
                db=self.db,
                now=self.now,
                faults=Faults.from_request(request),
                rules_version=self.rules_version,
            )
        return self.tool_context


def _step(state: AgentState, name: str, **updates: Any) -> dict[str, Any]:
    trace = list(state.get("trace") or []) + [name]
    return {**updates, "trace": trace}


# ---------------------------------------------------------------------------
# 预设骨架：意图解析
# ---------------------------------------------------------------------------
def _prepare(state: AgentState, deps: Deps) -> dict[str, Any]:
    intent = deps.llm.parse_intent(state["request"], deps.now)
    updates: dict[str, Any] = {"intent": intent.as_dict()}
    if not intent.material_id:
        updates["next"] = {
            "finish": {
                "status": "needs_input",
                "reason": "请求里没有素材ID",
                "missing_fields": ["material_id"],
            }
        }
    return _step(state, "prepare", **updates)


# ---------------------------------------------------------------------------
# ReAct：模型决策 → 执行 → 观测
# ---------------------------------------------------------------------------
def _agent(state: AgentState, deps: Deps) -> dict[str, Any]:
    observations = state.get("observations") or []

    if len(observations) >= deps.max_steps:
        return _step(
            state,
            "agent",
            next={
                "finish": {
                    "status": "step_limit_reached",
                    "reason": f"已达到工具调用上限 {deps.max_steps} 次，任务未完成",
                }
            },
        )

    try:
        decision = deps.policy.decide(
            Context(
                request=state["request"],
                intent=state["intent"],
                observations=observations,
                now=deps.now,
                max_steps=deps.max_steps,
            )
        )
    except ChatError as error:
        # 模型本身不可用（超时/限速/余额/鉴权）：如实报失败，不当成"没有结论"继续跑
        return _step(
            state,
            "agent",
            next={
                "finish": {
                    "status": "tool_error",
                    "reason": f"模型调用失败：{error}",
                    "model_error": error.as_dict(),
                }
            },
        )

    record = {
        "step": len(state.get("decisions") or []) + 1,
        "action": decision.tool or "finish",
        "args": dict(decision.args) if decision.tool else dict(decision.finish_payload or {}),
        "rationale": decision.rationale,
        "observation": None,
        "next": None,
    }
    usage = dict(state.get("llm_usage") or {})
    if decision.usage:
        for key, value in decision.usage.items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
    return _step(
        state,
        "agent",
        decisions=list(state.get("decisions") or []) + [record],
        next=decision.action_payload(),
        llm_usage=usage,
    )


def _act(state: AgentState, deps: Deps) -> dict[str, Any]:
    payload = state["next"]
    tool = payload["tool"]
    args = payload["args"]
    result, summary = execute_tool(tool, args, deps.tools(state["request"]))

    observation = {
        "step": len(state.get("observations") or []) + 1,
        "tool": tool,
        "args": args,
        "ok": result.ok,
        "code": result.code,
        "retryable": result.retryable,
        "summary": summary,
        "data": result.data,
    }
    decisions = list(state.get("decisions") or [])
    decisions[-1]["observation"] = {
        "ok": result.ok,
        "code": result.code,
        "retryable": result.retryable,
        "summary": summary,
    }
    return _step(
        state,
        f"act:{tool}",
        observations=list(state.get("observations") or []) + [observation],
        decisions=decisions,
    )


# ---------------------------------------------------------------------------
# 收尾：只汇总，不再判定
# ---------------------------------------------------------------------------
def _last_ok(observations: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    for observation in reversed(observations):
        if observation["tool"] == tool and observation["ok"]:
            return observation
    return None


def _last_any(observations: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    for observation in reversed(observations):
        if observation["tool"] == tool:
            return observation
    return None


def _finalize(state: AgentState, deps: Deps) -> dict[str, Any]:
    finish_payload = (state.get("next") or {}).get("finish") or {}
    status = finish_payload.get("status")
    if status not in BASE_STATUSES:
        status = "blocked"

    observations = state.get("observations") or []
    decisions = list(state.get("decisions") or [])
    for index, record in enumerate(decisions):
        following = decisions[index + 1] if index + 1 < len(decisions) else None
        record["next"] = following["action"] if following else None

    audit = (_last_ok(observations, "audit.check") or {}).get("data") or {}
    alternative = (_last_ok(observations, "slot.find_alternative") or {}).get("data") or {}
    save_observation = _last_any(observations, "schedule.save_draft")

    draft: dict[str, Any] = {"saved": False}
    suggested_slot = finish_payload.get("suggested_slot")
    if save_observation and save_observation["ok"]:
        saved = save_observation["data"]
        draft = {
            "saved": True,
            "draft_id": saved["draft"]["id"],
            "status": saved["draft"]["status"],
            "deduped": bool(saved.get("deduped")),
            "reused": bool(saved.get("reused")),
        }
        suggested_slot = suggested_slot or saved["draft"]["date"]

    pending = list(finish_payload.get("pending") or [])
    intent = state.get("intent") or {}
    material_id = intent.get("material_id")
    result: dict[str, Any] = {
        "status": status,
        "mode": f"{deps.policy.name}_react",
        "finished": status != "step_limit_reached",
        "completed": status in ("completed", "needs_confirmation"),
        "request_id": state["request"].get("request_id"),
        "material_id": material_id,
        "review_status": audit.get("conclusion")
        or ("INCOMPLETE" if status == "needs_input" else "UNKNOWN"),
        "risk_reason": audit.get("risk_reason"),
        "reason": finish_payload.get("reason"),
        "rule_hits": audit.get("risks") or [],
        "missing_fields": audit.get("missing_fields")
        or list(finish_payload.get("missing_fields") or []),
        "requested_slot": intent.get("desired_slot"),
        "suggested_slot": suggested_slot,
        "alternatives": alternative.get("alternatives") or [],
        "pending_confirmations": pending,
        "draft": draft,
        "notify": _notify(state, deps, status, draft, pending, suggested_slot, material_id),
        "published": False,
        "steps": len(decisions),
        "tool_calls": len(observations),
        "max_steps": deps.max_steps,
        "decisions": decisions,
        "llm_usage": state.get("llm_usage") or {},
        "message": _message(status, finish_payload, draft, pending, deps.max_steps, observations),
        "evidence": {
            "material_id": material_id,
            "rules_version": audit.get("rules_version") or deps.rules_version,
            "policy": deps.policy.name,
            "model": getattr(deps.policy, "model", None) or "rules",
            "audit_table": "material_review",
            "schedule_table": "publish_schedule",
        },
        "trace": list(state.get("trace") or []) + ["finalize"],
    }
    if finish_payload.get("model_error"):
        result["model_error"] = finish_payload["model_error"]
    return _step(state, "finalize", result=result)


def _notify(
    state: AgentState,
    deps: Deps,
    status: str,
    draft: dict[str, Any],
    pending: list[str],
    suggested_slot: str | None,
    material_id: str | None,
) -> dict[str, Any]:
    """提醒由框架收尾时触发一次，不作为模型可选动作（模型漏调就会漏发通知）。"""
    if status not in NOTIFY_STATUSES or not material_id:
        return {}
    event = {
        "completed": "draft_saved",
        "needs_confirmation": "confirm_needed",
        "needs_input": "review_incomplete",
        "blocked": "review_blocked",
    }[status]
    dedupe_key = f"{material_id}:{event}:{suggested_slot or '-'}"
    try:
        pushed = notify_push(
            deps.db,
            material_id,
            event,
            {"status": status, "draft": draft, "pending": pending},
            dedupe_key,
            Faults.from_request(state["request"]),
        )
    except Exception:  # pragma: no cover - 提醒是旁路，绝不影响主流程
        return {"ok": False, "event": event, "degraded": True}
    if not pushed.ok:
        return {"ok": False, "event": event, "degraded": True, "code": pushed.code}
    return {
        "ok": True,
        "event": event,
        "dedupe_key": dedupe_key,
        "deduped": bool(pushed.data.get("deduped")),
        "degraded": False,
    }


def _message(
    status: str,
    finish_payload: dict[str, Any],
    draft: dict[str, Any],
    pending: list[str],
    max_steps: int,
    observations: list[dict[str, Any]],
) -> str:
    if status == "completed":
        return f"已保存草稿（草稿ID {draft.get('draft_id')}，状态 DRAFT），未发布。"
    if status == "needs_confirmation":
        return (
            f"已保存草稿（草稿ID {draft.get('draft_id')}），但有待确认项："
            + "；".join(pending)
            + "。未发布。"
        )
    if status == "needs_input":
        missing = "、".join(
            FIELD_LABELS.get(name, name) for name in (finish_payload.get("missing_fields") or [])
        )
        if not missing:
            missing = "必填信息"
        return f"素材缺少必填信息：{missing}。已阻断，未保存草稿。"
    if status == "blocked":
        return f"已阻断：{finish_payload.get('reason') or '未通过审核'}。未保存草稿。"
    if status == "not_found":
        return f"素材不存在，请确认素材 ID：{finish_payload.get('reason') or ''}。"
    if status == "tool_error":
        failed = [item for item in observations if not item["ok"]]
        code = failed[-1]["code"] if failed else "UNKNOWN"
        retried = "，已重试 1 次" if any(item.get("retryable") for item in failed) else ""
        return f"调用 {code} 失败{retried}：{finish_payload.get('reason')}。未保存任何数据。"
    if status == "step_limit_reached":
        return (
            f"达到工具调用上限 {max_steps} 次仍未得出结论，本次未完成；"
            "请缩小请求范围或人工介入。未保存草稿。"
        )
    return "未知状态"


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
def _route_after_agent(state: AgentState) -> str:
    return "finalize" if "finish" in (state.get("next") or {}) else "act"


def build_graph(deps: Deps):
    graph = StateGraph(AgentState)
    graph.add_node("prepare", lambda state: _prepare(state, deps))
    graph.add_node("agent", lambda state: _agent(state, deps))
    graph.add_node("act", lambda state: _act(state, deps))
    graph.add_node("finalize", lambda state: _finalize(state, deps))

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare", _route_after_agent, {"act": "agent", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "agent", _route_after_agent, {"act": "act", "finalize": "finalize"}
    )
    graph.add_edge("act", "agent")
    graph.add_edge("finalize", END)
    return graph.compile()


def run_request(
    request: dict[str, Any],
    db: JsonDatabase,
    now: datetime | str | None = None,
    llm: LLMClient | None = None,
    policy: Policy | None = None,
    max_steps: int | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """跑一次完整 ReAct 流程。

    provider/model 只在未显式传 policy/llm 时生效：
    mock（默认）用规则策略，deepseek 用真实模型客户端。
    """
    resolved_now = parse_datetime(now) if now is not None else parse_datetime(datetime.now())

    if max_steps is None:
        requested = request.get("max_steps")
        max_steps = (
            requested if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0
            else MAX_STEPS_DEFAULT
        )

    if policy is None:
        policy = build_policy(provider, model)
    if llm is None:
        llm = build_llm(provider, model)

    deps = Deps(
        db=db,
        llm=llm,
        policy=policy,
        now=resolved_now,
        max_steps=max_steps,
    )
    app = build_graph(deps)
    final = app.invoke(
        {"request": request, "trace": [], "observations": [], "decisions": []}
    )
    return final["result"]
