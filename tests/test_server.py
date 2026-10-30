import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.server import App, Handler
from support import NOW


class AppTests(unittest.TestCase):
    """不起服务器，直接测业务门面。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name) / "db.json", now=NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bootstrap_lists_materials_and_date(self):
        data = self.app.bootstrap()
        self.assertEqual(len(data["materials"]), 10)
        self.assertEqual(data["suggested_date"], "2026-11-05")
        self.assertEqual(data["provider"], "mock")
        self.assertEqual(data["max_steps"], 10)

    def test_slots_marks_seed_occupancy(self):
        data = self.app.slots("2026-11-05")
        occupied = data["occupied"]
        self.assertEqual(occupied.get("2026-11-05T18:00:00+08:00"), "M00")

    def test_run_conflict_saves_draft(self):
        result = self.app.run(
            {
                "material_id": "M01",
                "date": "2026-11-05",
                "hour": 18,
                "constraint": "soft",
                "provider": "mock",
            }
        )
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["suggested_slot"], "2026-11-05T19:00:00+08:00")
        self.assertTrue(result["draft"]["saved"])
        self.assertFalse(result["published"])
        self.assertEqual(result["decisions"][0]["action"], "material_get")

    def test_hard_constraint_does_not_save(self):
        result = self.app.run(
            {
                "material_id": "M01",
                "date": "2026-11-05",
                "hour": 18,
                "constraint": "hard",
            }
        )
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["draft"]["saved"])

    def test_confirm_flips_status_but_stays_draft(self):
        result = self.app.run({"material_id": "M02", "date": "2026-11-05", "hour": 20})
        draft_id = result["draft"]["draft_id"]
        confirmed = self.app.confirm(draft_id)
        self.assertEqual(confirmed["draft"]["status"], "CONFIRMED")
        self.assertIsNotNone(confirmed["draft"]["confirm_time"])
        self.assertNotEqual(confirmed["draft"]["status"], "PUBLISHED")

    def test_reset_restores_seed(self):
        self.app.run({"material_id": "M02", "date": "2026-11-05", "hour": 20})
        self.app.reset()
        data = self.app.bootstrap()
        self.assertEqual(len(data["schedules"]), 1)

    def test_invalid_provider_rejected(self):
        from src.errors import InvalidInput

        with self.assertRaises(InvalidInput):
            self.app.run({"material_id": "M01", "provider": "openai"})

    def test_missing_hour_means_no_slot(self):
        result = self.app.run({"material_id": "M02", "date": "2026-11-05"})
        # 没给时间 → Agent 自己挑一个并标待确认
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertTrue(result["pending_confirmations"])

    # -- 草稿箱与素材详情 ------------------------------------------------
    def test_drafts_lists_all_rows(self):
        self.app.run({"material_id": "M01", "date": "2026-11-05", "hour": 18})
        data = self.app.drafts()
        self.assertEqual(len(data["drafts"]), 2)  # 种子 M00 + 新草稿
        self.assertEqual(data["counts"]["DRAFT"], 2)
        self.assertEqual(data["published_total"], 0)
        self.assertEqual(data["conflicts"], {})

    def test_material_detail_without_schedule(self):
        data = self.app.material_detail("M02")
        self.assertEqual(data["id"], "M02")
        self.assertFalse(data["has_schedule"])
        self.assertEqual(data["schedules"], [])
        self.assertEqual(data["review"]["status"], "PENDING")

    def test_material_detail_reports_seed_schedule(self):
        data = self.app.material_detail("M00")
        self.assertTrue(data["has_schedule"])
        self.assertEqual(data["schedules"][0]["date"], "2026-11-05T18:00:00+08:00")
        self.assertEqual(data["schedules"][0]["status"], "DRAFT")
        self.assertEqual(data["conflict_count"], 0)

    def test_material_detail_exposes_fingerprint_and_duplicates(self):
        """M08/M09 是同一份文件：两者互相标为重复，且指纹一致。"""
        data = self.app.material_detail("M08")
        self.assertEqual(data["duplicate_of"], ["M09"])
        self.assertEqual(len(data["content_hash"]), 64)
        mirror = self.app.material_detail("M09")
        self.assertEqual(mirror["duplicate_of"], ["M08"])
        self.assertEqual(mirror["content_hash"], data["content_hash"])

        clean = self.app.material_detail("M01")
        self.assertEqual(clean["duplicate_of"], [])

    def test_conflict_scan_detects_duplicate_slot(self):
        """容量是应用层校验，库里没有跨素材唯一键；直接插两条应被扫描出来。"""
        with self.app.db.transaction() as session:
            session.insert(
                "publish_schedule",
                {
                    "date": "2026-11-05T18:00:00+08:00",
                    "material_id": "M05",
                    "status": "DRAFT",
                    "confirm_time": None,
                    "idem_key": None,
                },
            )
        data = self.app.drafts()
        self.assertEqual(data["conflicts"]["2026-11-05T18:00:00+08:00"], ["M00", "M05"])
        detail = self.app.material_detail("M00")
        self.assertEqual(detail["conflict_count"], 1)
        self.assertEqual(detail["schedules"][0]["conflict_with"], ["M05"])

    def test_cancel_frees_the_slot(self):
        result = self.app.run({"material_id": "M02", "date": "2026-11-05", "hour": 20})
        draft_id = result["draft"]["draft_id"]
        cancelled = self.app.cancel(draft_id)
        self.assertEqual(cancelled["draft"]["status"], "CANCELLED")
        occupied = self.app.slots("2026-11-05")["occupied"]
        self.assertNotIn("2026-11-05T20:00:00+08:00", occupied)

    def test_cannot_cancel_confirmed_draft(self):
        from src.errors import InvalidInput

        result = self.app.run({"material_id": "M02", "date": "2026-11-05", "hour": 20})
        draft_id = result["draft"]["draft_id"]
        self.app.confirm(draft_id)
        with self.assertRaises(InvalidInput):
            self.app.cancel(draft_id)

    # -- 素材增删改查 ----------------------------------------------------
    def test_create_material_runs_audit(self):
        created = self.app.create_material(
            {
                "title": "新品开箱-便携榨汁机",
                "file_url": "https://cdn.demo/video/m10.mp4",
                "cover_url": "https://cdn.demo/cover/m10.jpg",
                "format": "mp4",
                "duration_sec": 36,
                "copyright": "original",
                "tags": "家电, 开箱",
            }
        )
        self.assertEqual(created["id"], "M10")
        self.assertEqual(created["review"]["status"], "PASSED")
        self.assertEqual(created["metadata"]["tags"], ["家电", "开箱"])
        ids = [item["id"] for item in self.app.bootstrap()["materials"]]
        self.assertIn("M10", ids)

    def test_create_rejects_duplicate_content(self):
        from src.errors import Conflict

        self.app.create_material(
            {"title": "A", "file_url": "https://cdn.demo/video/m10.mp4", "duration_sec": 30}
        )
        with self.assertRaises(Conflict) as caught:
            self.app.create_material(
                {"title": "B", "file_url": "https://cdn.demo/video/m10.mp4", "duration_sec": 30}
            )
        self.assertIn("M10", str(caught.exception))

    def test_create_requires_title_and_valid_duration(self):
        from src.errors import InvalidInput

        with self.assertRaises(InvalidInput):
            self.app.create_material({"title": "  "})
        with self.assertRaises(InvalidInput):
            self.app.create_material({"title": "x", "duration_sec": 0})
        with self.assertRaises(InvalidInput):
            self.app.create_material({"title": "x", "duration_sec": "三十秒"})

    def test_update_merges_and_reaudits(self):
        created = self.app.create_material(
            {
                "title": "待改",
                "file_url": "https://cdn.demo/video/m10.mp4",
                "cover_url": "https://cdn.demo/cover/m10.jpg",
                "duration_sec": 30,
                "format": "mp4",
            }
        )
        updated = self.app.update_material(created["id"], {"duration_sec": 42})
        self.assertEqual(updated["title"], "待改")  # 没传的字段保留
        self.assertEqual(updated["metadata"]["duration_sec"], 42)
        self.assertTrue(updated["metadata"]["cover_url"])
        self.assertEqual(updated["review"]["status"], "PASSED")

        cleared = self.app.update_material(created["id"], {"cover_url": ""})
        self.assertIsNone(cleared["metadata"].get("cover_url"))  # 传空 = 清除
        self.assertEqual(cleared["review"]["status"], "INCOMPLETE")

    def test_update_blocks_when_material_has_schedule(self):
        from src.errors import Conflict

        self.app.run({"material_id": "M01", "date": "2026-11-05", "hour": 18})
        with self.assertRaises(Conflict):
            self.app.update_material("M01", {"title": "改个名"})

    def test_delete_blocks_when_scheduled_then_succeeds_after_cancel(self):
        from src.errors import Conflict, InvalidInput

        result = self.app.run({"material_id": "M02", "date": "2026-11-05", "hour": 20})
        with self.assertRaises(Conflict):
            self.app.delete_material("M02")

        self.app.cancel(result["draft"]["draft_id"])
        deleted = self.app.delete_material("M02")
        self.assertEqual(deleted["deleted"], "M02")
        self.assertEqual(deleted["removed_schedules"], 1)
        self.assertNotIn("M02", [item["id"] for item in self.app.bootstrap()["materials"]])
        with self.assertRaises(InvalidInput):
            self.app.material_detail("M02")

    def test_delete_unknown_material(self):
        from src.errors import InvalidInput

        with self.assertRaises(InvalidInput):
            self.app.delete_material("M99")


class HttpTests(unittest.TestCase):
    """起一个真服务器，验证路由、静态资源与错误码。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app = App(Path(cls.tmp.name) / "db.json", now=NOW)
        app.db.initialize()
        handler = type("BoundHandler", (Handler,), {"app": app, "quiet": True})
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _get(self, path):
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def _post(self, path, payload):
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _request(self, method, path, payload):
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_material_crud_endpoints(self):
        payload = {
            "metadata": {
                "title": "接口新增素材",
                "file_url": "https://cdn.demo/video/api1.mp4",
                "duration_sec": 25,
                "format": "mp4",
            }
        }
        status, created = self._request("POST", "/api/materials", payload)
        self.assertEqual(status, 201)
        material_id = created["id"]

        status, updated = self._request(
            "PUT", f"/api/materials/{material_id}", {"metadata": {"duration_sec": 26}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["metadata"]["duration_sec"], 26)
        self.assertEqual(updated["title"], "接口新增素材")

        status, deleted = self._request("DELETE", f"/api/materials/{material_id}", {})
        self.assertEqual(status, 200)
        self.assertTrue(deleted["ok"])

        _, body = self._get("/api/bootstrap")
        self.assertNotIn(material_id, [item["id"] for item in json.loads(body)["materials"]])

    def test_material_create_conflict_and_validation(self):
        payload = {
            "metadata": {
                "title": "重复素材",
                "file_url": "https://cdn.demo/video/m01.mp4",
                "duration_sec": 45,
                "format": "mp4",
            }
        }
        with self.assertRaises(HTTPError) as caught:
            self._request("POST", "/api/materials", payload)  # 与种子 M01 同文件
        self.assertEqual(caught.exception.code, 409)

        with self.assertRaises(HTTPError) as caught:
            self._request("POST", "/api/materials", {"metadata": {"title": ""}})
        self.assertEqual(caught.exception.code, 400)

    def test_index_and_assets(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body)
        self.assertIn("app.js", body)

        css_status, css = self._get("/app.css")
        self.assertEqual(css_status, 200)
        self.assertIn(".timeline", css)

        js_status, js = self._get("/app.js")
        self.assertEqual(js_status, 200)
        self.assertIn("/api/run", js)

    def test_health(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_run_and_bootstrap(self):
        status, result = self._post(
            "/api/run",
            {"material_id": "M01", "date": "2026-11-05", "hour": 18, "provider": "mock"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "needs_confirmation")

        _, body = self._get("/api/bootstrap")
        self.assertEqual(len(json.loads(body)["materials"]), 10)

    def test_unknown_route_returns_404(self):
        with self.assertRaises(HTTPError) as caught:
            self._get("/api/nope")
        self.assertEqual(caught.exception.code, 404)

    def test_drafts_and_material_endpoints(self):
        _, run = self._post(
            "/api/run",
            {"material_id": "M01", "date": "2026-11-05", "hour": 18, "provider": "mock"},
        )
        status, body = self._get("/api/drafts")
        self.assertEqual(status, 200)
        drafts = json.loads(body)
        self.assertGreaterEqual(len(drafts["drafts"]), 2)
        self.assertIn("counts", drafts)

        status, body = self._get("/api/materials/M01")
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertEqual(detail["id"], "M01")
        self.assertTrue(detail["has_schedule"])
        self.assertEqual(detail["schedules"][0]["conflict_with"], [])

        # 取消后时段释放，草稿变 CANCELLED
        status, cancelled = self._post("/api/cancel", {"draft_id": run["draft"]["draft_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["draft"]["status"], "CANCELLED")

    def test_material_detail_unknown_returns_400(self):
        with self.assertRaises(HTTPError) as caught:
            self._get("/api/materials/M99")
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("不存在", body["error"])

    def test_bad_payload_returns_400(self):
        request = Request(
            f"http://127.0.0.1:{self.port}/api/run",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        body = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("JSON", body["error"])


if __name__ == "__main__":
    unittest.main()
