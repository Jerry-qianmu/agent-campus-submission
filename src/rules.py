"""素材审核规则：纯函数，可单测、可回归，永远不参与「创作」只做判定。

规则与 ``docs/01-requirements.md`` 的规则表一一对应：
    R001 标题缺失(必填)        R005 命中敏感词
    R002 封面缺失(必填)        R006 标题超长
    R003 时长非法(必填)        R007 标签为空
    R004 版权来源缺失          R008 格式不在白名单
另有 R009「重复提交」，由 publish_schedule 的唯一键在存储层兑现，不在这里判。

为什么规则不交给模型
    规则结论要可解释、可回归、可写进审计。模型只负责把用户的话翻译成结构化意图，
    判定一律走这里，避免同一条素材两次跑出两个结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

RULES_VERSION = "r2026.11.1"

FIELD_LABELS = {
    "title": "标题",
    "cover_url": "封面",
    "file_url": "素材文件",
    "duration_sec": "时长",
    "format": "格式",
    "copyright": "版权来源",
    "tags": "标签",
}

SENSITIVE_TERMS = ("最低价", "全网最低", "稳赚不赔", "包治百病", "国家级")
FORMAT_WHITELIST = {"mp4", "mov", "m4v", "jpg", "jpeg", "png"}

MISSING = "MISSING"
INVALID = "INVALID"


@dataclass(frozen=True)
class Risk:
    code: str
    level: str  # HIGH / MEDIUM / LOW
    field: str
    message: str
    suggestion: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "level": self.level,
            "field": self.field,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class AuditReport:
    conclusion: str  # PASSED / INCOMPLETE / RISK
    missing_fields: list[str] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
    rules_version: str = RULES_VERSION

    @property
    def blocking_risks(self) -> list[Risk]:
        return [risk for risk in self.risks if risk.level == "HIGH"]

    @property
    def pending_risks(self) -> list[Risk]:
        return [risk for risk in self.risks if risk.level != "HIGH"]

    @property
    def risk_reason(self) -> str | None:
        if self.conclusion == "INCOMPLETE":
            labels = "、".join(FIELD_LABELS.get(name, name) for name in self.missing_fields)
            return f"缺少必填信息：{labels}"
        if self.conclusion == "RISK":
            first = self.blocking_risks[0]
            return f"{first.code} {first.message}"
        if self.pending_risks:
            return "；".join(f"{risk.code} {risk.message}" for risk in self.pending_risks)
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "missing_fields": list(self.missing_fields),
            "risks": [risk.as_dict() for risk in self.risks],
            "rules_version": self.rules_version,
            "risk_reason": self.risk_reason,
        }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _duration(metadata: Mapping[str, Any]) -> tuple[str, float | None]:
    value = metadata.get("duration_sec")
    if value is None or value == "":
        return MISSING, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return INVALID, None
    if value <= 0:
        return INVALID, None
    return "", float(value)


def audit(
    metadata: Mapping[str, Any] | None,
    duplicate_of: Sequence[str] | None = None,
) -> AuditReport:
    """对一份素材元数据做完整性校验与规则检查。

    duplicate_of：内容指纹相同的其它素材 ID（由工具层查库后传入，规则本身保持纯函数）。
    """
    md: Mapping[str, Any] = metadata if isinstance(metadata, Mapping) else {}
    missing: list[str] = []
    risks: list[Risk] = []

    # R001 标题
    title = _text(md.get("title"))
    if not title:
        missing.append("title")
    elif len(title) > 30:
        risks.append(
            Risk("R006", "LOW", "title", f"标题 {len(title)} 字，超过 30 字上限", "精简标题")
        )

    # R002 封面
    if not _text(md.get("cover_url")):
        missing.append("cover_url")

    # R003 时长
    state, duration = _duration(md)
    if state == MISSING:
        missing.append("duration_sec")
    elif state == INVALID:
        missing.append("duration_sec")

    # R004 版权来源（缺失不阻断，交用户确认）
    if not _text(md.get("copyright")):
        risks.append(
            Risk("R004", "MEDIUM", "copyright", "未填写版权来源", "补充版权归属后复核")
        )

    # 素材文件与格式
    if not _text(md.get("file_url")):
        missing.append("file_url")
    fmt = _text(md.get("format")).lower()
    if fmt and fmt not in FORMAT_WHITELIST:
        risks.append(
            Risk("R008", "HIGH", "format", f"格式 {fmt} 不在白名单内", "转码为 mp4 后重新提交")
        )

    # R005 敏感词
    tags = md.get("tags") or []
    tag_text = " ".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags)
    haystack = f"{title} {tag_text}"
    hits = [term for term in SENSITIVE_TERMS if term in haystack]
    if hits:
        risks.append(
            Risk(
                "R005",
                "HIGH",
                "title",
                f"命中敏感词：{'、'.join(hits)}",
                "改写标题与标签，去掉绝对化或效果承诺类表述",
            )
        )

    # R007 标签为空
    if not tag_text.strip():
        risks.append(Risk("R007", "LOW", "tags", "素材没有标签", "补充标签便于分发与检索"))

    # R009 内容重复（同一份素材换 ID 重传）：指纹相同即视为重复，发布前必须拦下
    duplicates = [str(item) for item in (duplicate_of or []) if str(item)]
    if duplicates:
        risks.append(
            Risk(
                "R009",
                "HIGH",
                "file_url",
                f"与素材 {'、'.join(duplicates)} 内容重复（同一文件指纹）",
                "确认是否重复上传；合并或替换后再排期",
            )
        )

    if missing:
        conclusion = "INCOMPLETE"
    elif any(risk.level == "HIGH" for risk in risks):
        conclusion = "RISK"
    else:
        conclusion = "PASSED"
    return AuditReport(
        conclusion=conclusion,
        missing_fields=missing,
        risks=risks,
        rules_version=RULES_VERSION,
    )
