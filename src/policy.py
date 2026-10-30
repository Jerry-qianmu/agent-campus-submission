"""策略层：ReAct 循环里「模型」的那一半——看观测、选动作、决定何时结束。

为什么单独一层
    循环（graph.py）负责执行与限步，策略负责决策。两者分离后：
      · 换模型不动循环；
      · 离线 mock 策略与真实模型策略实现同一个 Protocol，验收可以只换一个实现；
      · 决策依据（rationale）与动作一起落进日志，便于评审复核"为什么这一步这么走"。

两个实现
    MockPolicy  默认，离线规则决策。确定性、可单测；只证明流程，不证明模型决策质量。
    LLMPolicy   真实模型路径：把工具清单交给模型，读回 tool_calls。本仓库未端到端联调（无密钥）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from .clients import ChatClient, ChatResult, build_client
from .registry import tool_schemas
from .util import parse_datetime, to_iso

# 允许的最终状态；step_limit_reached 由循环注入，不属于策略可选值
FINISH_STATUSES = (
    "completed",
    "needs_confirmation",
    "needs_input",
    "blocked",
    "not_found",
    "tool_error",
)


@dataclass(frozen=True)
class Decision:
    """一次「想」的结果：要么选一个工具，要么给出终局。"""

    rationale: str
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    finish_payload: dict[str, Any] | None = None
    usage: dict[str, int] | None = None

    @property
    def is_finish(self) -> bool:
        return self.finish_payload is not None

    def action_payload(self) -> dict[str, Any]:
        if self.is_finish:
            return {"finish": dict(self.finish_payload or {})}
        return {"tool": self.tool, "args": dict(self.args)}


def action(tool: str, args: dict[str, Any], rationale: str) -> Decision:
    return Decision(rationale=rationale, tool=tool, args=args)


def finish(
    status: str,
    rationale: str,
    reason: str | None = None,
    pending: list[str] | None = None,
    suggested_slot: str | None = None,
) -> Decision:
    payload: dict[str, Any] = {"status": status, "reason": reason or rationale}
    if pending:
        payload["pending"] = list(pending)
    if suggested_slot:
        payload["suggested_slot"] = suggested_slot
    return Decision(rationale=rationale, finish_payload=payload)


@dataclass
class Context:
    """策略能看到的全部信息：请求、意图、已有观测、时钟、步数上限。"""

    request: dict[str, Any]
    intent: dict[str, Any]
    observations: list[dict[str, Any]]
    now: datetime
    max_steps: int

    def of(self, tool: str) -> list[dict[str, Any]]:
        return [item for item in self.observations if item["tool"] == tool]

    def last(self, tool: str) -> dict[str, Any] | None:
        found = self.of(tool)
        return found[-1] if found else None

    @property
    def material_id(self) -> str:
        return self.intent["material_id"]

    @property
    def desired_slot(self) -> str | None:
        return self.intent.get("desired_slot")

    @property
    def constraint(self) -> str:
        return self.intent.get("slot_constraint") or "soft"


class Policy(Protocol):
    name: str

    def decide(self, ctx: Context) -> Decision: ...


def _fmt(slot: str | None) -> str:
    if not slot:
        return "-"
    return parse_datetime(slot).strftime("%Y-%m-%d %H:%M")


def _hhmm(slot: str | None) -> str:
    return parse_datetime(slot).strftime("%H:%M") if slot else "-"


# ---------------------------------------------------------------------------
# 离线策略
# ---------------------------------------------------------------------------
class MockPolicy:
    """规则版决策。它读的是「观测里的值」，不是固定的第几步——工具换结果，动作就换。"""

    name = "mock"

    def decide(self, ctx: Context) -> Decision:
        material_id = ctx.material_id
        gets = ctx.of("material_get")

        if not gets:
            return action(
                "material_get", {"material_id": material_id}, "先确认素材是否存在、元数据是否可读"
            )

        latest_get = gets[-1]
        if not latest_get["ok"]:
            status = "not_found" if latest_get.get("code") == "NOT_FOUND" else "tool_error"
            return finish(
                status,
                "素材读不到，无法继续，如实告知而不是编造",
                reason=latest_get["summary"],
            )

        audits = ctx.of("audit_check")
        if not audits or latest_get["step"] > audits[-1]["step"]:
            # 审核结论比最近一次读取更旧，说明元数据刚刷新过，必须重审再判断。
            return action(
                "audit_check", {"material_id": material_id}, "素材已读到（或刚补查过），先跑规则审核"
            )

        latest_audit = audits[-1]
        if not latest_audit["ok"]:
            return finish(
                "tool_error", "审核工具不可用，不能放行", reason=latest_audit["summary"]
            )

        conclusion = latest_audit["data"]["conclusion"]
        if conclusion == "INCOMPLETE":
            if len(gets) == 1:
                return action(
                    "material_get",
                    {"material_id": material_id},
                    "审核缺字段，可能是上游元数据延迟，补查一次再判断",
                )
            missing = "、".join(latest_audit["data"]["missing_fields"])
            return finish(
                "needs_input",
                "补查后仍缺字段，需要用户补齐，不能替用户编默认值",
                reason=f"缺少 {missing}",
            )

        if conclusion == "RISK":
            return finish(
                "blocked",
                "命中高危规则，先整改再排期，不能带病上线",
                reason=latest_audit["data"]["risk_reason"],
            )

        slots = ctx.of("slot_query")
        if not slots:
            date = (ctx.desired_slot or to_iso(ctx.now))[:10]
            return action(
                "slot_query",
                {"date": date, "material_id": material_id},
                "审核通过，查目标日期的占用情况再决定档位",
            )

        latest_slot = slots[-1]
        if not latest_slot["ok"]:
            return finish(
                "tool_error", "时段查询失败，不能凭猜落草稿", reason=latest_slot["summary"]
            )

        free = latest_slot["data"]["free"]
        if ctx.desired_slot is None:
            if not free:
                return finish("blocked", "当天没有可用时段", reason="可用时段为空")
            return self._save(
                ctx, free[0], "用户没指定时间，取当天最早可用时段，并标记待确认"
            )

        if ctx.desired_slot in free:
            return self._save(ctx, ctx.desired_slot, "目标时段空闲，直接落草稿")

        alternatives = ctx.of("slot_find_alternative")
        if not alternatives:
            return action(
                "slot_find_alternative",
                {"slot": ctx.desired_slot, "material_id": material_id},
                f"{_hhmm(ctx.desired_slot)} 不可用，先找最近的替代时段再决定",
            )

        candidates = alternatives[-1]["data"].get("alternatives") or []
        if not candidates:
            return finish(
                "blocked",
                "目标时段不可用且当天没有替代时段",
                reason=f"{_fmt(ctx.desired_slot)} 不可用，无替代",
            )

        if ctx.constraint == "hard":
            return finish(
                "blocked",
                "用户明确要求该时间，硬性要求不得擅自改动，把选择权交回用户",
                reason=f"{_fmt(ctx.desired_slot)} 已被占用，硬性要求未改动",
                pending=[
                    f"{_fmt(ctx.desired_slot)} 已被占用；硬性要求未改动，"
                    f"可在 改期({_fmt(candidates[0])}) / 取消 / 换素材 中选择"
                ],
                suggested_slot=None,
            )

        return self._save(
            ctx,
            candidates[0],
            f"用户未锁定时间，顺延到最近可用档 {_hhmm(candidates[0])} 并标记待确认",
        )

    # -- 保存与收尾 ------------------------------------------------------
    def _save(self, ctx: Context, slot: str, rationale: str) -> Decision:
        saves = ctx.of("schedule_save_draft")
        if saves:
            latest = saves[-1]
            if latest["ok"]:
                return self._finish_saved(ctx, slot)
            if latest.get("code") == "ALREADY_SCHEDULED":
                existing = (latest.get("data") or {}).get("existing") or {}
                return finish(
                    "blocked",
                    "该素材已经有一条有效排期，不能重复创建；改期必须先取消原草稿",
                    reason=(
                        f"素材已排期在 {_fmt(existing.get('date'))}"
                        f"（草稿 #{existing.get('draft_id')}，{existing.get('status')}），未重复创建"
                    ),
                    pending=[
                        f"如需改期：先取消草稿 #{existing.get('draft_id')}（{_fmt(existing.get('date'))}）再重跑",
                        "如需保留原排期：忽略本次请求",
                    ],
                )
            if latest.get("retryable") and len(saves) == 1:
                return action(
                    "schedule_save_draft",
                    {"material_id": ctx.material_id, "slot": slot},
                    "保存失败但错误可重试，重试一次同一动作；不可无限重试",
                )
            return finish(
                "tool_error",
                "保存失败，必须明确报失败，不能声称已排期",
                reason=latest["summary"],
            )
        return action(
            "schedule_save_draft",
            {"material_id": ctx.material_id, "slot": slot},
            rationale,
        )

    def _finish_saved(self, ctx: Context, slot: str) -> Decision:
        pending: list[str] = []
        audit = (ctx.last("audit_check") or {}).get("data") or {}
        for risk in audit.get("risks") or []:
            if risk["level"] != "HIGH":
                pending.append(f"{risk['code']} {risk['message']}，需确认后发布")

        if ctx.desired_slot is None:
            # 用户没给时间，我们只是"建议"了一个档位，必须标待确认而不是当成用户授权
            pending.append(f"未指定发布时间，建议 {_fmt(slot)}，需你确认")
        elif slot != ctx.desired_slot:
            alternative = ctx.last("slot_find_alternative") or {}
            owner = (alternative.get("data") or {}).get("occupied_by") or "其他素材"
            pending.append(
                f"{_fmt(ctx.desired_slot)} 该时段已被素材 {owner} 占用，"
                f"建议改为 {_fmt(slot)}，需你确认"
            )

        if pending:
            return finish(
                "needs_confirmation",
                "草稿已落库但有需要人确认的点，逐条列出来而不是替用户拍板",
                pending=pending,
                suggested_slot=slot,
            )
        return finish(
            "completed",
            "审核通过且时段空闲，草稿已保存；仍未发布",
            suggested_slot=slot,
        )


# ---------------------------------------------------------------------------
# 真实模型策略
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是短视频素材的发布排期助手。你只能通过工具获取事实，禁止猜测。

规则：
1. 只保存草稿，永远不要说"已发布"。
2. 用户明确要求"必须/只能"某个时间时，若该时段被占用，不得自行改期，必须 finish(blocked) 并把选项交回用户。
3. 素材信息缺失时不要编造字段，finish(needs_input) 并说明缺什么。
4. 遇到可重试的工具错误最多重试一次，仍失败则 finish(tool_error)。
5. 每次只调用一个工具；信息足够时调用 finish 给出结论。
6. 决策理由写在 assistant 的 content 里，一句话即可，不要输出长篇推理。
7. **只有调用过 schedule_save_draft 且成功之后，才允许 finish(completed) 或 finish(needs_confirmation)。**
   没有落草稿就收尾时，只能 finish(blocked)，并说明"未保存草稿"。
8. 时段冲突且用户是软性要求时，不需要先问；直接存到最近可用时段，把调整写进 pending 让用户确认。
9. 若 audit_check 返回 INCOMPLETE，先再调用一次 material_get（可能是上游元数据延迟），然后重新 audit_check；
   仍然缺字段才 finish(needs_input)。
"""


