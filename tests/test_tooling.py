import unittest

from src.registry import (
    REGISTRY,
    TOOL_NAME_PATTERN,
    ToolRegistrationError,
    discover,
    execute_tool,
    registered_tools,
    required_schema,
    tool,
    tool_schemas,
)
from src.tooling import ToolContext
from src.tools import Faults, fail_result, ok_result
from src.util import parse_datetime

NOW = parse_datetime("2026-11-05T09:00:00+08:00")
EXPECTED = {
    "material_get",
    "audit_check",
    "slot_query",
    "slot_find_alternative",
    "schedule_save_draft",
}


class DiscoveryTests(unittest.TestCase):
    """@tool 装饰器 + 包扫描：注册、schema、执行三者必须对上。"""

    def test_discover_finds_all_tools(self):
        self.assertEqual(set(registered_tools()), EXPECTED)

    def test_discover_is_idempotent(self):
        self.assertEqual(discover(), discover())
        self.assertEqual(set(REGISTRY), EXPECTED)

    def test_every_spec_is_complete(self):
        for spec in REGISTRY.values():
            self.assertEqual(spec.name, spec.as_schema()["function"]["name"])
            self.assertTrue(spec.description)
            self.assertEqual(spec.parameters["type"], "object")
            self.assertTrue(spec.parameters["required"])
            self.assertTrue(callable(spec.handler))
            self.assertTrue(callable(spec.formatter))

    def test_schemas_include_finish_last(self):
        names = [schema["function"]["name"] for schema in tool_schemas()]
        self.assertEqual(names[-1], "finish")
        self.assertEqual(set(names[:-1]), EXPECTED)
        self.assertEqual(len(names), len(REGISTRY) + 1)

    def test_names_are_model_safe(self):
        """function name 只允许字母数字下划线连字符；点号会让真实模型 400。"""
        for name in registered_tools():
            self.assertRegex(name, TOOL_NAME_PATTERN)


class RegistrationTests(unittest.TestCase):
    def test_duplicate_name_rejected(self):
        with self.assertRaises(ToolRegistrationError):

            @tool(
                name="material_get",
                description="重复注册",
                parameters=required_schema({}),
                formatter=lambda result: "",
            )
            def duplicate(ctx, args):
                return ok_result()

    def test_dotted_name_rejected(self):
        with self.assertRaises(ToolRegistrationError):

            @tool(
                name="material.get",
                description="点号不合法",
                parameters=required_schema({}),
                formatter=lambda result: "",
            )
            def dotted(ctx, args):
                return ok_result()

    def test_missing_formatter_rejected(self):
        with self.assertRaises(ToolRegistrationError):

            @tool(name="no_formatter", description="缺摘要", parameters=required_schema({}))
            def no_formatter(ctx, args):
                return ok_result()

    def test_decorator_registers_and_dispatches(self):
        @tool(
            name="demo_probe",
            description="测试用",
            parameters=required_schema({"x": "string"}),
            formatter=lambda result: f"probe={result.data['x']}",
        )
        def probe(ctx, args):
            return ok_result(x=args["x"])

        ctx = ToolContext(db=None, now=NOW, faults=Faults(), rules_version="r")
        try:
            result, summary = execute_tool("demo_probe", {"x": "hi"}, ctx)
            self.assertTrue(result.ok)
            self.assertEqual(summary, "probe=hi")

            missing, _ = execute_tool("demo_probe", {}, ctx)
            self.assertEqual(missing.code, "INVALID_ARGUMENT")

            unknown, _ = execute_tool("not_a_tool", {}, ctx)
            self.assertEqual(unknown.code, "INVALID_ARGUMENT")
        finally:
            REGISTRY.pop("demo_probe", None)

    def test_summarize_marks_retryable_failures(self):
        @tool(
            name="demo_fail",
            description="测试用失败",
            parameters=required_schema({}),
            formatter=lambda result: "unused",
        )
        def boom(ctx, args):
            return fail_result("DB_UNAVAILABLE", "连接断了", retryable=True)

        ctx = ToolContext(db=None, now=NOW, faults=Faults(), rules_version="r")
        try:
            result, summary = execute_tool("demo_fail", {}, ctx)
            self.assertFalse(result.ok)
            self.assertIn("可重试", summary)
            self.assertIn("DB_UNAVAILABLE", summary)
        finally:
            REGISTRY.pop("demo_fail", None)


if __name__ == "__main__":
    unittest.main()
