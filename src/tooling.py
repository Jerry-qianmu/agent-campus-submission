"""工具原语：@tool 装饰器、注册表、执行与摘要。

为什么单独一层
    `registry.py` 负责「发现」（扫包导入），这里负责「一个工具长什么样」。
    toolbox 里的模块只依赖本文件与 tools.py，不会和发现逻辑形成循环导入。

注册时做的三道检查（都是踩过坑加上的）
    1. 名字必须匹配 `^[a-zA-Z0-9_-]+$` —— DeepSeek/OpenAI 的 function name 不允许多余字符，
       点号会在真实调用时 400（离线 mock 测不出来）；
    2. 同名工具不允许覆盖 —— 静默覆盖会让两个模块的行为互相打架；
    3. 必须有 formatter —— 否则观测摘要退化成工具名，决策日志直接不可读。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .store import JsonDatabase
from .tools import Faults, ToolResult

TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ToolRegistrationError(RuntimeError):
    """工具注册不合法：名字不合法、重名、缺 formatter。"""


@dataclass
class ToolContext:
    """一次运行内的工具上下文：数据库、时钟、故障开关、调用计数、已观测到的元数据。"""

    db: JsonDatabase
    now: datetime
    faults: Faults
    rules_version: str
    calls: Counter = field(default_factory=Counter)
    observed_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


Formatter = Callable[[ToolResult], str]


@dataclass(frozen=True)
class ToolSpec:
    """一个可被模型选择的动作。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], ToolResult]
    formatter: Formatter

    def as_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# 全局注册表：@tool 往这里写，registry.discover() 负责把模块导进来触发写入
REGISTRY: dict[str, ToolSpec] = {}


def required_schema(props: dict[str, str]) -> dict[str, Any]:
    """按「字段 -> 类型」生成必填的对象 schema。"""
    return {
        "type": "object",
        "properties": {key: {"type": kind} for key, kind in props.items()},
        "required": list(props),
    }


def tool(
    name: str,
    *,
    description: str,
    parameters: dict[str, Any],
    formatter: Formatter | None = None,
) -> Callable[[Callable[..., ToolResult]], Callable[..., ToolResult]]:
    """把一个函数注册成模型可调用的工具。

    用法：
        @tool(name="material_get", description="...", parameters=required_schema({...}))
        def material_get_tool(ctx, args):
            return material_get(ctx.db, args["material_id"], ctx.faults)
    """

    def decorator(func: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        if not TOOL_NAME_PATTERN.match(name):
            raise ToolRegistrationError(
                f"工具名不合法：{name!r}（只允许字母、数字、下划线、连字符）"
            )
        if name in REGISTRY:
            raise ToolRegistrationError(f"工具重名：{name!r} 已注册，拒绝覆盖")
        if formatter is None:
            raise ToolRegistrationError(f"工具 {name!r} 缺少 formatter，观测摘要会不可读")
        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            handler=func,
            formatter=formatter,
        )
        return func

    return decorator


def summarize(name: str, result: ToolResult) -> str:
    """把工具结果压成一句 observation 摘要，供决策日志与模型下一步使用。"""
    if not result.ok:
        tail = "（可重试）" if result.retryable else ""
        return f"{name} 失败：{result.code} {result.message}{tail}"
    spec = REGISTRY.get(name)
    if spec is not None:
        return spec.formatter(result)
    return name


def execute_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> tuple[ToolResult, str]:
    """执行模型选择的动作，返回（结果, 摘要）。

    模型给的参数不可控：少传、类型错、日期格式怪都见过。这里统一兜住，
    把「模型写坏了参数」降级成一次失败的 observation，而不是让整个进程崩掉。
    """
    from .tools import fail_result

    spec = REGISTRY.get(name)
    if spec is None:
        return fail_result("INVALID_ARGUMENT", f"未知动作：{name}"), f"未知动作：{name}"
    if not isinstance(args, dict):
        return fail_result("INVALID_ARGUMENT", "动作参数必须是 JSON 对象"), "参数不是对象"
    missing = [key for key in spec.parameters.get("required", []) if key not in args]
    if missing:
        return (
            fail_result("INVALID_ARGUMENT", f"{name} 缺少参数：{', '.join(missing)}"),
            f"{name} 参数不足：{', '.join(missing)}",
        )
    try:
        result = spec.handler(ctx, args)
    except (ValueError, TypeError, KeyError) as exc:
        return (
            fail_result("INVALID_ARGUMENT", f"{name} 参数不合法：{exc}"),
            f"{name} 参数不合法：{exc}",
        )
    except Exception as exc:  # noqa: BLE001 - 兜底：工具内部异常不能击穿循环
        return (
            fail_result("TOOL_CRASH", f"{name} 执行异常：{exc}"),
            f"{name} 执行异常：{exc}",
        )
    return result, summarize(name, result)


def clear_registry() -> None:
    """仅供测试：清空注册表后重新 discover。"""
    REGISTRY.clear()
