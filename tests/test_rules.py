import unittest

from src.rules import FIELD_LABELS, RULES_VERSION, audit
from src.util import content_fingerprint

BASE = {
    "title": "秋日限定甜品探店",
    "cover_url": "https://cdn.demo/cover/m01.jpg",
    "file_url": "https://cdn.demo/video/m01.mp4",
    "format": "mp4",
    "duration_sec": 45,
    "copyright": "original",
    "tags": ["美食", "探店"],
}


def codes(report) -> set[str]:
    return {risk.code for risk in report.risks}


class RulesTests(unittest.TestCase):
    def test_clean_material_passes(self):
        report = audit(BASE)
        self.assertEqual(report.conclusion, "PASSED")
        self.assertEqual(report.missing_fields, [])
        self.assertEqual(report.risk_reason, None)
        self.assertEqual(report.rules_version, RULES_VERSION)

    def test_r001_missing_title(self):
        report = audit({**BASE, "title": "  "})
        self.assertEqual(report.conclusion, "INCOMPLETE")
        self.assertIn("title", report.missing_fields)
        self.assertIn("标题", report.risk_reason)

    def test_r002_missing_cover(self):
        payload = dict(BASE)
        payload.pop("cover_url")
        report = audit(payload)
        self.assertEqual(report.conclusion, "INCOMPLETE")
        self.assertEqual(report.missing_fields, ["cover_url"])

    def test_r003_invalid_duration(self):
        for value in (0, -1, "45", True, None):
            with self.subTest(value=value):
                report = audit({**BASE, "duration_sec": value})
                self.assertEqual(report.conclusion, "INCOMPLETE")
                self.assertIn("duration_sec", report.missing_fields)

    def test_r004_copyright_is_medium_not_blocking(self):
        payload = dict(BASE)
        payload.pop("copyright")
        report = audit(payload)
        self.assertEqual(report.conclusion, "PASSED")
        self.assertIn("R004", codes(report))
        self.assertIn("版权", report.risk_reason)

    def test_r005_sensitive_words_block(self):
        report = audit({**BASE, "title": "全网最低价零食开箱"})
        self.assertEqual(report.conclusion, "RISK")
        self.assertEqual([risk.level for risk in report.risks if risk.code == "R005"], ["HIGH"])
        self.assertIn("最低价", report.risk_reason)

    def test_r005_checks_tags_too(self):
        report = audit({**BASE, "tags": ["零食", "包治百病"]})
        self.assertEqual(report.conclusion, "RISK")

    def test_r006_long_title_is_low(self):
        report = audit({**BASE, "title": "标" * 31})
        self.assertEqual(report.conclusion, "PASSED")
        self.assertIn("R006", codes(report))

    def test_r007_empty_tags(self):
        report = audit({**BASE, "tags": []})
        self.assertIn("R007", codes(report))

    def test_r008_bad_format_blocks(self):
        report = audit({**BASE, "format": "exe"})
        self.assertEqual(report.conclusion, "RISK")
        self.assertIn("R008", codes(report))

    def test_missing_precedes_risk(self):
        """同时缺字段和违规时，先判信息缺失：让用户补完再谈风险。"""
        report = audit({**BASE, "title": "全网最低价", "cover_url": ""})
        self.assertEqual(report.conclusion, "INCOMPLETE")

    def test_metadata_none_is_incomplete(self):
        report = audit(None)
        self.assertEqual(report.conclusion, "INCOMPLETE")
        self.assertTrue(report.missing_fields)

    def test_field_labels_cover_all_required(self):
        for name in ("title", "cover_url", "file_url", "duration_sec"):
            self.assertIn(name, FIELD_LABELS)

    # -- R009 内容重复 --------------------------------------------------
    def test_r009_blocks_duplicate_content(self):
        report = audit(BASE, duplicate_of=["M09"])
        self.assertEqual(report.conclusion, "RISK")
        self.assertIn("R009", codes(report))
        self.assertEqual([r.level for r in report.risks if r.code == "R009"], ["HIGH"])
        self.assertIn("M09", report.risk_reason)

    def test_no_r009_without_duplicate(self):
        report = audit(BASE)
        self.assertNotIn("R009", codes(report))
        self.assertEqual(report.conclusion, "PASSED")

    def test_fingerprint_is_stable_for_same_file(self):
        base = content_fingerprint(BASE)
        noisy = {**BASE, "file_url": "  HTTPS://CDN.DEMO/video/m01.mp4  ", "title": "另一个标题"}
        self.assertEqual(base, content_fingerprint(noisy))

    def test_fingerprint_differs_on_duration_or_format(self):
        self.assertNotEqual(content_fingerprint(BASE), content_fingerprint({**BASE, "duration_sec": 46}))
        self.assertNotEqual(content_fingerprint(BASE), content_fingerprint({**BASE, "format": "mov"}))

    def test_fingerprint_falls_back_to_title_without_file(self):
        payload = {k: v for k, v in BASE.items() if k != "file_url"}
        same = {**payload, "title": "  秋日限定甜品探店  "}
        self.assertEqual(content_fingerprint(payload), content_fingerprint(same))
        self.assertNotEqual(
            content_fingerprint(payload), content_fingerprint({**payload, "title": "别的标题"})
        )


if __name__ == "__main__":
    unittest.main()
