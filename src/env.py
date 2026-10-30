"""极简 .env 加载器（不引第三方依赖）。配置的唯一来源就是 .env。

为什么需要它
    之前只读进程环境变量，用户在 .env 里配好 key 却报"没有 key"。这不是配置错，是代码没读。

加载方式
    解析后写回 os.environ，因此全项目的配置读取统一用 os.getenv("KEY")。
    读代码时只需要知道一事：**配置以 .env 为准**。

支持的写法
    KEY=value
    export KEY=value
    KEY="value with spaces"      # 双引号内的 # 不当注释
    KEY='value'
    KEY=value   # 行尾注释（只在不带引号时生效，且 # 前必须有空白）
    # 整行注释、空行

刻意不支持
    变量插值（${OTHER}）、多行值、转义序列。排期 Agent 的配置就几个字符串和数字，
    复杂语法只会带来"到底解析成什么"的歧义。

优先级
    .env 覆盖同名进程环境变量（override=True 为默认）。
    这样"改 .env 就生效"，不会出现 shell 里残留的旧 key 把文件配置压掉的情况。
    需要让外部注入优先时，显式调用 load_dotenv(override=False)。
"""

from __future__ import annotations

import os
from pathlib import Path

_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _valid_key(key: str) -> bool:
    return bool(key) and (key[0].isalpha() or key[0] == "_") and all(ch in _KEY_CHARS for ch in key)


def _strip_value(raw: str) -> str:
    """去掉首尾空白与行尾注释；引号内的内容原样保留。"""
    value = raw.strip()
    if not value:
        return ""

    if value[0] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]

    # 形如 `KEY=    # 说明` 的行：值是空的，后面只是注释
    if value.startswith("#"):
        return ""

    # 未加引号：只有"空白 + #"才算注释起点，避免把 a#b 这种值截断
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in (" ", "\t"):
            return value[:index].strip()
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本，返回键值对。非法行直接跳过，不抛异常。"""
    result: dict[str, str] = {}
    for line in text.lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not _valid_key(key):
            continue
        result[key] = _strip_value(value)
    return result


def dotenv_candidates() -> list[Path]:
    """仓库根的 .env 优先，其次当前工作目录的 .env（去重）。"""
    root = Path(__file__).resolve().parents[1]
    candidates = [root / ".env", Path.cwd() / ".env"]
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return seen


def load_dotenv(path: Path | str | None = None, override: bool = True) -> dict[str, str]:
    """加载 .env 到 os.environ，返回本次真正写入的键值（便于日志与排错）。

    path 为空时按 dotenv_candidates() 顺序加载，先到先得。
    override=True（默认）表示 .env 优先于同名进程环境变量。
    """
    paths = [Path(path)] if path is not None else dotenv_candidates()
    loaded: dict[str, str] = {}
    for candidate in paths:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for key, value in parse_env_text(text).items():
            if not override and os.getenv(key) not in (None, ""):
                continue
            if key in loaded:
                continue
            os.environ[key] = value
            loaded[key] = value
    return loaded


def describe_sources(path: Path | str | None = None) -> str:
    """给报错信息用：说明到底查了哪些地方。"""
    paths = [Path(path)] if path is not None else dotenv_candidates()
    parts = []
    for candidate in paths:
        parts.append(f"{candidate}（{'存在' if candidate.is_file() else '不存在'}）")
    return "环境变量，以及 " + "、".join(parts)
