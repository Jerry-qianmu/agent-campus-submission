"""用真实 DeepSeek key 手动验证接入是否通。

    PowerShell:
        $env:DEEPSEEK_API_KEY = "sk-..."      # 仅当前会话
        python -m scripts.smoke_deepseek
        python -m scripts.smoke_deepseek --full   # 再跑一遍完整 ReAct 示例

三个检查点
    1. 连通性：一次普通 chat，确认 key / 网络 / 模型名可用；
    2. 工具调用：带上 tools 让模型选动作，确认它回的是 tool_calls 而不是一段散文；
    3. 完整流程（--full）：用 mock 库跑 conflict 示例，看决策日志是否符合预期。

不带 key 时直接给出可操作的提示，不抛出栈。本脚本不写入仓库内的任何数据库（用临时库）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients import ChatError, build_client  # noqa: E402
from src.env import load_dotenv  # noqa: E402
from src.graph import run_request  # noqa: E402
from src.registry import tool_schemas  # noqa: E402
from src.store import JsonDatabase  # noqa: E402


def _check_client():
    client = build_client("deepseek")
    if client is None:  # pragma: no cover - provider 固定为 deepseek
        raise SystemExit("build_client 返回 None，检查 provider")
    return client


def step_connectivity(client) -> int:
    print(f"[1/3] 连通性：{client.endpoint} model={client.model}")
    try:
        result = client.chat([{"role": "user", "content": "只回复两个字：连上了"}])
    except ChatError as error:
        print(f"  ✗ 失败：{error}（code={error.code}, retryable={error.retryable}）")
        return 1
    print(f"  ✓ 回复：{result.content.strip()[:40]!r}")
    if result.usage:
        print(f"  token 用量：{result.usage}")
    return 0


def step_tool_call(client) -> int:
    print("[2/3] 工具调用：让模型在 5 个动作里选一个")
    messages = [
        {
            "role": "system",
            "content": "你是排期助手。先调用 material.get 读素材，不要直接回答。",
        },
        {"role": "user", "content": "把素材 M01 排在 2026-11-05 18:00"},
    ]
    try:
        result = client.chat(messages, tools=tool_schemas())
    except ChatError as error:
        print(f"  ✗ 失败：{error}（code={error.code}）")
        return 1
    if not result.tool_calls:
        print(f"  ✗ 模型没有返回 tool_calls，content={result.content[:80]!r}")
        print("    可能是模型不支持 function calling，或 tool_choice 被忽略")
        return 1
    call = result.tool_calls[0]
    print(f"  ✓ 选中动作：{call['name']}  参数：{call['arguments']}")
    if result.reasoning:
        print(f"  （思考模式返回了 reasoning_content，长度 {len(result.reasoning)}）")
    return 0


def step_full_flow() -> int:
    print("[3/3] 完整 ReAct 流程：examples/conflict.input.json")
    request = json.loads(
        (ROOT / "examples" / "conflict.input.json").read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory(prefix="deepseek-smoke-") as tmp:
        db = JsonDatabase(Path(tmp) / "db.json", seed_path=ROOT / "data" / "seed.json")
        try:
            result = run_request(request, db, provider="deepseek")
        except ChatError as error:
            print(f"  ✗ 失败：{error}（code={error.code}）")
            return 1

    print(f"  status={result['status']} steps={result['steps']} calls={result['tool_calls']}")
    print(f"  suggested_slot={result['suggested_slot']}")
    print(f"  published={result['published']}  llm_usage={result.get('llm_usage')}")
    for decision in result["decisions"]:
        action = decision["action"]
        args = json.dumps(decision["args"], ensure_ascii=False)
        print(f"    {decision['step']}. {action} {args}")
        print(f"       依据：{decision['rationale'][:60]}")
    if result["status"] == "needs_confirmation" and result["draft"]["saved"]:
        print("  ✓ 与 mock 路径结论一致（改档到 19:00 并标待确认）")
        return 0
    print("  ⚠ 与 mock 路径结论不同——这属于真实模型的差异，需要人工看决策日志")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="额外跑一遍完整 ReAct 示例")
    args = parser.parse_args(argv)

    loaded = load_dotenv()
    if loaded:
        print(f"[env] 从 .env 载入：{', '.join(sorted(loaded))}")

    try:
        client = _check_client()
    except ChatError as error:
        print(f"构建客户端失败：{error}", file=sys.stderr)
        return 2
    except Exception as error:  # InvalidInput：没配 key
        print(f"{error}", file=sys.stderr)
        print('设置 DEEPSEEK_API_KEY 后重试，例如：$env:DEEPSEEK_API_KEY = "sk-..."', file=sys.stderr)
        return 2

    failures = step_connectivity(client) or step_tool_call(client)
    if args.full and failures == 0:
        failures = step_full_flow()
    print("\n结论：" + ("全部通过" if failures == 0 else "有失败项，见上"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
