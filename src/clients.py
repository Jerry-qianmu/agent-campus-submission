"""模型客户端：DeepSeek（OpenAI 兼容）的真实 HTTP 实现。

为什么单独一层
    policy/llm 只依赖 ChatClient 协议，真实 HTTP、超时、重试、错误码翻译都收在这里。
    这样换厂商只改本文件，换决策逻辑只改 policy，两个维度不会互相牵制。

依赖的 DeepSeek 事实（2026-10 文档）
    POST https://api.deepseek.com/chat/completions
    model: deepseek-flash / deepseek-v4-pro
    tools/tool_choice: 支持 function calling；思考模式下不允许 required 或指定具体 tool（会 400）
    thinking / reasoning_effort: 省略时默认开启思考模式，所以要"不开"必须显式传 disabled
    reasoning_content: 思考模式下返回的推理内容，回传时不要带回去
    错误码: 400 格式 / 401 认证 / 402 余额 / 422 参数 / 429 限速 / 500 故障 / 503 繁忙

重试策略
    只重试 408/409/429/5xx 与网络超时，指数退避（0.5s 起，上限 8s），最多 2 次；
    400/401/402/422 一律不重试——重试只会把同一个错误再花钱跑一遍。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .env import describe_sources
from .errors import AgentError, InvalidInput

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class ChatError(AgentError):
    """模型调用失败。retryable 决定上层是重试还是直接收手。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "UNKNOWN",
        status: int | None = None,
        retryable: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "status": self.status,
            "retryable": self.retryable,
        }


@dataclass
class HttpResponse:
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""


# transport(url, headers, body) -> HttpResponse | str
# 允许直接返回字符串，是为了让测试可以只关心返回体；真实实现返回 HttpResponse。
Transport = Callable[..., Any]


