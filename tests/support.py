"""测试共用脚手架：每个用例一份独立的临时库，互不污染。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from src.store import JsonDatabase
from src.util import parse_datetime

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed.json"
EXAMPLES = ROOT / "examples"
NOW = parse_datetime("2026-11-05T09:00:00+08:00")


def fresh_db(directory: str) -> JsonDatabase:
    return JsonDatabase(Path(directory) / "db.json", seed_path=SEED)


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.input.json").read_text(encoding="utf-8"))


def read_table(db: JsonDatabase, table: str) -> list[dict]:
    return json.loads(db.path.read_text(encoding="utf-8"))["tables"][table]


class TempDb:
    """contextmanager：with TempDb() as db: ..."""

    def __enter__(self) -> JsonDatabase:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-test-")
        return fresh_db(self._tmp.name)

    def __exit__(self, *exc_info) -> None:
        self._tmp.cleanup()
