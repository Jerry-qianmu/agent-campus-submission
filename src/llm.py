"""意图解析层：把「用户怎么说」翻译成结构化意图，只做翻译不做判定。

两种实现
    MockLLM  默认。纯规则解析，确定性、可单测，用于本仓库的离线验收。
    HttpLLM  可选。调用 OpenAI 兼容的 chat/completions，返回同样的 Intent。

为什么把「模型」单独关在这一层
    模型唯一被允许做的事就是理解自然语言；审核结论、时段占用、幂等全部在 rules/tools 里。
    这样即便换模型、模型抽风，业务判定也不会漂移。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

from .errors import InvalidInput
from .util import TZ, floor_hour, parse_datetime, to_iso

HARD_WORDS = ("必须", "只能", "务必", "一定要", "不得调整", "不能改")
SOFT_WORDS = ("希望", "最好", "建议", "尽量", "如果可以")

MATERIAL_PATTERN = re.compile(r"\b([Mm]\d{2,})\b")
TIME_PATTERN = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
DATE_PATTERN = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


@dataclass(frozen=True)
class Intent:
    material_id: str | None
    desired_slot: str | None
    slot_constraint: str  # hard / soft
    source: str  # structured / user_text
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "desired_slot": self.desired_slot,
            "slot_constraint": self.slot_constraint,
            "source": self.source,
            "notes": list(self.notes),
        }


class LLMClient(Protocol):
    name: str

    def parse_intent(self, request: dict[str, Any], now: datetime) -> Intent: ...


def _clean_material_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InvalidInput("material_id 必须是字符串")
    return value.strip().upper() or None


def _combine(target_date: str, hour: int, minute: int) -> str:
    moment = datetime(
        year=int(target_date[0:4]),
        month=int(target_date[5:7]),
        day=int(target_date[8:10]),
        hour=hour,
        minute=minute,
        tzinfo=TZ,
    )
    return to_iso(floor_hour(moment))


class MockLLM:
    """规则版意图解析：结构化字段优先，缺什么从 user_text 里补。"""

    name = "mock"

    def parse_intent(self, request: dict[str, Any], now: datetime) -> Intent:
        if not isinstance(request, dict):
            raise InvalidInput("input must be a JSON object")
        notes: list[str] = []

        material_id = _clean_material_id(request.get("material_id"))
        user_text = request.get("user_text")
        if user_text is not None and not isinstance(user_text, str):
            raise InvalidInput("user_text 必须是字符串")

        source = "structured"
        if material_id is None and user_text:
            found = MATERIAL_PATTERN.search(user_text)
            if found:
                material_id = found.group(1).upper()
                source = "user_text"
                notes.append("素材ID 来自自然语言")

        desired = request.get("desired_slot")
        if desired is not None and not isinstance(desired, str):
            raise InvalidInput("desired_slot 必须是 ISO 8601 字符串")
        slot: str | None = None
        if desired:
            try:
                slot = to_iso(floor_hour(parse_datetime(desired)))
            except ValueError as exc:
                raise InvalidInput(f"desired_slot 无法解析：{desired}") from exc
        elif user_text:
            slot = self._slot_from_text(user_text, request.get("target_date"), now)
            if slot:
                source = "user_text" if source != "structured" else source
                notes.append("时段来自自然语言")

        constraint = request.get("slot_constraint")
        if constraint is not None and constraint not in ("hard", "soft"):
            raise InvalidInput("slot_constraint 只能是 hard 或 soft")
        if constraint is None:
            constraint = self._constraint_from_text(user_text or "", request)
            if constraint == "hard":
                notes.append("识别为硬性时间要求")

        return Intent(
            material_id=material_id,
            desired_slot=slot,
            slot_constraint=constraint,
            source=source,
            notes=notes,
        )

    @staticmethod
    def _slot_from_text(text: str, target_date: Any, now: datetime) -> str | None:
        found_time = TIME_PATTERN.search(text)
        if not found_time:
            return None
        hour, minute = int(found_time.group(1)), int(found_time.group(2))
        if hour > 23 or minute > 59:
            raise InvalidInput(f"user_text 中的时间不合法：{found_time.group(0)}")
        date_text = target_date if isinstance(target_date, str) else None
        found_date = DATE_PATTERN.search(text)
        if found_date:
            date_text = f"{int(found_date.group(1)):04d}-{int(found_date.group(2)):02d}-{int(found_date.group(3)):02d}"
        if date_text is None:
            date_text = to_iso(now)[:10]
        return _combine(date_text, hour, minute)

    @staticmethod
    def _constraint_from_text(text: str, request: dict[str, Any]) -> str:
        if not text:
            return "soft"
        if any(word in text for word in HARD_WORDS):
            return "hard"
        if any(word in text for word in SOFT_WORDS):
            return "soft"
        return "soft"


Transport = Callable[[str, dict[str, str], bytes], str]


def _urllib_transport(url: str, headers: dict[str, str], body: bytes) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - 仅访问自配地址
        return resp.read().decode("utf-8")


class HttpLLM:
    """OpenAI 兼容实现。默认不启用；仓库内未做端到端联调（无密钥）。"""

    name = "http"

    SYSTEM_PROMPT = (
        "你是素材发布排期的意图解析器。只输出 JSON，不要解释。字段："
        "material_id（字符串或 null）、desired_slot（ISO 8601 或 null）、"
        "slot_constraint（hard 或 soft）。用户语气是「必须/只能」时为 hard。"
    )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.transport = transport or _urllib_transport

    def parse_intent(self, request: dict[str, Any], now: datetime) -> Intent:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "now": to_iso(now),
                            "user_text": request.get("user_text"),
                            "structured": {
                                "material_id": request.get("material_id"),
                                "desired_slot": request.get("desired_slot"),
                                "slot_constraint": request.get("slot_constraint"),
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        raw = self.transport(
            f"{self.base_url}/chat/completions", headers, body
        )
        return self._parse_response(raw, request, now)

    def _parse_response(
        self, raw: str, request: dict[str, Any], now: datetime
    ) -> Intent:
        try:
            content = json.loads(raw)["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidInput(f"模型返回不是合法 JSON：{exc}") from exc

        material_id = _clean_material_id(parsed.get("material_id")) or _clean_material_id(
            request.get("material_id")
        )
        slot = parsed.get("desired_slot")
        if slot:
            slot = to_iso(floor_hour(parse_datetime(slot)))
        else:
            slot = request.get("desired_slot")
        constraint = parsed.get("slot_constraint")
        if constraint not in ("hard", "soft"):
            constraint = "soft"
        return Intent(
            material_id=material_id,
            desired_slot=slot,
            slot_constraint=constraint,
            source="model",
            notes=["由模型解析"],
        )


def get_llm() -> LLMClient:
    provider = (os.environ.get("LLM_PROVIDER") or "mock").strip().lower()
    if provider != "http":
        return MockLLM()
    base_url = os.environ.get("LLM_BASE_URL")
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if not (base_url and api_key and model):
        raise InvalidInput("LLM_PROVIDER=http 需要同时配置 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL")
    return HttpLLM(base_url=base_url, api_key=api_key, model=model)
