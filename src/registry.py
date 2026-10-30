"""工具注册表的入口：发现 `src/toolbox/` 下的 `@tool`，对外提供 schema / 执行 / 摘要。

发现机制
    `import src.registry` 时用 `pkgutil.iter_modules` 扫 `src.toolbox` 包，逐个 import 模块；
    模块里的 `@tool` 装饰器在导入时完成注册。
    **新增一个动作 = 在 toolbox/ 下写一个函数 + 加一行 @tool**，不用改本文件。

为什么改成装饰器发现
    之前是手写 `TOOL_SPECS` 字典 + `summarize()` 里一串 if/elif + `MockPolicy` 里硬编码工具名，
    新增工具要改三处；漏一处就会出现「模型能调、观测摘要为空」或「mock 路径不认」。
    现在名字、schema、执行、摘要都在 `@tool` 一处声明，注册时还会校验名字字符集
    （点号会在真实模型调用时 400，离线 mock 测不出来）。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from . import toolbox
from .tooling import (  # noqa: F401 - 对外保持 registry 这一个导入入口
    REGISTRY,
    TOOL_NAME_PATTERN,
    ToolContext,
    ToolRegistrationError,
    ToolSpec,
    execute_tool,
    required_schema,
    summarize,
    tool,
)
from .tools import duplicate_materials  # noqa: F401 - 兼容旧的导入路径

_discovered = False


def discover() -> list[str]:
    """扫描并导入 toolbox 下全部模块；重复调用只生效一次。"""
    global _discovered
    if _discovered:
        return sorted(REGISTRY)
    for module in pkgutil.iter_modules(toolbox.__path__, prefix=f"{toolbox.__name__}."):
        importlib.import_module(module.name)
    _discovered = True
    return sorted(REGISTRY)


# 兼容旧名：TOOL_SPECS 就是注册表本身（同一个 dict 对象）
TOOL_SPECS = REGISTRY


def registered_tools() -> list[str]:
    return sorted(REGISTRY)


FINISH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "结束本次任务并给出结论。业务上无法继续时也调用它，而不是编造结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "completed",
                        "needs_confirmation",
                        "needs_input",
                        "blocked",
                        "not_found",
                        "tool_error",
                    ],
                },
                "reason": {"type": "string"},
                "suggested_slot": {"type": "string"},
                "pending": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["status"],
        },
    },
}


def tool_schemas() -> list[dict[str, Any]]:
    """给真实模型用的 tools 参数（含 finish）。"""
    return [spec.as_schema() for spec in REGISTRY.values()] + [FINISH_SCHEMA]


# 模块导入即发现：policy / graph / 测试拿到的都是已注册的清单
discover()
