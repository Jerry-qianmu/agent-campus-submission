"""时间与哈希工具：全项目只在这里解析和格式化时间，避免各处裸用 datetime.now()。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # Linux/macOS 自带 tzdata；Windows 可能没有，退回固定 +08:00（无夏令时，等价）
    TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # pragma: no cover - 取决于运行环境
    TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 发布窗口：08:00 起，最晚 23:00 结束，整点粒度。
WINDOW_START_HOUR = 8
WINDOW_END_HOUR = 23


def parse_datetime(value: str | datetime) -> datetime:
    """解析 ISO 8601（允许省略偏移，缺省按 Asia/Shanghai）。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip())
    else:
        raise ValueError("时间必须是非空的 ISO 8601 字符串")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def to_iso(moment: datetime) -> str:
    """规范化为库内存储格式：2026-11-05T20:00:00+08:00。"""
    return moment.astimezone(TZ).replace(microsecond=0).isoformat()


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def day_window(moment: datetime) -> list[datetime]:
    """某天的全部可选时段（整点，落在发布窗口内）。"""
    start = moment.astimezone(TZ).replace(
        hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    return [
        start + timedelta(hours=offset)
        for offset in range(WINDOW_END_HOUR - WINDOW_START_HOUR)
    ]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def idempotency_key(material_id: str, slot_iso: str, rules_version: str) -> str:
    """与 publish_schedule.uk_idem 对应的幂等键。"""
    return sha256_text(f"{material_id}|{slot_iso}|{rules_version}")


def content_fingerprint(metadata: dict | None) -> str:
    """素材内容指纹：用于识别「同一份素材换了 ID 又传一次」。

    归一化后再哈希，避免大小写/空白造成假不重复：
        有 file_url → file|<url>|<duration>|<format>
        无 file_url → meta|<title>|<duration>|<format>

    边界：mock 环境没有文件字节，只能指纹元数据；真实环境应当对文件流做 sha256
    （或直接用存储侧的 etag），那时把本函数换成文件哈希即可，上层无需改动。
    """
    md = metadata if isinstance(metadata, dict) else {}
    file_url = str(md.get("file_url") or "").strip().lower()
    duration = md.get("duration_sec")
    fmt = str(md.get("format") or "").strip().lower()
    if file_url:
        basis = f"file|{file_url}|{duration}|{fmt}"
    else:
        title = str(md.get("title") or "").strip().lower()
        basis = f"meta|{title}|{duration}|{fmt}"
    return sha256_text(basis)
