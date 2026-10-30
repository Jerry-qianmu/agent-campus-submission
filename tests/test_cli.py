import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "src.main", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


class CliTests(unittest.TestCase):
    def test_normal_example(self):
        process = run_cli("--input", "examples/normal.input.json")
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mode"], "mock_react")
        self.assertFalse(result["published"])
        self.assertGreaterEqual(result["steps"], 4)

    def test_max_steps_flag_returns_unfinished(self):
        process = run_cli(
            "--input", "examples/metadata-lag.input.json", "--max-steps", "2", "--compact"
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "step_limit_reached")
        self.assertFalse(result["finished"])

    def test_decision_log_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "run.jsonl"
            process = run_cli(
                "--input", "examples/conflict.input.json", "--log", str(log_path)
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            lines = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        self.assertEqual(len(lines), json.loads(process.stdout)["steps"])
        self.assertEqual(lines[0]["action"], "material.get")
        self.assertEqual(lines[-1]["action"], "finish")
        self.assertIn("rationale", lines[0])
        self.assertIn("summary", lines[0]["observation"])

    def test_invalid_json_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("[]", encoding="utf-8")
            process = run_cli("--input", str(path))
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stderr)["status"], "invalid_input")

    def test_missing_file(self):
        process = run_cli("--input", "examples/does-not-exist.json")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stderr)["status"], "invalid_input")

    def test_accepts_utf8_bom(self):
        """Windows 记事本/PowerShell 存出的 JSON 带 BOM，不能被误判成非法输入。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.json"
            body = json.dumps(
                {"material_id": "M02", "desired_slot": "2026-11-05T20:00:00+08:00"},
                ensure_ascii=False,
            )
            path.write_text(body, encoding="utf-8-sig")
            process = run_cli("--input", str(path), "--now", "2026-11-05T09:00:00+08:00")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "completed")

    def test_bad_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repeat.json"
            path.write_text(json.dumps({"material_id": "M02", "repeat": 99}), encoding="utf-8")
            process = run_cli("--input", str(path))
        self.assertEqual(process.returncode, 2)

    def test_persistent_db_dedupes_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "db.json")
            first = run_cli("--input", "examples/normal.input.json", "--db", db_path)
            second = run_cli("--input", "examples/normal.input.json", "--db", db_path)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_result = json.loads(first.stdout)
        second_result = json.loads(second.stdout)
        self.assertEqual(first_result["draft"]["draft_id"], second_result["draft"]["draft_id"])
        self.assertTrue(second_result["draft"]["deduped"])

    def test_now_override(self):
        process = run_cli(
            "--input", "examples/conflict.input.json", "--now", "2026-11-05T09:00:00+08:00"
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["suggested_slot"], "2026-11-05T19:00:00+08:00")

    def test_compact_output_is_single_line(self):
        process = run_cli("--input", "examples/normal.input.json", "--compact")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(process.stdout.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
