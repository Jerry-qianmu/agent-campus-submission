"""时段查询动作：slot_query / slot_find_alternative。"""

from __future__ import annotations

from typing import Any

from ..tooling import ToolContext, required_schema, tool
from ..tools import ToolResult, find_alternatives, ok_result, slot_query
from ..util import parse_datetime, to_iso


def _format_query(result: ToolResult) -> str:
    data = result.data
    occupied = [item for item in data["slots"] if item["state"] == "occupied"]
    free = data["free"]
    head = "、".join(f"{item['slot'][11:16]}({item['occupied_by']})" for item in occupied) or "无占用"
    earliest = free[0][11:16] if free else "无"
    return f"占用：{head}；可用 {len(free)} 个时段，最早 {earliest}"


def _format_alternative(result: ToolResult) -> str:
    data = result.data
    alternatives = [slot[11:16] for slot in data["alternatives"]]
    joined = "、".join(alternatives) if alternatives else "无可用"
    return f"替代时段：{joined}（原档占用方 {data.get('occupied_by') or '窗口外'}）"


@tool(
    name="slot_query",
    description="查询某一天各时段的占用情况，返回 free / occupied / closed 列表。",
    parameters=required_schema({"date": "string", "material_id": "string"}),
    formatter=_format_query,
)
def slot_query_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.calls["slot_query"] += 1
    raw = str(args["date"]).strip()
    # 模型可能只给日期，也可能给完整 ISO；两种都接受
    moment = parse_datetime(raw if "T" in raw else f"{raw}T00:00:00+08:00")
    return slot_query(
        ctx.db, moment, ctx.now, ctx.faults, exclude_material=args.get("material_id")
    )


@tool(
    name="slot_find_alternative",
    description="目标时段不可用时，按「先顺延再提前」找最近的可用时段。",
    parameters=required_schema({"slot": "string", "material_id": "string"}),
    formatter=_format_alternative,
)
def slot_find_alternative_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.calls["slot_find_alternative"] += 1
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
