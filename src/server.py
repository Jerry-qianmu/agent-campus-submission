"""本地 Web 应用：把 ReAct 排期流程包成 HTTP API + 单页界面。

为什么不用 FastAPI / Flask
    这个项目的卖点之一是"clone 下来就能跑"：`requirements.txt` 只有 langgraph。
    界面是给人看的，不值得为它再引一个框架。`http.server` + `ThreadingHTTPServer`
    足够支撑本地单用户场景，且零安装。

接口
    GET  /                 单页界面
    GET  /api/bootstrap    素材库 + 已有排期 + 默认日期 + provider 状态
    GET  /api/slots?date=  某天的时段占用（给日期/时间选择器用）
    POST /api/run          跑一次 ReAct 流程，返回与 CLI 完全一致的结果 JSON
    POST /api/confirm      确认草稿（DRAFT → CONFIRMED，仍是草稿、不发布）
    POST /api/reset        把运行库重置回种子数据（演示用）

启动
    python -m src.server                 # http://127.0.0.1:8765
    python -m src.server --port 9000 --provider deepseek
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .clients import ChatError
from .env import load_dotenv
from .errors import AgentError, InvalidInput
from .graph import MAX_STEPS_DEFAULT, run_request
from .repository import Repository
from .store import JsonDatabase
from .tools import schedule_confirm, slot_query
from .util import TZ, parse_datetime, to_iso

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
SEED = ROOT / "data" / "seed.json"

ALLOWED_PROVIDERS = ("mock", "deepseek")
ALLOWED_CONSTRAINTS = ("hard", "soft")
ALLOWED_FAULTS = {
    "none": None,
    "material_lookup": {"material_lookup": True},
    "save_failure": {"save_failure": "db_unavailable"},
    "notify_failure": {"notify_failure": True},
}


class App:
    """不依赖 HTTP 的业务门面：单测可以直接调用，不用起服务器。"""

    def __init__(
        self,
        db_path: Path,
        seed_path: Path = SEED,
        provider: str | None = None,
        now: datetime | None = None,
        max_steps: int = MAX_STEPS_DEFAULT,
    ) -> None:
        self.db = JsonDatabase(db_path, seed_path=seed_path)
        self.provider = provider
        self.now = now or datetime.now(TZ)
        self.max_steps = max_steps

    # -- 读 ----------------------------------------------------------------
    def bootstrap(self) -> dict[str, Any]:
        self.db.initialize()
        with self.db.transaction() as session:
            materials = [dict(row) for row in session.select("materials")]
            reviews = {row["material_id"]: row for row in session.select("material_review")}
            schedules = [dict(row) for row in session.select("publish_schedule")]

        items = []
        for material in materials:
            metadata = material.get("metadata") or {}
            review = reviews.get(material["id"]) or {}
            items.append(
                {
                    "id": material["id"],
                    "title": metadata.get("title") or "(无标题)",
                    "duration_sec": metadata.get("duration_sec"),
                    "tags": metadata.get("tags") or [],
                    "cover_url": metadata.get("cover_url"),
                    "audit_status": review.get("status") or "PENDING",
                    "risk_reason": review.get("risk_reason"),
                    "release_date": review.get("release_date"),
                }
            )

        dates = sorted({row["date"][:10] for row in schedules})
        suggested_date = dates[0] if dates else (self.now + timedelta(days=1)).strftime("%Y-%m-%d")
        return {
            "materials": items,
            "schedules": [
                {
                    "id": row["id"],
                    "date": row["date"],
                    "material_id": row["material_id"],
                    "status": row["status"],
                }
                for row in schedules
            ],
            "suggested_date": suggested_date,
            "provider": self.provider or "mock",
            "deepseek_ready": bool(self._env("DEEPSEEK_API_KEY") or self._env("LLM_API_KEY")),
            "now": to_iso(self.now),
            "max_steps": self.max_steps,
        }

    def slots(self, date: str) -> dict[str, Any]:
        moment = parse_datetime(f"{date}T00:00:00+08:00")
        result = slot_query(self.db, moment, self.now)
        if not result.ok:
            raise InvalidInput(result.message or "时段查询失败")
        return {
            "date": date,
            "slots": result.data["slots"],
            "free": result.data["free"],
            "occupied": result.data["occupied"],
        }

    # -- 写 ----------------------------------------------------------------
    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self._provider(payload.get("provider"))
        material_id = str(payload.get("material_id") or "").strip().upper()
        if not material_id:
            raise InvalidInput("请选择素材")

        date = str(payload.get("date") or "").strip()
        hour = payload.get("hour")
        desired_slot = None
        if date and hour not in (None, ""):
            hour_int = int(hour)
            if not 0 <= hour_int <= 23:
                raise InvalidInput("时间必须是 0-23 的整点")
            desired_slot = to_iso(parse_datetime(f"{date}T{hour_int:02d}:00:00+08:00"))
        elif date:
            # 只选了日期没选时间：不报错，交给 Agent 提议并标待确认
            pass

        constraint = payload.get("constraint") or "soft"
        if constraint not in ALLOWED_CONSTRAINTS:
            raise InvalidInput("时间约束只能是 hard 或 soft")

        simulate_key = payload.get("simulate") or "none"
        simulate = ALLOWED_FAULTS.get(simulate_key, None)
        if simulate_key not in ALLOWED_FAULTS:
            raise InvalidInput("未知的故障注入项")

        max_steps = int(payload.get("max_steps") or self.max_steps)
        if not 1 <= max_steps <= 20:
            raise InvalidInput("步数上限需在 1-20 之间")

        request: dict[str, Any] = {
            "request_id": f"web-{int(time.time() * 1000)}",
            "material_id": material_id,
            "slot_constraint": constraint,
            "max_steps": max_steps,
            "now": to_iso(self.now),
        }
        if desired_slot:
            request["desired_slot"] = desired_slot
        user_text = str(payload.get("user_text") or "").strip()
        if user_text:
            request["user_text"] = user_text
        if simulate:
            request["simulate"] = simulate

        return run_request(
            request,
            self.db,
            now=self.now,
            provider=provider,
            max_steps=max_steps,
        )

    def confirm(self, draft_id: int) -> dict[str, Any]:
        result = schedule_confirm(self.db, int(draft_id))
        if not result.ok:
            raise InvalidInput(result.message or "确认失败")
        return {"ok": True, "draft": result.data["draft"]}

    def reset(self) -> dict[str, Any]:
        for suffix in ("", ".lock", ".tmp"):
            path = Path(str(self.db.path) + suffix)
            if path.exists():
                path.unlink()
        self.db.initialize()
        return {"ok": True}

    # -- 内部 --------------------------------------------------------------
    def _provider(self, requested: Any) -> str:
        resolved = str(requested or self.provider or "mock").strip().lower()
        if resolved not in ALLOWED_PROVIDERS:
            raise InvalidInput(f"provider 只能是 {'/'.join(ALLOWED_PROVIDERS)}")
        return resolved

    @staticmethod
    def _env(key: str) -> str | None:
        return os.getenv(key)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "ScheduleAgent/1.0"
    app: App  # 由 serve() 注入

    # -- 路由 --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - 标准库约定
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                return self._static("index.html", "text/html; charset=utf-8")
            if path in ("/app.js", "/app.css"):
                content_type = (
                    "application/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                )
                return self._static(path.lstrip("/"), content_type)
            if path == "/api/health":
                return self._send(200, {"ok": True, "now": to_iso(self.app.now)})
            if path == "/api/bootstrap":
                return self._send(200, self.app.bootstrap())
            if path == "/api/slots":
                query = parse_qs(urlparse(self.path).query)
                date = (query.get("date") or [""])[0]
                if not date:
                    raise InvalidInput("缺少 date 参数")
                return self._send(200, self.app.slots(date))
            return self._send(404, {"error": "not found"})
        except (InvalidInput, AgentError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 本地应用，返回可读错误即可
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/run":
                return self._send(200, self.app.run(payload))
            if path == "/api/confirm":
                draft_id = payload.get("draft_id")
                if draft_id in (None, ""):
                    raise InvalidInput("缺少 draft_id")
                return self._send(200, self.app.confirm(int(draft_id)))
            if path == "/api/reset":
                return self._send(200, self.app.reset())
            return self._send(404, {"error": "not found"})
        except ChatError as exc:
            self._send(400, {"error": str(exc), "code": exc.code, "retryable": exc.retryable})
        except (InvalidInput, AgentError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- 工具 --------------------------------------------------------------
    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidInput(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise InvalidInput("请求体必须是 JSON 对象")
        return payload

    def _static(self, name: str, content_type: str) -> None:
        path = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in path.parents or not path.is_file():
            return self._send(404, {"error": "not found"})
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self, "quiet", False):
            return
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    db_path: Path | None = None,
    provider: str | None = None,
    now: datetime | None = None,
    open_browser: bool = True,
) -> None:
    app = App(
        db_path=db_path or (ROOT / "var" / "web-db.json"),
        provider=provider,
        now=now,
    )
    app.db.initialize()
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"[web] 排期 Agent 界面已启动：{url}")
    print(f"[web] 数据库：{app.db.path}（可用界面上的「重置数据」回到种子状态）")
    print("[web] Ctrl+C 停止")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - 无桌面环境时忽略
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 已停止")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址，默认仅本机")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", help="运行库路径，默认 var/web-db.json")
    parser.add_argument("--provider", choices=ALLOWED_PROVIDERS, help="默认 mock；界面里也能切")
    parser.add_argument("--now", help="注入当前时间（ISO 8601），便于复现")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    load_dotenv()
    now = parse_datetime(args.now) if args.now else None
    try:
        serve(
            host=args.host,
            port=args.port,
            db_path=Path(args.db) if args.db else None,
            provider=args.provider,
            now=now,
            open_browser=not args.no_open,
        )
    except OSError as exc:
        print(f"[web] 启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
