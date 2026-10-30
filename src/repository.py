"""数据访问层：把「表」的读写收敛在这里，业务层不直接碰 JSON 结构。

换 MySQL 时，本文件的方法体改成 SQLAlchemy 查询即可，方法签名与语义保持不变。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .errors import UniqueViolation
from .store import Session
from .util import to_iso

# 占用时段 = 仍然有效的排期（草稿未确认也算占位，避免两个素材抢同一档）。
ACTIVE_SCHEDULE_STATUS = ("DRAFT", "CONFIRMED")


def _now_iso() -> str:
    """对应 DDL 里的 create_time/update_time 默认值。"""
    return to_iso(datetime.now())


class DuplicateKey(Exception):
    """把存储层的唯一约束冲突翻译成业务可判断的异常。"""

    def __init__(self, constraint: str, existing: dict[str, Any] | None) -> None:
        self.constraint = constraint
        self.existing = existing
        super().__init__(f"duplicate: {constraint}")


class Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- 素材库 ------------------------------------------------------------
    def get_material(self, material_id: str) -> dict[str, Any] | None:
        return self.session.select_one("materials", id=material_id)

    # -- 素材审核 ----------------------------------------------------------
    def get_review(self, material_id: str) -> dict[str, Any] | None:
        return self.session.select_one("material_review", material_id=material_id)

    def upsert_review(
        self,
        material_id: str,
        status: str,
        metadata: dict[str, Any],
        risk_reason: str | None,
        rules_version: str,
    ) -> dict[str, Any]:
        """一素材一行。release_date 永不在此写入——本期不接真实发布。"""
        existing = self.get_review(material_id)
        if existing is None:
            return self.session.insert(
                "material_review",
                {
                    "material_id": material_id,
                    "status": status,
                    "metadata": metadata,
                    "release_date": None,
                    "risk_reason": risk_reason,
                    "rules_version": rules_version,
                    "create_time": _now_iso(),
                    "update_time": _now_iso(),
                },
            )
        return self.session.update(
            "material_review",
            existing["id"],
            status=status,
            metadata=metadata,
            risk_reason=risk_reason,
            rules_version=rules_version,
            update_time=_now_iso(),
        )

    # -- 发布排期 ----------------------------------------------------------
    def active_schedules(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.session.select("publish_schedule")
            if row["date"] >= date_from
            and row["date"] < date_to
            and row["status"] in ACTIVE_SCHEDULE_STATUS
        ]

    def find_schedule_by_idem(self, idem_key: str) -> dict[str, Any] | None:
        return self.session.select_one("publish_schedule", idem_key=idem_key)

    def find_schedule(self, material_id: str, date: str) -> dict[str, Any] | None:
        return self.session.select_one(
            "publish_schedule", material_id=material_id, date=date
        )

    def active_schedules_of(self, material_id: str) -> list[dict[str, Any]]:
        """该素材当前所有有效排期（DRAFT/CONFIRMED），用于「一个素材只排一次」的校验。"""
        return [
            row
            for row in self.session.select("publish_schedule", material_id=material_id)
            if row["status"] in ACTIVE_SCHEDULE_STATUS
        ]

    def insert_schedule(self, row: dict[str, Any]) -> dict[str, Any]:
        stamped = {"create_time": _now_iso(), "update_time": _now_iso(), **row}
        try:
            return self.session.insert("publish_schedule", stamped)
        except UniqueViolation as exc:
            constraint = "uk_material_date" if "material_id" in exc.columns else "uk_idem"
            existing = (
                self.find_schedule(row["material_id"], row["date"])
                if constraint == "uk_material_date"
                else self.find_schedule_by_idem(row.get("idem_key") or "")
            )
            raise DuplicateKey(constraint, existing) from None

    def update_schedule(self, pk: int, **changes: Any) -> dict[str, Any]:
        return self.session.update(
            "publish_schedule", pk, update_time=_now_iso(), **changes
        )

    # -- 提醒日志 ----------------------------------------------------------
    def find_notify(self, dedupe_key: str) -> dict[str, Any] | None:
        return self.session.select_one("notify_log", dedupe_key=dedupe_key)

    def insert_notify(self, row: dict[str, Any]) -> dict[str, Any]:
        stamped = {"create_time": _now_iso(), **row}
        try:
            return self.session.insert("notify_log", stamped)
        except UniqueViolation:
            existing = self.find_notify(row["dedupe_key"])
            raise DuplicateKey("uk_dedupe", existing) from None
