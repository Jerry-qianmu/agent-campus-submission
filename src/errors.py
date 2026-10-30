"""领域异常。业务异常最终都要落到 JSON 的错误码上，不向调用方抛栈。"""

from __future__ import annotations


class AgentError(Exception):
    """所有可预期异常的基类。"""


class InvalidInput(ValueError):
    """入参不是合法的请求对象（类型错误/不可解析），对应非零退出码。"""


class LockTimeout(AgentError):
    """等待数据库文件锁超时。"""


class Conflict(AgentError):
    """与现有数据/状态冲突（内容重复、已有排期等），HTTP 层映射为 409。"""


class UniqueViolation(AgentError):
    """违反唯一约束，语义等价于 MySQL 的 Duplicate entry。"""

    def __init__(self, table: str, columns: tuple[str, ...], value: object) -> None:
        self.table = table
        self.columns = columns
        self.value = value
        super().__init__(f"{table}: duplicate entry for {columns} = {value!r}")
