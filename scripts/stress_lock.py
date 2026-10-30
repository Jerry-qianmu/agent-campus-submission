"""锁压力测试：多线程并发写同一个库，验证「不丢行、不抛非预期异常」。

这个脚本就是当初发现「Windows 锁文件 delete-pending 时报 PermissionError」的工具：
    python -m scripts.stress_lock            # 默认 20 轮 × 8 线程
    python -m scripts.stress_lock 50 12      # 50 轮 × 12 线程

任何一轮出现异常都会打印完整栈并返回非零退出码，便于在 CI 或本地反复验证。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 python scripts/stress_lock.py 直接跑
    sys.path.insert(0, str(ROOT))

from tests.support import fresh_db  # noqa: E402 - 复用测试的建库逻辑


def _worker(db, index: int, errors: list[str]) -> None:
    try:
        with db.transaction() as session:
            session.insert(
                "publish_schedule",
                {
                    "date": f"2026-11-06T{index % 15 + 8:02d}:00:00+08:00",
                    "material_id": f"C{index:03d}",
                    "status": "DRAFT",
                    "confirm_time": None,
                    "idem_key": None,
                },
            )
    except Exception:  # pragma: no cover - 失败才记录
        errors.append(traceback.format_exc())


def stress(rounds: int = 20, n: int = 8) -> int:
    failures = 0
    for round_index in range(rounds):
        with tempfile.TemporaryDirectory() as tmp:
            db = fresh_db(tmp)
            db.initialize()
            errors: list[str] = []
            threads = [
                threading.Thread(target=_worker, args=(db, index, errors))
                for index in range(n)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            rows = [
                row
                for row in json.loads(db.path.read_text(encoding="utf-8"))["tables"][
                    "publish_schedule"
                ]
                if row["material_id"].startswith("C")
            ]
            if errors or len(rows) != n:
                failures += 1
                print(f"--- 第 {round_index + 1} 轮失败 (rows={len(rows)}, 期望 {n}) ---")
                print(errors[0] if errors else "")
    print(f"结果: {failures} 轮失败 / {rounds} 轮")
    return 1 if failures else 0


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    threads = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    sys.exit(stress(rounds, threads))
