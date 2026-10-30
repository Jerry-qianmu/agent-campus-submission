"""命令行入口：读一个请求 JSON，跑一遍 LangGraph 流程，把结果 JSON 打到 stdout。

约定（与参考交付示例一致）：
    业务异常（缺字段、风险、冲突、工具失败）-> 退出码 0，结果里带 status；
    入参本身不合法（不是 JSON 对象、字段类型错、文件读不到）-> 退出码 2，stderr 输出 invalid_input。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .env import load_dotenv
from .graph import run_request
from .store import JsonDatabase
from .util import parse_datetime

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed.json"

MAX_REPEAT = 5


def _dump(payload: dict[str, Any], compact: bool = False) -> str:
    if compact:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(payload, ensure_ascii=False, indent=2)


def read_request(path: Path) -> dict[str, Any]:
    # utf-8-sig：Windows 记事本/PowerShell 存出的 JSON 常带 BOM，直接按 utf-8 读会解析失败。
    request = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(request, dict):
        raise ValueError("input must be a JSON object")
    return request


def resolve_clock(request: dict[str, Any], cli_now: str | None) -> datetime | None:
    """时钟优先级：--now > 请求里的 now > 真实时间（便于示例长期可复现）。"""
    if cli_now:
        return parse_datetime(cli_now)
    if request.get("now"):
        return parse_datetime(request["now"])
    return None


def open_database(explicit: str | None):
    """没有 --db 时用临时库：每个示例都从同一份 seed 起步，输出可复现。"""
    if explicit:
        return JsonDatabase(Path(explicit), seed_path=SEED), None
    sandbox = tempfile.TemporaryDirectory(prefix="material-schedule-")
    return JsonDatabase(Path(sandbox.name) / "db.json", seed_path=SEED), sandbox


def run_once(
    request: dict[str, Any],
    db: JsonDatabase,
    now: datetime | None,
    compact: bool = False,
    max_steps: int | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    repeat = request.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f"repeat 必须是 1..{MAX_REPEAT} 的整数")

    results = [
        run_request(
            request,
            db,
            now=now,
            max_steps=max_steps,
            provider=provider,
            model=model,
        )
        for _ in range(repeat)
    ]
    result = results[-1]

    if repeat > 1:
        # 同一请求连提两次：必须落到同一份草稿，不能产生第二行。
        draft_ids = [item["draft"]["draft_id"] for item in results if item["draft"].get("saved")]
        result = dict(result)
        result["deduplication"] = {
            "repeat": repeat,
            "draft_ids": draft_ids,
            "single_draft": len(set(draft_ids)) == 1 and len(draft_ids) == repeat,
            "deduped_on_last_try": bool(result["draft"].get("deduped")),
        }
    return result


def write_decision_log(path: Path, result: dict[str, Any]) -> None:
    """把决策日志写成 JSONL：每行一步，含动作、参数、观测摘要、后续动作、决策依据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(step, ensure_ascii=False) for step in result.get("decisions") or []]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="请求 JSON 路径")
    parser.add_argument("--db", help="持久化数据库文件；省略则用临时库")
    parser.add_argument("--now", help="注入当前时间（ISO 8601），用于复现示例")
    parser.add_argument("--compact", action="store_true", help="输出单行 JSON")
    parser.add_argument(
        "--max-steps", type=int, help="工具调用次数上限（默认 10），达到后返回 step_limit_reached"
    )
    parser.add_argument("--log", help="把决策日志写成 JSONL（每行一步）")
    parser.add_argument(
        "--provider",
        choices=("mock", "deepseek", "http"),
        help="模型提供方；省略则读 LLM_PROVIDER，默认 mock（离线规则策略）",
    )
    parser.add_argument("--model", help="覆盖模型名，如 deepseek-flash / deepseek-v4-pro")
    parser.add_argument(
        "--env-file", help="指定 .env 路径；省略则读仓库根目录的 .env（已存在的环境变量优先）"
    )
    args = parser.parse_args(argv)

    # .env 必须在构造任何客户端之前加载，否则 os.environ 里没有 key
    loaded = load_dotenv(args.env_file)
    provider = (args.provider or os.getenv("LLM_PROVIDER") or "mock").strip().lower()
    if loaded and provider != "mock":
        # 只打键名不打值；mock 模式不需要打扰
        print(f"[env] 已载入 {len(loaded)} 个变量：{', '.join(sorted(loaded))}", file=sys.stderr)

    try:
        request = read_request(Path(args.input))
        now = resolve_clock(request, args.now)
        if args.max_steps is not None and args.max_steps <= 0:
            raise ValueError("--max-steps 必须是正整数")
        db, sandbox = open_database(args.db)
        try:
            result = run_once(
                request,
                db,
                now,
                compact=args.compact,
                max_steps=args.max_steps,
                provider=args.provider,
                model=args.model,
            )
        finally:
            if sandbox is not None:
                sandbox.cleanup()
        if args.log:
            write_decision_log(Path(args.log), result)
        print(_dump(result, compact=args.compact))
        return 0
    except (OSError, ValueError) as exc:
        print(
            _dump({"status": "invalid_input", "error": str(exc)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