def _urllib_transport(url: str, headers: dict[str, str], body: bytes) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return HttpResponse(
                status=getattr(response, "status", 200),
                text=response.read().decode("utf-8"),
                headers=dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要把 body 读出来
        return HttpResponse(
            status=exc.code,
            text=exc.read().decode("utf-8", errors="replace"),
            headers=dict((exc.headers or {}).items()),
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise ChatError(f"网络错误：{exc}", code="NETWORK", retryable=True) from exc


class ChatClient(Protocol):
    name: str
    model: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = 0.0,
    ) -> ChatResult: ...


class OpenAICompatibleClient:
    """OpenAI 兼容实现；DeepSeek 直接继承它。"""

    name = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        backoff: float = 0.5,
        max_backoff: float = 8.0,
    ) -> None:
        if not api_key:
            raise InvalidInput(f"{self.name} 需要 API key")
        self.api_key = api_key
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.transport = transport or _urllib_transport
        self.sleeper = sleeper
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.last_usage: dict[str, int] = {}

    # -- 公开接口 --------------------------------------------------------
    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = 0.0,
    ) -> ChatResult:
        payload = self.build_payload(messages, tools, tool_choice, response_format, temperature)
        result = self.parse(self._post(payload))
        self.last_usage = dict(result.usage)
        return result

    # -- 报文 ------------------------------------------------------------
    def build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if response_format:
            payload["response_format"] = response_format
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    # -- 带重试的请求 ----------------------------------------------------
    def _post(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            try:
                response = self._call(body)
            except ChatError as error:
                if error.retryable and attempt < self.max_retries:
                    self._wait(attempt, None)
                    attempt += 1
                    continue
                raise

            if response.status == 200:
                return response.text

            error = self._error_from(response)
            if error.retryable and attempt < self.max_retries:
                self._wait(attempt, response.headers)
                attempt += 1
                continue
            raise error

    def _call(self, body: bytes) -> HttpResponse:
        try:
            raw = self.transport(self.endpoint, self.headers(), body)
        except ChatError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise ChatError(f"请求超时：{exc}", code="TIMEOUT", retryable=True) from exc
        except urllib.error.URLError as exc:
            raise ChatError(f"网络错误：{exc.reason}", code="NETWORK", retryable=True) from exc
        except OSError as exc:
            raise ChatError(f"网络错误：{exc}", code="NETWORK", retryable=True) from exc
        if isinstance(raw, str):
            return HttpResponse(status=200, text=raw)
        if isinstance(raw, dict):
            return HttpResponse(status=raw.get("status", 200), text=raw.get("text", ""))
        return raw

    def _wait(self, attempt: int, headers: dict[str, str] | None) -> None:
        delay = min(self.max_backoff, self.backoff * (2**attempt))
        retry_after = None
        for key, value in (headers or {}).items():
            if key.lower() == "retry-after":
                retry_after = value
                break
        if retry_after:
            try:
                delay = min(self.max_backoff, max(delay, float(retry_after)))
            except ValueError:
                pass
        self.sleeper(delay)

    # -- 错误翻译 --------------------------------------------------------
    def _error_from(self, response: HttpResponse) -> ChatError:
        detail = self._error_detail(response.text)
        status = response.status
        mapping = {
            400: ("BAD_REQUEST", False),
            401: ("AUTH", False),
            402: ("INSUFFICIENT_BALANCE", False),
            403: ("FORBIDDEN", False),
            404: ("NOT_FOUND", False),
            422: ("INVALID_PARAMETERS", False),
            429: ("RATE_LIMITED", True),
            500: ("SERVER_ERROR", True),
            503: ("SERVER_BUSY", True),
        }
        code, retryable = mapping.get(status, (f"HTTP_{status}", status in RETRYABLE_STATUS))
        message = f"{self.name} 调用失败：HTTP {status}"
        if detail:
            message = f"{message} - {detail}"
        return ChatError(message, code=code, status=status, retryable=retryable, detail=detail)

    @staticmethod
    def _error_detail(text: str) -> str | None:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return (text or "").strip()[:300] or None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:300]
            if isinstance(error, str):
                return error[:300]
        return None

    # -- 响应解析 --------------------------------------------------------
    def parse(self, text: str) -> ChatResult:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ChatError(
                "模型响应不是合法 JSON", code="BAD_RESPONSE", retryable=False
            ) from exc
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatError(
                "模型响应缺少 choices/message", code="BAD_RESPONSE", retryable=False
            ) from exc

        tool_calls: list[dict[str, Any]] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            tool_calls.append(
                {
                    "id": call.get("id") or f"call_{index}",
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments") or "{}",
                }
            )

        usage = {
            key: value
            for key, value in (data.get("usage") or {}).items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        return ChatResult(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            reasoning=message.get("reasoning_content"),
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            model=data.get("model") or self.model,
        )


class DeepSeekClient(OpenAICompatibleClient):
    """DeepSeek 官方接口。思考模式默认关闭：工具调用场景下更快更稳。"""

    name = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-flash"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        *,
        thinking: str = "disabled",
        reasoning_effort: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, base_url, model, **kwargs)
        self.thinking = thinking if thinking in ("enabled", "disabled", "auto") else "disabled"
        self.reasoning_effort = reasoning_effort

    def build_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = super().build_payload(*args, **kwargs)
        payload.update(self.thinking_payload())
        if self.thinking == "enabled":
            # 思考模式忽略 temperature，显式去掉以免误导
            payload.pop("temperature", None)
        return payload

    def thinking_payload(self) -> dict[str, Any]:
        if self.reasoning_effort:
            return {"reasoning_effort": self.reasoning_effort}
        if self.thinking in ("enabled", "disabled"):
            return {"thinking": {"type": self.thinking}}
        return {}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise InvalidInput(f"{name} 必须是数字") from exc


def build_client(provider: str | None = None, model: str | None = None) -> ChatClient | None:
    """按 provider 造客户端；mock 返回 None（调用方用 MockPolicy / MockLLM）。"""
    resolved = (provider or os.environ.get("LLM_PROVIDER") or "mock").strip().lower()

    if resolved in ("mock", "offline"):
        return None

    if resolved == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
        if not api_key:
            raise InvalidInput(
                "LLM_PROVIDER=deepseek 需要 DEEPSEEK_API_KEY。已查找："
                + describe_sources()
                + "。可用 --env-file 指定文件，或 --provider mock 先离线跑"
            )
        return DeepSeekClient(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
            model=model or os.environ.get("DEEPSEEK_MODEL") or None,
            thinking=(os.environ.get("DEEPSEEK_THINKING") or "disabled").strip().lower(),
            reasoning_effort=os.environ.get("DEEPSEEK_REASONING_EFFORT") or None,
            timeout=_env_float("DEEPSEEK_TIMEOUT", 60.0),
            max_retries=int(_env_float("DEEPSEEK_MAX_RETRIES", 2)),
        )

    if resolved in ("http", "openai"):
        api_key = os.environ.get("LLM_API_KEY")
        base_url = os.environ.get("LLM_BASE_URL")
        if not (api_key and base_url):
            raise InvalidInput("LLM_PROVIDER=http 需要同时设置 LLM_BASE_URL 与 LLM_API_KEY")
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=base_url,
            model=model or os.environ.get("LLM_MODEL"),
        )

    raise InvalidInput(f"未知的 LLM_PROVIDER：{resolved}（可选 mock / deepseek / http）")
