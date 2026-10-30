import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.env import describe_sources, load_dotenv, parse_env_text

SAMPLE = """# 注释行
DEEPSEEK_API_KEY = sk-test
export DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL="deepseek-flash"   # 引号内的值，引号外是注释
DEEPSEEK_THINKING='disabled'
DEEPSEEK_REASONING_EFFORT=          # 只有注释，值为空
DEEPSEEK_TIMEOUT=60
DEEPSEEK_MAX_RETRIES=3
不合法行没有等号
=没有键
DEEPSEEK_URL_WITH_HASH=a#b
"""


class ParseTests(unittest.TestCase):
    def test_parses_all_supported_forms(self):
        values = parse_env_text(SAMPLE)
        self.assertEqual(values["DEEPSEEK_API_KEY"], "sk-test")
        self.assertEqual(values["DEEPSEEK_BASE_URL"], "https://api.deepseek.com")
        self.assertEqual(values["DEEPSEEK_MODEL"], "deepseek-flash")
        self.assertEqual(values["DEEPSEEK_THINKING"], "disabled")
        self.assertEqual(values["DEEPSEEK_REASONING_EFFORT"], "")
        self.assertEqual(values["DEEPSEEK_TIMEOUT"], "60")
        self.assertEqual(values["DEEPSEEK_MAX_RETRIES"], "3")

    def test_skips_invalid_lines(self):
        values = parse_env_text(SAMPLE)
        self.assertNotIn("不合法行没有等号", values)
        self.assertNotIn("", values)

    def test_hash_inside_value_is_kept(self):
        """只有"空白+#"才是注释，a#b 这种值不能被截断。"""
        self.assertEqual(parse_env_text(SAMPLE)["DEEPSEEK_URL_WITH_HASH"], "a#b")

    def test_bom_and_crlf(self):
        text = "\ufeffA=1\r\nB=2\r\n"
        self.assertEqual(parse_env_text(text), {"A": "1", "B": "2"})

    def test_empty_text(self):
        self.assertEqual(parse_env_text(""), {})


class LoadTests(unittest.TestCase):
    def _write(self, directory: str, body: str) -> Path:
        path = Path(directory) / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_writes_into_environment(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            path = self._write(tmp, "DEEPSEEK_MODEL=deepseek-v4-pro\n")
            loaded = load_dotenv(path)
            self.assertEqual(loaded, {"DEEPSEEK_MODEL": "deepseek-v4-pro"})
            self.assertEqual(os.getenv("DEEPSEEK_MODEL"), "deepseek-v4-pro")

    def test_dotenv_wins_by_default(self):
        """默认 .env 覆盖同名进程变量：改文件就生效，不被 shell 残留值压掉。"""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"DEEPSEEK_MODEL": "stale-from-shell"}, clear=True
        ):
            path = self._write(tmp, "DEEPSEEK_MODEL=from-dotenv\n")
            load_dotenv(path)
            self.assertEqual(os.getenv("DEEPSEEK_MODEL"), "from-dotenv")

    def test_override_false_keeps_existing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"DEEPSEEK_MODEL": "from-shell"}, clear=True
        ):
            path = self._write(tmp, "DEEPSEEK_MODEL=from-dotenv\n")
            loaded = load_dotenv(path, override=False)
            self.assertEqual(loaded, {})
            self.assertEqual(os.getenv("DEEPSEEK_MODEL"), "from-shell")

    def test_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_dotenv(Path(tmp) / "nope.env"), {})

    def test_first_candidate_wins(self):
        """一次加载多个候选文件时，先出现的生效（仓库根 .env 优先于 cwd .env）。"""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            first = Path(tmp) / "first.env"
            second = Path(tmp) / "second.env"
            first.write_text("DEEPSEEK_MODEL=first\n", encoding="utf-8")
            second.write_text("DEEPSEEK_MODEL=second\n", encoding="utf-8")
            with mock.patch("src.env.dotenv_candidates", return_value=[first, second]):
                loaded = load_dotenv()
            self.assertEqual(loaded, {"DEEPSEEK_MODEL": "first"})
            self.assertEqual(os.getenv("DEEPSEEK_MODEL"), "first")

    def test_describe_sources_mentions_existence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            text = describe_sources(path)
            self.assertIn("不存在", text)
            path.write_text("A=1\n", encoding="utf-8")
            self.assertIn("存在", describe_sources(path))


if __name__ == "__main__":
    unittest.main()
