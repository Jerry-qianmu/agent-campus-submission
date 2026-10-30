import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.errors import LockTimeout, UniqueViolation
from src.store import JsonDatabase
from support import SEED, TempDb, fresh_db, read_table


class StoreTests(unittest.TestCase):
    def test_seeded_from_seed_file(self):
        with TempDb() as db:
            db.initialize()
            self.assertEqual(len(read_table(db, "publish_schedule")), 1)
            self.assertEqual(read_table(db, "publish_schedule")[0]["material_id"], "M00")

    def test_unique_material_date(self):
        with TempDb() as db:
            with self.assertRaises(UniqueViolation):
                with db.transaction() as session:
                    session.insert(
                        "publish_schedule",
                        {
                            "date": "2026-11-05T18:00:00+08:00",
                            "material_id": "M00",
                            "status": "DRAFT",
                            "confirm_time": None,
                            "idem_key": None,
                        },
                    )

    def test_null_unique_column_allows_duplicates(self):
        """MySQL 唯一索引允许多个 NULL；idem_key 为空的种子行不能被判重复。"""
        with TempDb() as db:
            with db.transaction() as session:
                session.insert(
                    "publish_schedule",
                    {
                        "date": "2026-11-05T20:00:00+08:00",
                        "material_id": "M02",
                        "status": "DRAFT",
                        "confirm_time": None,
                        "idem_key": None,
                    },
                )
            self.assertEqual(len(read_table(db, "publish_schedule")), 2)

    def test_auto_increment(self):
        with TempDb() as db:
            with db.transaction() as session:
                first = session.insert(
                    "publish_schedule",
                    {"date": "2026-11-05T20:00:00+08:00", "material_id": "M02",
                     "status": "DRAFT", "confirm_time": None, "idem_key": None},
                )
                second = session.insert(
                    "publish_schedule",
                    {"date": "2026-11-05T21:00:00+08:00", "material_id": "M03",
                     "status": "DRAFT", "confirm_time": None, "idem_key": None},
                )
            self.assertEqual((first["id"], second["id"]), (2, 3))

    def test_rollback_leaves_no_partial_row(self):
        with TempDb() as db:
            with self.assertRaises(RuntimeError):
                with db.transaction() as session:
                    session.insert(
                        "publish_schedule",
                        {"date": "2026-11-05T20:00:00+08:00", "material_id": "M02",
                         "status": "DRAFT", "confirm_time": None, "idem_key": None},
                    )
                    raise RuntimeError("boom")
            self.assertEqual(len(read_table(db, "publish_schedule")), 1)

    def test_transaction_persists_and_is_atomic(self):
        with TempDb() as db:
            with db.transaction() as session:
                session.insert(
                    "publish_schedule",
                    {"date": "2026-11-05T20:00:00+08:00", "material_id": "M02",
                     "status": "DRAFT", "confirm_time": None, "idem_key": None},
                )
            self.assertEqual(len(read_table(db, "publish_schedule")), 2)
            self.assertFalse(Path(str(db.path) + ".tmp").exists())

    def test_concurrent_writers_do_not_lose_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = fresh_db(tmp)
            db.initialize()
            errors: list[Exception] = []

            def writer(index: int) -> None:
                try:
                    with db.transaction() as session:
                        session.insert(
                            "publish_schedule",
                            {"date": f"2026-11-06T{index % 15 + 8:02d}:00:00+08:00",
                             "material_id": f"C{index:03d}", "status": "DRAFT",
                             "confirm_time": None, "idem_key": None},
                        )
                except Exception as exc:  # pragma: no cover - 失败才记录
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(read_table(db, "publish_schedule")), 9)

    def test_permission_denied_on_lock_is_retried(self):
        """Windows 上锁文件处于 delete-pending 时，O_CREAT|O_EXCL 抛 PermissionError。

        语义等同「锁被占用」，必须等待重试而不是把异常抛给调用方。
        """
        real_open = os.open
        calls = {"lock": 0}

        def flaky_open(path, flags, *args, **kwargs):
            if str(path).endswith(".lock") and calls["lock"] == 0:
                calls["lock"] += 1
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        with TempDb() as db:
            with mock.patch("src.store.os.open", side_effect=flaky_open):
                with db.transaction() as session:
                    session.insert(
                        "publish_schedule",
                        {"date": "2026-11-05T20:00:00+08:00", "material_id": "M02",
                         "status": "DRAFT", "confirm_time": None, "idem_key": None},
                    )
            self.assertEqual(calls["lock"], 1)
            self.assertEqual(len(read_table(db, "publish_schedule")), 2)

    def test_persistent_permission_denied_raises_lock_timeout(self):
        """持续被拒只能是 LockTimeout，让工具层翻译成可重试的 DB_UNAVAILABLE。"""

        def denied(path, flags, *args, **kwargs):
            if str(path).endswith(".lock"):
                raise PermissionError(13, "Permission denied")
            return os.open(path, flags, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            db = fresh_db(tmp)
            db.lock_timeout = 0.05
            with mock.patch("src.store.os.open", side_effect=denied):
                with self.assertRaises(LockTimeout):
                    with db.transaction():
                        pass

    def test_seed_missing_creates_empty_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = JsonDatabase(Path(tmp) / "db.json")
            db.initialize()
            self.assertEqual(json.loads(db.path.read_text())["schema_version"], 1)
            self.assertTrue((SEED).exists())


if __name__ == "__main__":
    unittest.main()
