"""工具注册表：把「模型可以选的动作」集中声明成带 JSON Schema 的工具。

这一层存在的意义
    ReAct 的关键是「模型从候选动作里选一个」。候选动作必须是一份模型可读的清单，
    而不是散落在 if/else 里的函数调用。所以这里声明 name / description / 入参 schema，
    执行时统一走 execute_tool，并产出一句人类可读的 observation 摘要。

七个动作
    material.get / audit.check / slot.query / slot.find_alternative
    / schedule.save_draft / schedule.save_draft(重试同一动作) / （finish 由策略层直接返回）
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .repository import Repository
from .store import JsonDatabase
from .tools import (
    Faults,
    ToolResult,
    audit_check,
    fail_result,
    find_alternatives,
    material_get,
    ok_result,
    schedule_save_draft,
    slot_query,
)
from .util import parse_datetime, to_iso

# 模型可选动作的完整清单（finish 不是 DB 动作，由策略返回 Decision.finish）
TOOL_NAMES = (
    "material.get",
    "audit.check",
    "slot.query",
    "slot.find_alternative",
    "schedule.save_draft",
)


@dataclass
class ToolContext:
    """一次运行内的工具上下文：数据库、时钟、故障开关、调用计数、已观测到的元数据。"""

    db: JsonDatabase
    now: datetime
    faults: Faults
    rules_version: str
    calls: Counter = field(default_factory=Counter)
    observed_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], ToolResult]

    def as_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _required(props: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: {"type": kind} for key, kind in props.items()},
        "required": list(props),
    }


def _material_get(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    material_id = args["material_id"]
    first = ctx.calls["material.get"] == 0
    ctx.calls["material.get"] += 1
    degrade = ctx.faults.metadata_lag_once and first
    result = material_get(ctx.db, material_id, ctx.faults, degrade=degrade)
    if result.ok:
        # 记下这次读到的快照：后续审核要审「模型看到的那份数据」，而不是再从库里读一遍。
        ctx.observed_metadata[material_id] = dict(result.data["material"]["metadata"])
    return result


def _audit_check(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    material_id = args["material_id"]
    ctx.calls["audit.check"] += 1
    metadata = ctx.observed_metadata.get(material_id)
    if metadata is None:
        with ctx.db.transaction() as session:
            material = Repository(session).get_material(material_id)
        if material is None:
            return fail_result("NOT_FOUND", f"素材 {material_id} 不存在")
        metadata = dict(material.get("metadata") or {})
    return audit_check(ctx.db, material_id, metadata, ctx.faults)


def _slot_query(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.calls["slot.query"] += 1
    moment = parse_datetime(args["date"] + "T00:00:00+08:00")
    return slot_query(ctx.db, moment, ctx.now, ctx.faults, exclude_material=args.get("material_id"))


def _slot_find_alternative(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.calls["slot.find_alternative"] += 1
    desired = parse_datetime(args["slot"])
    result = find_alternatives(
        ctx.db, desired, ctx.now, exclude_material=args.get("material_id")
    )
    if not result.ok:
        return result
    target = to_iso(desired)
    occupied_by = None
    for item in result.data["slots"]:
        if item["slot"] == target:
            occupied_by = item["occupied_by"]
    return ok_result(
        alternatives=result.data["alternatives"],
        occupied_by=occupied_by,
        free=result.data["free"],
    )


def _save_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.calls["schedule.save_draft"] += 1
    return schedule_save_draft(
        ctx.db,
        args["material_id"],
        parse_datetime(args["slot"]),
        ctx.rules_version,
        ctx.faults,
    )


TOOL_SPECS: dict[str, ToolSpec] = {
    "material.get": ToolSpec(
        name="material.get",
        description="按素材ID读取元数据与最近一次审核结论。第一步通常调用它确认素材存在。",
        parameters=_required({"material_id": "string"}),
        handler=_material_get,
    ),
    "audit.check": ToolSpec(
        name="audit.check",
        description="对素材跑审核规则，返回 PASSED / INCOMPLETE / RISK 与风险明细。",
        parameters=_required({"material_id": "string"}),
        handler=_audit_check,
    ),
    "slot.query": ToolSpec(
        name="slot.query",
        description="查询某一天各时段的占用情况，返回 free / occupied / closed 列表。",
        parameters=_required({"date": "string", "material_id": "string"}),
        handler=_slot_query,
    ),
    "slot.find_alternative": ToolSpec(
        name="slot.find_alternative",
        description="目标时段不可用时，按「先顺延再提前」找最近的可用时段。",
        parameters=_required({"slot": "string", "material_id": "string"}),
        handler=_slot_find_alternative,
    ),
    "schedule.save_draft": ToolSpec(
        name="schedule.save_draft",
        description="保存发布排期草稿（幂等）。只写 DRAFT，不会发布，也不会改动用户的时间要求。",
        parameters=_required({"material_id": "string", "slot": "string"}),
        handler=_save_draft,
    ),
}

FINISH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "结束本次任务并给出结论。业务上无法继续时也调用它，而不是编造结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "completed",
                        "needs_confirmation",
                        "needs_input",
                        "blocked",
                        "not_found",
                        "tool_error",
                    ],
                },
                "reason": {"type": "string"},
                "suggested_slot": {"type": "string"},
                "pending": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["status"],
        },
    },
}


def tool_schemas() -> list[dict[str, Any]]:
    """给真实模型用的 tools 参数（含 finish）。"""
    return [spec.as_schema() for spec in TOOL_SPECS.values()] + [FINISH_SCHEMA]


def summarize(name: str, result: ToolResult) -> str:
    """把工具结果压成一句 observation 摘要，供决策日志与模型下一步使用。"""
    if not result.ok:
        tail = "（可重试）" if result.retryable else ""
        return f"{name} 失败：{result.code} {result.message}{tail}"

    data = result.data
    if name == "material.get":
        review = data.get("review") or {}
        return f"素材 {data['material']['material_id']} 存在，最近审核状态 {review.get('status') or '未审核'}"
    if name == "audit.check":
        if data["conclusion"] == "INCOMPLETE":
            return f"审核结论 INCOMPLETE，缺少 {', '.join(data['missing_fields'])}"
        if data["conclusion"] == "RISK":
            return f"审核结论 RISK：{data['risk_reason']}"
        detail = "无风险" if not data["risks"] else f"{len(data['risks'])} 条中低风险"
        return f"审核结论 PASSED，{detail}"
    if name == "slot.query":
        occupied = [item for item in data["slots"] if item["state"] == "occupied"]
        free = data["free"]
        head = "、".join(f"{item['slot'][11:16]}({item['occupied_by']})" for item in occupied) or "无占用"
        return f"占用：{head}；可用 {len(free)} 个时段，最早 {free[0][11:16] if free else '无'}"
    if name == "slot.find_alternative":
        alternatives = [slot[11:16] for slot in data["alternatives"]]
        return f"替代时段：{'、'.join(alternatives) if alternatives else '无可用'}（原档占用方 {data.get('occupied_by') or '窗口外'}）"
    if name == "schedule.save_draft":
        draft = data["draft"]
        if data.get("deduped"):
            return f"幂等命中，复用已有草稿 #{draft['id']}（{draft['status']}）"
        if data.get("reused"):
            return f"复活已取消草稿 #{draft['id']}，状态回到 DRAFT"
        return f"已保存草稿 #{draft['id']}（{draft['status']}），未发布"
    return name


def execute_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> tuple[ToolResult, str]:
    """执行模型选择的动作，返回（结果, 摘要）。未注册的动作按参数错误处理。"""
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return fail_result("INVALID_ARGUMENT", f"未知动作：{name}"), f"未知动作：{name}"
    missing = [key for key in spec.parameters["required"] if key not in args]
    if missing:
        return (
            fail_result("INVALID_ARGUMENT", f"{name} 缺少参数：{', '.join(missing)}"),
            f"{name} 参数不足：{', '.join(missing)}",
        )
    result = spec.handler(ctx, args)
    return result, summarize(name, result)
