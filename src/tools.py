"""工具层：素材读取、规则审核、时段查询、草稿保存、提醒。

三条约定
    1. 工具只返回 ToolResult，不向上抛业务异常——图里的节点据此分支，而不是靠 try/except 猜。
    2. 每个工具自己开一个短事务（类似 MySQL 逐条提交），失败只回滚自己那一步，不留半条数据。
    3. 判定类逻辑（能否排期、时段是否被占、是否重复）一律在工具里做，模型不参与。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .errors import LockTimeout
from .repository import DuplicateKey, Repository
from .rules import AuditReport, audit
from .store import JsonDatabase
from .util import day_window, floor_hour, idempotency_key, to_iso

# -- 错误码（与 docs/06-implementation.md 的错误码表一致） --------------------
ERR_NOT_FOUND = "NOT_FOUND"
ERR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
ERR_INVALID_STATE = "INVALID_STATE"
ERR_SLOT_CONFLICT = "SLOT_CONFLICT"
ERR_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
ERR_RULES_UNAVAILABLE = "RULES_UNAVAILABLE"
ERR_DB_UNAVAILABLE = "DB_UNAVAILABLE"
ERR_DB_WRITE_REJECTED = "DB_WRITE_REJECTED"
ERR_CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    message: str | None = None
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "data": self.data}
        if self.code:
            payload["error"] = {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            }
        return payload


def ok_result(**data: Any) -> ToolResult:
    """成功结果。"""
    return ToolResult(ok=True, data=data)


def fail_result(code: str, message: str, retryable: bool = False) -> ToolResult:
    """失败结果：带错误码与是否可重试，调用方据此决定重试还是收尾。"""
    return ToolResult(ok=False, code=code, message=message, retryable=retryable)


# 内部沿用的简写，避免几十处调用点改名
_ok = ok_result
_fail = fail_result


@dataclass
class Faults:
    """故障注入。mock 模式的验收靠它复现「工具失败/保存失败/提醒失败」。"""

    material_lookup: bool = False
    rules_unavailable: bool = False
    save_failure: str | None = None
    notify_failure: bool = False
    metadata_lag_once: bool = False

    @classmethod
    def from_request(cls, request: dict[str, Any]) -> "Faults":
        simulate = request.get("simulate") or {}
        if not isinstance(simulate, dict):
            return cls()
        return cls(
            material_lookup=bool(simulate.get("material_lookup")),
            rules_unavailable=bool(simulate.get("rules_unavailable")),
            save_failure=simulate.get("save_failure"),
            notify_failure=bool(simulate.get("notify_failure")),
            metadata_lag_once=bool(simulate.get("metadata_lag_once")),
        )


# ---------------------------------------------------------------------------
# 1. 素材读取
# ---------------------------------------------------------------------------
def material_get(
    db: JsonDatabase, material_id: str, faults: Faults, degrade: bool = False
) -> ToolResult:
    if faults.material_lookup:
        return _fail(
            ERR_SOURCE_UNAVAILABLE, "素材服务不可用（已模拟），请稍后重试", retryable=True
        )
    try:
        with db.transaction() as session:
            repo = Repository(session)
            material = repo.get_material(material_id)
            if material is None:
                return _fail(ERR_NOT_FOUND, f"素材 {material_id} 不存在", retryable=False)
            metadata = dict(material.get("metadata") or {})
            if degrade:
                # 模拟上游元数据延迟：首次读取缺字段，补查一次才完整。
                metadata.pop("cover_url", None)
            review = repo.get_review(material_id)
            return _ok(
                material={"material_id": material_id, "metadata": metadata},
                review=review,
            )
    except (LockTimeout, OSError) as exc:
        return _fail(ERR_DB_UNAVAILABLE, f"读取素材失败：{exc}", retryable=True)


# ---------------------------------------------------------------------------
# 2. 规则审核
# ---------------------------------------------------------------------------
def audit_check(
    db: JsonDatabase, material_id: str, metadata: dict[str, Any], faults: Faults
) -> ToolResult:
    if faults.rules_unavailable:
        return _fail(ERR_RULES_UNAVAILABLE, "审核规则不可用（已模拟）", retryable=False)
    report: AuditReport = audit(metadata)
    try:
        with db.transaction() as session:
            Repository(session).upsert_review(
                material_id=material_id,
                status=report.conclusion,
                metadata=metadata,
                risk_reason=report.risk_reason,
                rules_version=report.rules_version,
            )
    except (LockTimeout, OSError) as exc:
        return _fail(ERR_DB_UNAVAILABLE, f"写入审核结果失败：{exc}", retryable=True)
    return _ok(**report.as_dict())


# ---------------------------------------------------------------------------
# 3. 时段查询
# ---------------------------------------------------------------------------
def slot_query(
    db: JsonDatabase,
    moment: datetime,
    now: datetime,
    faults: Faults | None = None,
    exclude_material: str | None = None,
) -> ToolResult:
    """查询某天各时段状态。

    exclude_material：同一素材重复提交时，它自己占的档不算冲突——否则「再提交一次」
    会被误判成时段冲突，进而生成第二份草稿。这正是重复提交要防的坑。
    """
    day = floor_hour(moment)
    day_start = to_iso(day.replace(hour=0))
    day_end = to_iso(day.replace(hour=0) + timedelta(days=1))
    try:
        with db.transaction() as session:
            occupied = {
                row["date"]: row["material_id"]
                for row in Repository(session).active_schedules(day_start, day_end)
            }
    except (LockTimeout, OSError) as exc:
        return _fail(ERR_DB_UNAVAILABLE, f"查询时段失败：{exc}", retryable=True)

    slots: list[dict[str, Any]] = []
    for slot in day_window(day):
        iso = to_iso(slot)
        owner = occupied.get(iso)
        if owner is not None and owner != exclude_material:
            state = "occupied"
        elif slot < now:
            state, owner = "closed", None
        else:
            state = "free"
        slots.append({"slot": iso, "state": state, "occupied_by": owner})

    free = [item["slot"] for item in slots if item["state"] == "free"]
    return _ok(slots=slots, free=free, occupied=occupied)


def find_alternatives(
    db: JsonDatabase,
    desired: datetime,
    now: datetime,
    limit: int = 2,
    exclude_material: str | None = None,
) -> ToolResult:
    """目标时段附近最近可用的时段：先顺延、再提前，避免改动用户习惯的档位。"""
    result = slot_query(db, desired, now, exclude_material=exclude_material)
    if not result.ok:
        return result
    free = result.data["free"]
    target = to_iso(desired)
    later = [slot for slot in free if slot > target]
    earlier = [slot for slot in reversed(free) if slot < target]
    return _ok(
        alternatives=(later + earlier)[:limit],
        slots=result.data["slots"],
        occupied=result.data["occupied"],
        free=free,
    )


# ---------------------------------------------------------------------------
# 4. 草稿保存（幂等）
# ---------------------------------------------------------------------------
def schedule_save_draft(
    db: JsonDatabase,
    material_id: str,
    slot: datetime,
    rules_version: str,
    faults: Faults,
) -> ToolResult:
    if faults.save_failure:
        retryable = faults.save_failure != "db_write_rejected"
        code = ERR_DB_UNAVAILABLE if retryable else ERR_DB_WRITE_REJECTED
        message = (
            "数据库连接不可用（已模拟），写入未生效"
            if retryable
            else "数据库拒绝写入（已模拟），写入未生效"
        )
        return _fail(code, message, retryable=retryable)

    slot_iso = to_iso(slot)
    idem = idempotency_key(material_id, slot_iso, rules_version)
    try:
        with db.transaction() as session:
            repo = Repository(session)

            review = repo.get_review(material_id)
            if review is None or review.get("status") != "PASSED":
                return _fail(
                    ERR_INVALID_STATE,
                    "素材尚未通过审核，不能生成排期草稿",
                    retryable=False,
                )

            # 与 uk_idem 对应：同一请求重复提交复用同一份草稿。
            existing = repo.find_schedule_by_idem(idem)
            if existing is not None:
                return _ok(draft=existing, deduped=True, reused=False)

            # 与 uk_material_date 对应：同一素材同一时段只允许一行。
            existing = repo.find_schedule(material_id, slot_iso)
            if existing is not None:
                if existing.get("status") == "CANCELLED":
                    revived = repo.update_schedule(
                        existing["id"], status="DRAFT", confirm_time=None, idem_key=idem
                    )
                    return _ok(draft=revived, deduped=False, reused=True)
                return _ok(draft=existing, deduped=True, reused=False)

            # 容量为 1：同一时段不能有两个素材。
            hour_end = to_iso(slot + timedelta(hours=1))
            for row in repo.active_schedules(slot_iso, hour_end):
                if row["date"] == slot_iso and row["material_id"] != material_id:
                    return _fail(
                        ERR_SLOT_CONFLICT,
                        f"时段 {slot_iso} 已被素材 {row['material_id']} 占用",
                        retryable=False,
                    )

            row = repo.insert_schedule(
                {
                    "date": slot_iso,
                    "material_id": material_id,
                    "status": "DRAFT",
                    "confirm_time": None,
                    "idem_key": idem,
                }
            )
            return _ok(draft=row, deduped=False, reused=False)
    except DuplicateKey as exc:
        if exc.constraint == "uk_idem" and exc.existing:
            return _ok(draft=exc.existing, deduped=True, reused=False)
        if exc.existing:
            return _ok(draft=exc.existing, deduped=True, reused=False)
        return _fail(ERR_SLOT_CONFLICT, "时段冲突，请重新选择时段", retryable=False)
    except (LockTimeout, OSError) as exc:
        return _fail(ERR_DB_UNAVAILABLE, f"保存草稿失败：{exc}", retryable=True)


def schedule_confirm(db: JsonDatabase, draft_id: int) -> ToolResult:
    """用户确认：仍是草稿，只是把 confirm_time 落下来；不触发真实发布。"""
    try:
        with db.transaction() as session:
            repo = Repository(session)
            rows = session.select("publish_schedule", id=draft_id)
            if not rows:
                return _fail(ERR_NOT_FOUND, f"草稿 {draft_id} 不存在")
            if rows[0]["status"] != "DRAFT":
                return _fail(ERR_INVALID_STATE, f"草稿 {draft_id} 当前状态不可确认")
            updated = repo.update_schedule(
                draft_id, status="CONFIRMED", confirm_time=to_iso(datetime.now())
            )
            return _ok(draft=updated)
    except (LockTimeout, OSError) as exc:
        return _fail(ERR_DB_UNAVAILABLE, f"确认失败：{exc}", retryable=True)


def schedule_cancel(db: JsonDatabase, draft_id: int) -> ToolResult:
    """取消草稿：DRAFT → CANCELLED，该时段立即释放。CONFIRMED 不允许直接取消。"""
    try:
        with db.transaction() as session:
            repo = Repository(session)
            rows = session.select("publish_schedule", id=draft_id)
            if not rows:
                return _fail(ERR_NOT_FOUND, f"草稿 {draft_id} 不存在")
            if rows[0]["status"] != "DRAFT":
                return _fail(
                    ERR_INVALID_STATE,
                    f"草稿 {draft_id} 当前状态（{rows[0]['status']}）不可取消",
                )
            updated = repo.update_schedule(
                draft_id, status="CANCELLED", confirm_time=None
            )
            return _ok(draft=updated)
    except (LockTimeout, OSError) as exc:
        return _fail(ERR_DB_UNAVAILABLE, f"取消失败：{exc}", retryable=True)


# ---------------------------------------------------------------------------
# 5. 提醒
# ---------------------------------------------------------------------------
def notify_push(
    db: JsonDatabase,
    material_id: str,
    event: str,
    payload: dict[str, Any],
    dedupe_key: str,
    faults: Faults,
) -> ToolResult:
    if faults.notify_failure:
        return _fail(ERR_CHANNEL_UNAVAILABLE, "提醒通道不可用（已模拟）", retryable=True)
    try:
        with db.transaction() as session:
            repo = Repository(session)
            existing = repo.find_notify(dedupe_key)
            if existing is not None:
                return _ok(notify=existing, deduped=True)
            row = repo.insert_notify(
                {
                    "dedupe_key": dedupe_key,
                    "material_id": material_id,
                    "event": event,
                    "channel": "inapp",
                    "payload": payload,
                    "status": "SENT",
                }
            )
            return _ok(notify=row, deduped=False)
    except DuplicateKey as exc:
        return _ok(notify=exc.existing or {}, deduped=True)
    except (LockTimeout, OSError) as exc:
        return _fail(ERR_DB_UNAVAILABLE, f"提醒失败：{exc}", retryable=True)
