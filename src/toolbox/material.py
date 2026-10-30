"""素材读取动作：material_get。"""

from __future__ import annotations

from typing import Any

from ..tooling import ToolContext, required_schema, tool
from ..tools import ToolResult, material_get


def _format(result: ToolResult) -> str:
    review = result.data.get("review") or {}
    return (
        f"素材 {result.data['material']['material_id']} 存在，"
        f"最近审核状态 {review.get('status') or '未审核'}"
    )


@tool(
    name="material_get",
    description="按素材ID读取元数据与最近一次审核结论。第一步通常调用它确认素材存在。",
    parameters=required_schema({"material_id": "string"}),
    formatter=_format,
)
def material_get_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    material_id = args["material_id"]
    first = ctx.calls["material_get"] == 0
    ctx.calls["material_get"] += 1
    degrade = ctx.faults.metadata_lag_once and first
    result = material_get(ctx.db, material_id, ctx.faults, degrade=degrade)
    if result.ok:
        # 记下这次读到的快照：后续审核要审「模型看到的那份数据」，而不是再从库里读一遍。
        ctx.observed_metadata[material_id] = dict(result.data["material"]["metadata"])
    return result
