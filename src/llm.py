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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .clients import ChatClient, build_client
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


class HttpLLM:
    """真实模型做意图解析：一次 chat 调用 + JSON 输出，client 负责重试与错误翻译。"""

    name = "llm"

    SYSTEM_PROMPT = (
        "你是素材发布排期的意图解析器。只输出 JSON，不要解释，也不要输出推理过程。字段："
        "material_id（字符串或 null）、desired_slot（ISO 8601 或 null）、"
        "slot_constraint（hard 或 soft）。用户语气是「必须/只能」时为 hard。"
        "拿不准的字段一律给 null，不要编造。"
    )

    def __init__(self, client: ChatClient) -> None:
        self.client = client
        self.model = getattr(client, "model", "")
        self.name = getattr(client, "name", "llm")

    def parse_intent(self, request: dict[str, Any], now: datetime) -> Intent:
        messages = [
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
        ]
        result = self.client.chat(
            messages, response_format={"type": "json_object"}, temperature=0.0
        )
        return self._parse_content(result.content, request)

    def _parse_content(self, content: str, request: dict[str, Any]) -> Intent:
        try:
            parsed = json.loads(_strip_code_fence(content))
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidInput(f"模型返回不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise InvalidInput("模型返回的意图必须是 JSON 对象")

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


def _strip_code_fence(text: str) -> str:
    """模型偶尔会包一层 ```json ... ```，这里兜底剥掉。"""
    body = (text or "").strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def get_llm(provider: str | None = None, model: str | None = None) -> LLMClient:
    """按 provider 选择意图解析实现；mock 时用规则解析，不产生任何网络请求。"""
    resolved = (provider or os.environ.get("LLM_PROVIDER") or "mock").strip().lower()
    if resolved in ("mock", "offline"):
        return MockLLM()
    client = build_client(resolved, model)
    if client is None:  # pragma: no cover - build_client 在非 mock 时不会返回 None
        return MockLLM()
    return HttpLLM(client)
