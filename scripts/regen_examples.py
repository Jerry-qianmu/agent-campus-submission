"""重新生成 examples/*.output.json、执行路径记录与 examples/test-results.txt。

示例输出必须是「真实跑出来的」，不是手写的。改了规则或流程后执行一次：
    python -m scripts.regen_examples
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

CASES = (
    "normal",
    "missing",
    "risk",
    "conflict",
    "hard-conflict",
    "duplicate",
    "duplicate-material",
    "save-failure",
    "tool-failure",
    "metadata-lag",
    "not-found",
)

# 需要留档的 ReAct 执行路径（决策日志 JSONL，含步骤/参数/观测/后续动作/依据）
RUN_RECORDS = {
    "run-record-soft-conflict.jsonl": "conflict",
    "run-record-hard-conflict.jsonl": "hard-conflict",
    "run-record-metadata-lag.jsonl": "metadata-lag",
}


def _run(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        env=env,
    )


def write(path: Path, text: str) -> None:
    # Windows 下子进程 stdout 已经带 \r\n，统一折成 LF，避免整文件被 git 判成改动
    body = text.replace("\r\n", "\n").rstrip("\n") + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")


def main() -> int:
    for name in CASES:
        process = _run("-m", "src.main", "--input", f"examples/{name}.input.json")
        if process.returncode != 0:
            print(f"{name}: 运行失败\n{process.stderr.decode('utf-8')}", file=sys.stderr)
            return 1
        write(EXAMPLES / f"{name}.output.json", process.stdout.decode("utf-8"))
        print(f"{name}: ok")

    for filename, case in RUN_RECORDS.items():
        process = _run(
            "-m",
            "src.main",
            "--input",
            f"examples/{case}.input.json",
            "--log",
            f"examples/{filename}",
        )
        if process.returncode != 0:
            print(f"{filename}: 运行失败\n{process.stderr.decode('utf-8')}", file=sys.stderr)
            return 1
        print(f"{filename}: ok")

    tests = _run("-m", "unittest", "discover", "-s", "tests", "-v")
    if tests.returncode != 0:
        print(tests.stderr.decode("utf-8"), file=sys.stderr)
        return 1
    body = tests.stderr.decode("utf-8")
    write(
        EXAMPLES / "test-results.txt",
        "Command: python -m unittest discover -s tests -v\n"
        f"Environment: {sys.version.split()[0]} / langgraph mock mode\n"
        f"{body}",
    )
    print("test-results.txt: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
