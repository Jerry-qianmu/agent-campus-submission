"""JSON 单文件数据库：用最小实现复刻 MySQL 的表、主键、唯一键与事务语义。

为什么不是 sqlite
    题目要求保留 MySQL 设计，但交付环境不能依赖数据库服务。这里把真正用到的四件事
    抽出来实现——表、自增主键、唯一键、事务——数据模型与 ``sql/schema.sql`` 一一对应，
    换成 MySQL 时只替换本模块与 repository 的驱动，业务代码不动。

语义对应
    MySQL 表                 -> data["tables"][name] 的数组
    AUTO_INCREMENT id        -> data["sequences"][name]
    唯一索引                 -> TableSpec.unique；按 MySQL 规则，含 NULL 的组合不参与判定
    事务提交                 -> 写临时文件后 os.replace 原子替换
    事务回滚                 -> 丢弃内存副本，不写盘
    表级/行级锁              -> 独占锁文件（O_CREAT|O_EXCL），跨进程互斥
    DATETIME                 -> 带偏移的 ISO 8601 字符串（统一 Asia/Shanghai）
"""

from __future__ import annotations

import copy
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import LockTimeout, UniqueViolation

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TableSpec:
    """一张表的约束声明，等价于 DDL 中的 PRIMARY KEY / UNIQUE KEY。"""

    name: str
    pk: str
    unique: tuple[tuple[str, ...], ...] = ()
    auto_id: bool = False


TABLES: dict[str, TableSpec] = {
    "materials": TableSpec("materials", "id"),
    "material_review": TableSpec(
        "material_review", "id", unique=(("material_id",),), auto_id=True
    ),
    "publish_schedule": TableSpec(
        "publish_schedule",
        "id",
        unique=(("material_id", "date"), ("idem_key",)),
        auto_id=True,
    ),
    "notify_log": TableSpec("notify_log", "id", unique=(("dedupe_key",),), auto_id=True),
}


def empty_database() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "sequences": {name: 0 for name, spec in TABLES.items() if spec.auto_id},
        "tables": {name: [] for name in TABLES},
    }


class Session:
    """一次事务内的内存操作句柄。所有写入在 commit 前只存在于内存副本中。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    # -- 读 ----------------------------------------------------------------
    def rows(self, table: str) -> list[dict[str, Any]]:
        return self._data["tables"][table]

    def select(self, table: str, **filters: Any) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows(table)
            if all(row.get(key) == value for key, value in filters.items())
        ]

    def select_one(self, table: str, **filters: Any) -> dict[str, Any] | None:
        found = self.select(table, **filters)
        return found[0] if found else None

    # -- 写 ----------------------------------------------------------------
    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        spec = TABLES[table]
        record = dict(row)
        if spec.auto_id and record.get(spec.pk) is None:
            record[spec.pk] = self.next_id(table)
        self._assert_unique(table, record, skip_pk=None)
        self.rows(table).append(record)
        return record

    def update(self, table: str, pk_value: Any, **changes: Any) -> dict[str, Any]:
        spec = TABLES[table]
        target = self.select_one(table, **{spec.pk: pk_value})
        if target is None:
            raise KeyError(f"{table} has no row with {spec.pk}={pk_value!r}")
        merged = {**target, **changes}
        self._assert_unique(table, merged, skip_pk=pk_value)
        target.update(changes)
        return target

    def next_id(self, table: str) -> int:
        spec = TABLES[table]
        if not spec.auto_id:
            raise TypeError(f"{table} has no AUTO_INCREMENT column")
        current = int(self._data["sequences"].get(table, 0)) + 1
        self._data["sequences"][table] = current
        return current

    # -- 约束 --------------------------------------------------------------
    def _assert_unique(
        self, table: str, record: dict[str, Any], skip_pk: Any
    ) -> None:
        spec = TABLES[table]
        for columns in spec.unique:
            values = tuple(record.get(column) for column in columns)
            if any(value is None for value in values):
                # MySQL 唯一索引允许多个 NULL，这里保持一致。
                continue
            for other in self.rows(table):
                if skip_pk is not None and other.get(spec.pk) == skip_pk:
                    continue
                if tuple(other.get(column) for column in columns) == values:
                    raise UniqueViolation(table, columns, values)


class JsonDatabase:
    """一份 JSON 文件就是一个库；用文件锁把并发进程串行化。"""

    def __init__(
        self,
        path: Path,
        seed_path: Path | None = None,
        lock_timeout: float = 5.0,
        stale_lock_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path)
        self.seed_path = Path(seed_path) if seed_path else None
        self.lock_timeout = lock_timeout
        self.stale_lock_seconds = stale_lock_seconds
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    # -- 初始化 ------------------------------------------------------------
    def initialize(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = (
            json.loads(self.seed_path.read_text(encoding="utf-8"))
            if self.seed_path and self.seed_path.exists()
            else empty_database()
        )
        self._write(data)

    # -- 事务 --------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """排他锁 + 读全库 + 成功时原子落盘；异常时整体丢弃。"""
        self.initialize()
        with self._file_lock():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            snapshot = copy.deepcopy(data)
            try:
                yield Session(data)
            except Exception:
                # 回滚 = 什么都不写：磁盘上仍是 snapshot 之前的内容。
                raise
            if data != snapshot:
                self._write(data)

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        deadline = time.monotonic() + self.lock_timeout
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                if time.monotonic() > deadline:
                    if self._is_stale_lock():
                        os.unlink(self.lock_path)
                        continue
                    raise LockTimeout(f"lock timeout: {self.lock_path}") from None
                time.sleep(0.005)
            except PermissionError:
                # Windows 特有：另一个持有者正在删除锁文件（delete-pending），
                # 此时 O_CREAT|O_EXCL 返回 ERROR_ACCESS_DENIED 而不是 ERROR_FILE_EXISTS。
                # 语义上等同于「锁已被占用」，按等待处理，不能当异常抛出去。
                if time.monotonic() > deadline:
                    raise LockTimeout(f"lock timeout: {self.lock_path}") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            os.close(fd)
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:  # pragma: no cover - 已被外部清理
                pass

    def _is_stale_lock(self) -> bool:
        """进程崩溃会留下锁文件；超过阈值视为陈旧并回收。"""
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:  # pragma: no cover - 竞态窗口
            return True
        return age > self.stale_lock_seconds

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
