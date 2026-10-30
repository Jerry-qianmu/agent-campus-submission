"""草稿保存动作：schedule_save_draft（幂等，只写 DRAFT）。"""

from __future__ import annotations

from typing import Any

from ..tooling import ToolContext, required_schema, tool
from ..tools import ToolResult, schedule_save_draft
from ..util import parse_datetime


def _format(result: ToolResult) -> str:
    data = result.data
    draft = data["draft"]
    if data.get("deduped"):
        return f"幂等命中，复用已有草稿 #{draft['id']}（{draft['status']}）"
    if data.get("reused"):
        return f"复活已取消草稿 #{draft['id']}，状态回到 DRAFT"
    return f"已保存草稿 #{draft['id']}（{draft['status']}），未发布"


@tool(
    name="schedule_save_draft",
    description="保存发布排期草稿（幂等）。只写 DRAFT，不会发布，也不会改动用户的时间要求。",
    parameters=required_schema({"material_id": "string", "slot": "string"}),
    formatter=_format,
)
def schedule_save_draft_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.calls["schedule_save_draft"] += 1
    return schedule_save_draft(
        ctx.db,
        args["material_id"],
        parse_datetime(args["slot"]),
        ctx.rules_version,
        ctx.faults,
    )
