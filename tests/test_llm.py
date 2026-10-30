import json
import unittest

from src.errors import InvalidInput
from src.llm import HttpLLM, MockLLM
from src.util import parse_datetime

NOW = parse_datetime("2026-11-05T09:00:00+08:00")


class MockLLMTests(unittest.TestCase):
    def setUp(self):
        self.llm = MockLLM()

    def test_structured_fields_win(self):
        intent = self.llm.parse_intent(
            {
                "material_id": "m02",
                "desired_slot": "2026-11-05T20:00:00+08:00",
                "slot_constraint": "hard",
            },
            NOW,
        )
        self.assertEqual(intent.material_id, "M02")
        self.assertEqual(intent.desired_slot, "2026-11-05T20:00:00+08:00")
        self.assertEqual(intent.slot_constraint, "hard")
        self.assertEqual(intent.source, "structured")

    def test_parses_material_and_time_from_text(self):
        intent = self.llm.parse_intent(
            {"user_text": "把 M01 排在 2026-11-05 18:00", "target_date": "2026-11-05"}, NOW
        )
        self.assertEqual(intent.material_id, "M01")
        self.assertEqual(intent.desired_slot, "2026-11-05T18:00:00+08:00")
        self.assertEqual(intent.source, "user_text")

    def test_hard_keyword_detected(self):
        intent = self.llm.parse_intent(
            {"material_id": "M01", "user_text": "18:00 必须这个时间"}, NOW
        )
        self.assertEqual(intent.slot_constraint, "hard")

    def test_default_constraint_is_soft(self):
        intent = self.llm.parse_intent({"material_id": "M01"}, NOW)
        self.assertEqual(intent.slot_constraint, "soft")

    def test_time_only_falls_back_to_now_date(self):
        intent = self.llm.parse_intent({"user_text": "把 M03 排在 20:00"}, NOW)
        self.assertEqual(intent.desired_slot, "2026-11-05T20:00:00+08:00")

    def test_floor_to_hour(self):
        intent = self.llm.parse_intent(
            {"material_id": "M01", "desired_slot": "2026-11-05T18:37:12+08:00"}, NOW
        )
        self.assertEqual(intent.desired_slot, "2026-11-05T18:00:00+08:00")

    def test_invalid_material_id_type(self):
        with self.assertRaises(InvalidInput):
            self.llm.parse_intent({"material_id": 123}, NOW)

    def test_invalid_slot(self):
        with self.assertRaises(InvalidInput):
            self.llm.parse_intent({"material_id": "M01", "desired_slot": "明天下午"}, NOW)

    def test_not_a_dict(self):
        with self.assertRaises(InvalidInput):
            self.llm.parse_intent([], NOW)

    def test_invalid_constraint(self):
        with self.assertRaises(InvalidInput):
            self.llm.parse_intent({"material_id": "M01", "slot_constraint": "maybe"}, NOW)

    def test_illegal_hour_in_text(self):
        with self.assertRaises(InvalidInput):
            self.llm.parse_intent({"user_text": "把 M01 排在 25:00"}, NOW)


class HttpLLMTests(unittest.TestCase):
    def test_parses_openai_style_payload(self):
        captured = {}

        def fake_transport(url, headers, body):
            captured["url"] = url
            captured["body"] = json.loads(body)
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "material_id": "m05",
                                        "desired_slot": "2026-11-05T21:00:00+08:00",
                                        "slot_constraint": "hard",
                                    }
                                )
                            }
                        }
                    ]
                }
            )

        llm = HttpLLM("https://api.demo/v1", "k", "demo-model", transport=fake_transport)
        intent = llm.parse_intent({"user_text": "把 M05 排在 21:00"}, NOW)

        self.assertEqual(captured["url"], "https://api.demo/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "demo-model")
        self.assertEqual(intent.material_id, "M05")
        self.assertEqual(intent.desired_slot, "2026-11-05T21:00:00+08:00")
        self.assertEqual(intent.slot_constraint, "hard")

    def test_malformed_response_raises(self):
        llm = HttpLLM("https://api.demo/v1", "k", "m", transport=lambda *a: "not json")
        with self.assertRaises(InvalidInput):
            llm.parse_intent({"material_id": "M01"}, NOW)


if __name__ == "__main__":
    unittest.main()