def coerce_status(value: Any) -> str:
    """模型可能给出非法状态，收敛到允许集合，宁可 blocked 也不能谎报完成。"""
    return value if value in FINISH_STATUSES else "blocked"


class LLMPolicy:
    """把工具清单交给模型、读回 tool_calls。循环与 MockPolicy 完全共用，只换"谁在决定"。"""

    name = "llm"

    def __init__(self, client: ChatClient, system_prompt: str = SYSTEM_PROMPT) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.model = getattr(client, "model", "")
        self.name = getattr(client, "name", "llm")  # mode 会显示成 deepseek_react / openai_react

    def decide(self, ctx: Context) -> Decision:
        result = self.client.chat(self._messages(ctx), tools=tool_schemas())
        return self._decide(result)

    def _messages(self, ctx: Context) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "now": to_iso(ctx.now),
                        "request": ctx.request,
                        "intent": ctx.intent,
                        "used_tool_calls": len(ctx.observations),
                        "max_tool_calls": ctx.max_steps,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        for index, observation in enumerate(ctx.observations):
            call_id = f"call_{index}"
            messages.append(
                {
                    "role": "assistant",
                    "content": observation.get("rationale") or "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": observation["tool"],
                                "arguments": json.dumps(observation["args"], ensure_ascii=False),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        {
                            "ok": observation["ok"],
                            "summary": observation["summary"],
                            "data": observation.get("data"),
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        return messages

    def _decide(self, result: ChatResult) -> Decision:
        rationale = (result.content or "").strip()
        calls = result.tool_calls
        if not calls:
            # 模型没调工具：可能是它认为可以直接回答。按"没给出可执行结论"处理，不瞎猜。
            return finish(
                "blocked",
                rationale or "模型没有选择任何动作",
                reason=(result.content or "").strip()[:200] or "模型没有选择任何动作",
            )

        call = calls[0]
        name = call.get("name") or ""
        if not rationale:
            rationale = f"模型选择调用 {name}"
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            decision = finish(
                "tool_error", rationale, reason=f"动作参数不是合法 JSON：{name}"
            )
            return replace(decision, usage=result.usage or None)
        if not isinstance(args, dict):
            decision = finish("tool_error", rationale, reason="动作参数必须是 JSON 对象")
            return replace(decision, usage=result.usage or None)

        if name == "finish":
            decision = finish(
                coerce_status(args.get("status")),
                rationale,
                reason=args.get("reason"),
                pending=args.get("pending"),
                suggested_slot=args.get("suggested_slot"),
            )
        else:
            decision = action(name, args, rationale)
        return replace(decision, usage=result.usage or None)


def get_policy(provider: str | None = None, model: str | None = None) -> Policy:
    """按 provider 选择策略：mock 走规则，deepseek/http 走真实模型。"""
    resolved = (provider or os.getenv("LLM_PROVIDER") or "mock").strip().lower()
    if resolved in ("mock", "offline"):
        return MockPolicy()
    client = build_client(resolved, model)
    if client is None:  # pragma: no cover - build_client 不会在非 mock 时返回 None
        return MockPolicy()
    return LLMPolicy(client)
