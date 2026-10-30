import json
import os
import unittest
from unittest import mock

from src.clients import (
    ChatError,
    DeepSeekClient,
    HttpResponse,
    OpenAICompatibleClient,
    build_client,
)
from src.errors import InvalidInput

OK_BODY = json.dumps(
    {
        "model": "deepseek-flash",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "先读素材",
                    "reasoning_content": "用户在问 M01",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "material.get",
                                "arguments": '{"material_id": "M01"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
)


def transport_sequence(responses, calls=None):
    """按顺序返回响应；用完后重复最后一个。"""
    index = {"i": 0}

    def transport(url, headers, body):
        if calls is not None:
            calls.append({"url": url, "body": json.loads(body), "headers": headers})
        position = min(index["i"], len(responses) - 1)
        index["i"] += 1
        item = responses[position]
        if isinstance(item, Exception):
            raise item
        return item

    return transport


def make_client(transport, **kwargs):
    return OpenAICompatibleClient(
        "test-key", "https://api.demo/v1", "demo-model", transport=transport, **kwargs
    )


class ParseTests(unittest.TestCase):
    def test_parses_tool_calls_usage_and_reasoning(self):
        client = make_client(transport_sequence([HttpResponse(200, OK_BODY)]))
        result = client.chat([{"role": "user", "content": "hi"}], tools=[])

        self.assertEqual(result.content, "先读素材")
        self.assertEqual(result.reasoning, "用户在问 M01")
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(result.tool_calls[0]["name"], "material.get")
        self.assertEqual(result.tool_calls[0]["arguments"], '{"material_id": "M01"}')
        self.assertEqual(result.usage["total_tokens"], 150)
        self.assertNotIn("prompt_tokens_details", result.usage)  # 只保留整型计数
        self.assertEqual(client.last_usage["prompt_tokens"], 120)

    def test_bad_json_response(self):
        client = make_client(transport_sequence([HttpResponse(200, "oops")]))
        with self.assertRaises(ChatError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.code, "BAD_RESPONSE")

    def test_missing_choices(self):
        client = make_client(transport_sequence([HttpResponse(200, json.dumps({"choices": []}))]))
        with self.assertRaises(ChatError):
            client.chat([{"role": "user", "content": "hi"}])


class RetryTests(unittest.TestCase):
    def test_retries_429_then_succeeds(self):
        sleeps = []
        client = make_client(
            transport_sequence([HttpResponse(429, '{"error": {"message": "slow down"}}'), HttpResponse(200, OK_BODY)]),
            sleeper=sleeps.append,
        )
        result = client.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(result.tool_calls)
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)

    def test_honours_retry_after_header(self):
        sleeps = []
        client = make_client(
            transport_sequence(
                [HttpResponse(429, "{}", {"Retry-After": "3"}), HttpResponse(200, OK_BODY)]
            ),
            sleeper=sleeps.append,
        )
        client.chat([{"role": "user", "content": "hi"}])
        self.assertGreaterEqual(sleeps[0], 3)

    def test_5xx_exhausts_retries_then_raises(self):
        calls = []
        client = make_client(
            transport_sequence([HttpResponse(503, '{"error": {"message": "busy"}}')], calls),
            max_retries=2,
            sleeper=lambda _: None,
        )
        with self.assertRaises(ChatError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "SERVER_BUSY")
        self.assertEqual(len(calls), 3)  # 首次 + 2 次重试

    def test_400_is_not_retried(self):
        calls = []
        client = make_client(
            transport_sequence([HttpResponse(400, '{"error": {"message": "bad body"}}')], calls),
            sleeper=lambda _: None,
        )
        with self.assertRaises(ChatError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertIn("bad body", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_401_auth_not_retried(self):
        calls = []
        client = make_client(
            transport_sequence([HttpResponse(401, "{}")], calls), sleeper=lambda _: None
        )
        with self.assertRaises(ChatError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.code, "AUTH")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(calls), 1)

    def test_402_insufficient_balance(self):
        client = make_client(
            transport_sequence([HttpResponse(402, "{}")]), sleeper=lambda _: None
        )
        with self.assertRaises(ChatError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.code, "INSUFFICIENT_BALANCE")
        self.assertFalse(caught.exception.retryable)

    def test_timeout_is_retried_then_raises(self):
        calls = []
        client = make_client(
            transport_sequence([TimeoutError("timed out")], calls),
            max_retries=1,
            sleeper=lambda _: None,
        )
        with self.assertRaises(ChatError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(calls), 2)

    def test_network_error_maps_to_network(self):
        client = make_client(transport_sequence([OSError("connection reset")]), max_retries=0)
        with self.assertRaises(ChatError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.code, "NETWORK")


class DeepSeekTests(unittest.TestCase):
    def test_defaults(self):
        client = DeepSeekClient("k")
        self.assertEqual(client.endpoint, "https://api.deepseek.com/chat/completions")
        self.assertEqual(client.model, "deepseek-flash")

    def test_thinking_disabled_payload(self):
        captured = []
        client = DeepSeekClient(
            "k", transport=transport_sequence([HttpResponse(200, OK_BODY)], captured)
        )
        client.chat([{"role": "user", "content": "hi"}])
        body = captured[0]["body"]
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(captured[0]["url"], "https://api.deepseek.com/chat/completions")

    def test_thinking_enabled_drops_temperature(self):
        captured = []
        client = DeepSeekClient(
            "k",
            thinking="enabled",
            transport=transport_sequence([HttpResponse(200, OK_BODY)], captured),
        )
        client.chat([{"role": "user", "content": "hi"}], temperature=0.0)
        body = captured[0]["body"]
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertNotIn("temperature", body)

    def test_reasoning_effort_overrides_thinking(self):
        captured = []
        client = DeepSeekClient(
            "k",
            reasoning_effort="high",
            transport=transport_sequence([HttpResponse(200, OK_BODY)], captured),
        )
        client.chat([{"role": "user", "content": "hi"}])
        body = captured[0]["body"]
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("thinking", body)

    def test_tools_and_tool_choice(self):
        captured = []
        client = DeepSeekClient(
            "k", transport=transport_sequence([HttpResponse(200, OK_BODY)], captured)
        )
        client.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
        )
        body = captured[0]["body"]
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["tools"][0]["function"]["name"], "x")
        self.assertFalse(body["stream"])


class FactoryTests(unittest.TestCase):
    def test_mock_returns_none(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "mock"}, clear=False):
            self.assertIsNone(build_client())

    def test_deepseek_requires_key(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "deepseek"}, clear=True):
            with self.assertRaises(InvalidInput) as caught:
                build_client()
            self.assertIn("DEEPSEEK_API_KEY", str(caught.exception))

    def test_deepseek_reads_env(self):
        env = {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "sk-test",
            "DEEPSEEK_MODEL": "deepseek-v4-pro",
            "DEEPSEEK_THINKING": "enabled",
            "DEEPSEEK_TIMEOUT": "30",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            client = build_client()
        self.assertIsInstance(client, DeepSeekClient)
        self.assertEqual(client.model, "deepseek-v4-pro")
        self.assertEqual(client.thinking, "enabled")
        self.assertEqual(client.timeout, 30.0)

    def test_unknown_provider(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "nope"}, clear=True):
            with self.assertRaises(InvalidInput):
                build_client()


if __name__ == "__main__":
    unittest.main()
