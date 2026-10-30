"""规则审核动作：audit_check（含内容指纹去重）。"""

from __future__ import annotations

from typing import Any

from ..repository import Repository
from ..tooling import ToolContext, required_schema, tool
from ..tools import ToolResult, audit_check, duplicate_materials, fail_result
from ..util import content_fingerprint


def _format(result: ToolResult) -> str:
    data = result.data
    if data["conclusion"] == "INCOMPLETE":
        return f"审核结论 INCOMPLETE，缺少 {', '.join(data['missing_fields'])}"
    if data["conclusion"] == "RISK":
        return f"审核结论 RISK：{data['risk_reason']}"
    detail = "无风险" if not data["risks"] else f"{len(data['risks'])} 条中低风险"
    return f"审核结论 PASSED，{detail}"


@tool(
    name="audit_check",
    description="对素材跑审核规则，返回 PASSED / INCOMPLETE / RISK 与风险明细。",
    parameters=required_schema({"material_id": "string"}),
    formatter=_format,
)
def audit_check_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    material_id = args["material_id"]
    ctx.calls["audit_check"] += 1
    # 审「模型看到的那份快照」；没有快照才回库读
    metadata = ctx.observed_metadata.get(material_id)
    if metadata is None:
        with ctx.db.transaction() as session:
            material = Repository(session).get_material(material_id)
        if material is None:
            return fail_result("NOT_FOUND", f"素材 {material_id} 不存在")
        metadata = dict(material.get("metadata") or {})
    duplicates = duplicate_materials(ctx.db, material_id, metadata)
    result = audit_check(
        ctx.db, material_id, metadata, ctx.faults, duplicate_of=duplicates
    )
    if result.ok:
        result.data["content_fingerprint"] = content_fingerprint(metadata)
    return result
