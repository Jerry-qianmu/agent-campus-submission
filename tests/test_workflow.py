import copy
import json
import unittest

from src.clients import OpenAICompatibleClient
from src.graph import run_request
from src.policy import LLMPolicy
from support import NOW, TempDb, load_example, read_table

ALLOWED_STATUS = {
    "completed",
    "needs_confirmation",
    "needs_input",
    "blocked",
    "not_found",
    "tool_error",
    "step_limit_reached",
}


def actions(result):
    return [step["action"] for step in result["decisions"]]


class WorkflowTests(unittest.TestCase):
    def run_example(self, db, name):
        request = load_example(name)
        return run_request(request, db, now=NOW)

    # -- 必须验收的六类 ------------------------------------------------
    def test_normal_completes_with_draft(self):
        with TempDb() as db:
            result = self.run_example(db, "normal")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["review_status"], "PASSED")
            self.assertEqual(result["draft"]["draft_id"], 2)
            self.assertEqual(result["draft"]["status"], "DRAFT")
            self.assertEqual(result["pending_confirmations"], [])
            self.assertFalse(result["published"])
            self.assertEqual(result["mode"], "mock_react")
            self.assertEqual(result["trace"][0], "prepare")
            self.assertEqual(result["trace"][-1], "finalize")
            self.assertEqual(actions(result)[-1], "finish")

    def test_missing_material_info_blocks(self):
        with TempDb() as db:
            result = self.run_example(db, "missing")
            self.assertEqual(result["status"], "needs_input")
            self.assertEqual(result["review_status"], "INCOMPLETE")
            self.assertEqual(result["missing_fields"], ["cover_url"])
            self.assertFalse(result["draft"]["saved"])
            self.assertEqual(len(read_table(db, "publish_schedule")), 1)
            # 模型看到 INCOMPLETE 后补查了一次元数据，并重新审核
            self.assertEqual(actions(result).count("material.get"), 2)
            self.assertEqual(actions(result).count("audit.check"), 2)

    def test_rule_risk_blocks(self):
        with TempDb() as db:
            result = self.run_example(db, "risk")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["review_status"], "RISK")
            self.assertIn("R005", result["risk_reason"])
            self.assertFalse(result["draft"]["saved"])
            self.assertEqual(len(read_table(db, "publish_schedule")), 1)

    def test_slot_conflict_suggests_alternative(self):
        """参考实例：M01 要 18:00，被 M00 占用，19:00 可用。"""
        with TempDb() as db:
            result = self.run_example(db, "conflict")
            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["suggested_slot"], "2026-11-05T19:00:00+08:00")
            self.assertTrue(result["draft"]["saved"])
            self.assertEqual(len(result["pending_confirmations"]), 1)
            self.assertIn("19:00", result["pending_confirmations"][0])
            self.assertFalse(result["published"])  # 不得称为已发布
            rows = read_table(db, "publish_schedule")
            self.assertEqual(rows[-1]["status"], "DRAFT")
            self.assertIsNone(rows[-1]["confirm_time"])

    def test_hard_conflict_never_moves_slot(self):
        with TempDb() as db:
            result = self.run_example(db, "hard-conflict")
            self.assertEqual(result["status"], "blocked")
            self.assertIsNone(result["suggested_slot"])
            self.assertFalse(result["draft"]["saved"])
            self.assertEqual(len(read_table(db, "publish_schedule")), 1)
            self.assertIn("硬性要求未改动", result["pending_confirmations"][0])
            self.assertIn("2026-11-05T19:00:00+08:00", result["alternatives"])

    def test_duplicate_submission_creates_one_draft(self):
        with TempDb() as db:
            request = load_example("normal")
            first = run_request(copy.deepcopy(request), db, now=NOW)
            second = run_request(copy.deepcopy(request), db, now=NOW)

            self.assertEqual(first["draft"]["draft_id"], second["draft"]["draft_id"])
            self.assertTrue(second["draft"]["deduped"])
            rows = [row for row in read_table(db, "publish_schedule") if row["material_id"] == "M02"]
            self.assertEqual(len(rows), 1)

    def test_save_failure_is_reported_loudly(self):
        with TempDb() as db:
            result = self.run_example(db, "save-failure")
            self.assertEqual(result["status"], "tool_error")
            self.assertFalse(result["draft"]["saved"])
            self.assertIn("DB_UNAVAILABLE", result["message"])
            # 可重试错误只重试一次：同一动作出现两次
            self.assertEqual(actions(result).count("schedule.save_draft"), 2)
            self.assertEqual(len(read_table(db, "publish_schedule")), 1)
            self.assertNotIn("已保存", result["message"])

    # -- 其它边界 ------------------------------------------------------
    def test_metadata_lag_recovers_after_recheck(self):
        with TempDb() as db:
            result = self.run_example(db, "metadata-lag")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(actions(result).count("audit.check"), 2)
            self.assertEqual(actions(result).count("material.get"), 2)

    def test_unknown_material(self):
        with TempDb() as db:
            result = self.run_example(db, "not-found")
            self.assertEqual(result["status"], "not_found")
            self.assertFalse(result["draft"]["saved"])

    def test_tool_failure_is_retryable_but_still_fails(self):
        with TempDb() as db:
            result = self.run_example(db, "tool-failure")
            self.assertEqual(result["status"], "tool_error")
            self.assertIn("SOURCE_UNAVAILABLE", result["message"])

    def test_notify_failure_degrades_without_blocking(self):
        with TempDb() as db:
            request = load_example("normal")
            request["simulate"] = {"notify_failure": True}
            result = run_request(request, db, now=NOW)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["draft"]["saved"])
            self.assertFalse(result["notify"]["ok"])
            self.assertTrue(result["notify"]["degraded"])

    def test_repeat_runs_do_not_publish_anything(self):
        with TempDb() as db:
            for name in ("normal", "conflict", "hard-conflict", "risk", "missing"):
                self.run_example(db, name)
            rows = read_table(db, "publish_schedule")
            self.assertTrue(all(row["status"] in ("DRAFT", "CONFIRMED", "CANCELLED") for row in rows))
            reviews = read_table(db, "material_review")
            self.assertTrue(all(row["release_date"] is None for row in reviews))

    def test_cancelled_slot_is_revived_not_duplicated(self):
        with TempDb() as db:
            with db.transaction() as session:
                session.insert(
                    "publish_schedule",
                    {
                        "date": "2026-11-05T20:00:00+08:00",
                        "material_id": "M02",
                        "status": "CANCELLED",
                        "confirm_time": None,
                        "idem_key": None,
                    },
                )
            result = self.run_example(db, "normal")
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["draft"]["reused"])
            rows = [row for row in read_table(db, "publish_schedule") if row["material_id"] == "M02"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "DRAFT")

    def test_other_material_occupies_slot(self):
        """M00 占 20:00 时，M02 要 20:00 应顺延到 21:00。"""
        with TempDb() as db:
            with db.transaction() as session:
                session.insert(
                    "publish_schedule",
                    {
                        "date": "2026-11-05T20:00:00+08:00",
                        "material_id": "M00",
                        "status": "DRAFT",
                        "confirm_time": None,
                        "idem_key": None,
                    },
                )
            result = self.run_example(db, "normal")
            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["suggested_slot"], "2026-11-05T21:00:00+08:00")

    def test_every_status_is_known(self):
        with TempDb() as db:
            for name in ("normal", "missing", "risk", "conflict", "hard-conflict", "not-found", "tool-failure"):
                with self.subTest(name=name):
                    self.assertIn(self.run_example(db, name)["status"], ALLOWED_STATUS)

    def test_input_object_is_not_mutated(self):
        with TempDb() as db:
            request = load_example("conflict")
            before = copy.deepcopy(request)
            run_request(request, db, now=NOW)
            self.assertEqual(request, before)

    def test_missing_material_id_asks_for_it(self):
        with TempDb() as db:
            result = run_request({"user_text": "帮我排个素材"}, db, now=NOW)
            self.assertEqual(result["status"], "needs_input")
            self.assertEqual(result["missing_fields"], ["material_id"])

    def test_no_desired_slot_picks_first_free(self):
        with TempDb() as db:
            result = run_request({"material_id": "M02"}, db, now=NOW)
            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["suggested_slot"], "2026-11-05T09:00:00+08:00")
            self.assertTrue(result["pending_confirmations"])

    # -- ReAct 专项 ----------------------------------------------------
    def test_step_limit_returns_unfinished_state(self):
        """限步必须返回明确的未完成状态，而不是悄悄继续或抛异常。"""
        with TempDb() as db:
            request = load_example("metadata-lag")  # 正常需要 6 次工具调用
            result = run_request(request, db, now=NOW, max_steps=2)
            self.assertEqual(result["status"], "step_limit_reached")
            self.assertFalse(result["finished"])
            self.assertFalse(result["completed"])
            self.assertEqual(result["tool_calls"], 2)
            self.assertFalse(result["draft"]["saved"])
            self.assertIn("未完成", result["message"])

    def test_decision_log_has_required_fields(self):
        with TempDb() as db:
            result = self.run_example(db, "conflict")
            self.assertGreaterEqual(result["steps"], 4)
            for step in result["decisions"]:
                self.assertIn("step", step)
                self.assertIn("action", step)
                self.assertIn("args", step)
                self.assertTrue(step["rationale"])
                if step["action"] != "finish":
                    self.assertIsNotNone(step["observation"])
                    self.assertIn("summary", step["observation"])
            self.assertIsNone(result["decisions"][-1]["next"])

    def test_action_changes_after_observing_conflict(self):
        """同一素材、同一时段冲突：软性要求会改档并存草稿，硬性要求则直接收手。"""
        with TempDb() as db:
            soft = self.run_example(db, "conflict")
        with TempDb() as db:
            hard = self.run_example(db, "hard-conflict")

        self.assertIn("schedule.save_draft", actions(soft))
        self.assertNotIn("schedule.save_draft", actions(hard))
        self.assertEqual(soft["status"], "needs_confirmation")
        self.assertEqual(hard["status"], "blocked")
        # 两条路径在观测到「18:00 被占」之前完全一致，之后分叉
        self.assertEqual(actions(soft)[:4], actions(hard)[:4])

    def test_llm_policy_drives_the_same_loop(self):
        """用脚本化的假模型跑通「模型选动作 → 执行 → 观察 → 模型再选」。"""
        script = [
            ("material.get", {"material_id": "M02"}),
            ("audit.check", {"material_id": "M02"}),
            ("slot.query", {"date": "2026-11-05", "material_id": "M02"}),
            ("schedule.save_draft", {"material_id": "M02", "slot": "2026-11-05T20:00:00+08:00"}),
            ("finish", {"status": "completed", "suggested_slot": "2026-11-05T20:00:00+08:00"}),
        ]
        seen = {"index": 0, "bodies": []}

        def transport(url, headers, body):
            seen["bodies"].append(json.loads(body))
            name, args = script[seen["index"]]
            seen["index"] += 1
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": f"调用 {name}",
                                "tool_calls": [
                                    {
                                        "id": f"call_{seen['index']}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        client = OpenAICompatibleClient("k", "https://api.demo/v1", "demo", transport=transport)
        policy = LLMPolicy(client)
        with TempDb() as db:
            result = run_request(load_example("normal"), db, now=NOW, policy=policy)

        self.assertEqual(result["mode"], "openai_react")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["draft"]["draft_id"], 2)
        self.assertEqual(
            actions(result),
            ["material.get", "audit.check", "slot.query", "schedule.save_draft", "finish"],
        )
        # 第二轮请求里必须带上一轮的工具观测，否则模型无从「根据结果调整」
        second_round = seen["bodies"][1]["messages"]
        self.assertTrue(any(message["role"] == "tool" for message in second_round))
        self.assertEqual(len(seen["bodies"][1]["tools"]), 6)  # 5 个业务工具 + finish
        self.assertEqual(seen["bodies"][1]["tool_choice"], "auto")

    def test_model_failure_finishes_with_tool_error(self):
        """模型层失败（超时/鉴权/余额）必须如实报错，而不是继续猜。"""
        from src.clients import ChatError

        class BrokenPolicy:
            name = "broken"

            def decide(self, ctx):
                raise ChatError("鉴权失败", code="AUTH", status=401, retryable=False)

        with TempDb() as db:
            result = run_request(load_example("normal"), db, now=NOW, policy=BrokenPolicy())
        self.assertEqual(result["status"], "tool_error")
        self.assertTrue(result["finished"])
        self.assertFalse(result["draft"]["saved"])
        self.assertIn("鉴权失败", result["message"])
        self.assertEqual(result["model_error"]["code"], "AUTH")


if __name__ == "__main__":
    unittest.main()
