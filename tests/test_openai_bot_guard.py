"""Exercise Bob's actual guard bridge with a synthetic SDK and frame scheduler.

Only selected methods are AST-extracted. No native plugin, credentials, database,
real SDK or network is imported or accessed.
"""

import ast
import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
from threading import Lock
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src/plugins/openai_bot.py"
ROLE = SOURCE.with_name("openai_bot_role_balance_guard.txt")
METHODS = {
    "bot_role_chat",
    "bot_role_balance_guard",
    "comment_balance_guard",
    "_balance_guard_worker",
    "_balance_guard_completion",
    "_completion",
    "_pick_choice",
    "_cleanup_choice",
    "_send_message",
}


class SyntheticAPIError(Exception):
    pass


class Harness:
    def __init__(self):
        self.workers, self.frames, self.chat, self.discord, self.logs, self.requests = [], [], [], [], [], []
        self.result = "Blue requesting another buff. Very balanced."
        self.current = True
        self.finished = []
        self.validity_checks = 0
        self.stage = "main"
        self.api_error = None

        def schedule(queue):
            def decorate(function):
                def enqueue(*args, **kwargs):
                    queue.append(lambda: function(*args, **kwargs))

                return enqueue

            return decorate

        async def create(**kwargs):
            self.requests.append(kwargs)
            if self.api_error:
                raise self.api_error
            if self.result is None:
                return None
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.result))])

        def make_client(**kwargs):
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        plugin = SimpleNamespace(
            clean_text=lambda text: text,
            _loaded_plugins={
                "mydiscordbot": SimpleNamespace(discord=SimpleNamespace(relay_message=self.discord.append))
            },
        )
        native = SimpleNamespace(thread=schedule(self.workers), next_frame=schedule(self.frames))
        channel = SimpleNamespace(reply=self.chat.append)
        namespace = {
            "minqlx": native,
            "Plugin": plugin,
            "CHAT_CHANNEL": channel,
            "json": json,
            "asyncio": asyncio,
            "datetime": datetime,
            "re": re,
            "AsyncOpenAI": make_client,
            "OpenAIError": SyntheticAPIError,
        }
        tree = ast.parse(SOURCE.read_bytes())
        original_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "openai_bot")
        selected = [n for n in original_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        selected = [n for n in selected if n.name in METHODS]
        self.assert_methods = {n.name for n in selected}
        isolated = ast.ClassDef(name="IsolatedBob", bases=[], keywords=[], body=selected, decorator_list=[])
        unit = ast.fix_missing_locations(ast.Module(body=[isolated], type_ignores=[]))
        exec(compile(unit, str(SOURCE), "exec"), namespace)
        self.namespace = namespace
        self.bot = namespace["IsolatedBob"]()
        self.bot._guard_comment_lock = Lock()
        self.bot.queue_lock = Lock()
        self.bot.plugins = {"openai_bot": self.bot}
        self.bot.bot_name = "Bob"
        self.bot.bot_clanprefix = ""
        self.bot.bot_api_key = "synthetic-unit-test-value"
        self.bot.model = "synthetic-existing-model"
        self.bot.max_tokens = 100
        self.bot.temperature = 0.7
        self.bot.top_p = 0.8
        self.bot.frequency_penalty = 0.1
        self.bot.presence_penalty = 0.2
        self.bot.logger = SimpleNamespace(debug=lambda *args: self.logs.append(args))
        contexts = {
            "role_balance_guard": ROLE.read_text(encoding="utf-8"),
            "role_chat": "You are {bot_name}. Use dry, restrained, deadpan English. Plain ASCII, one line.",
        }
        self.bot.read_context_for = lambda event: contexts.get(event, "")
        self.bot.system_context = lambda: "You are {bot_name}, the resident spectator. State: {game_state}"
        self.bot._ql_cleaned_up = lambda text: text

    def facts(self):
        return {
            "reason": "rating",
            "dominant_team": "blue",
            "score": {"red": 0, "blue": 5},
            "rule_threshold": 125,
            "action_performed": False,
        }

    def is_current(self):
        if self.stage != "frame":
            raise AssertionError("Game-state access must run on a game frame")
        self.validity_checks += 1
        return self.current

    def start(self):
        return self.bot.comment_balance_guard(self.facts(), self.is_current, self.finished.append)

    def worker(self):
        self.stage = "worker"
        self.workers.pop(0)()
        self.stage = "main"

    def frame(self):
        self.stage = "frame"
        self.frames.pop(0)()
        self.stage = "main"

    def complete(self):
        self.worker()
        self.frame()


class BobGuardTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_real_bridge_uses_existing_persona_model_parameters_and_new_role(self):
        h = self.h
        self.assertEqual(h.assert_methods, METHODS)
        self.assertTrue(h.start())
        self.assertEqual(h.requests, [])
        self.assertEqual(h.chat, [])
        h.worker()
        self.assertEqual(h.chat, [])
        self.assertEqual(h.validity_checks, 0)
        request = h.requests[0]
        self.assertEqual(request["model"], "synthetic-existing-model")
        self.assertEqual((request["temperature"], request["top_p"]), (0.7, 0.8))
        messages = request["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "system", "system", "user"])
        self.assertIn("resident spectator", messages[0]["content"])
        self.assertIn("You are Bob. Use dry, restrained, deadpan English.", messages[1]["content"])
        self.assertIn("existing chat persona", messages[2]["content"])
        self.assertIn("never claim you stopped", messages[2]["content"])
        self.assertEqual(json.loads(messages[3]["content"]), h.facts())
        h.frame()
        self.assertEqual(h.finished, [True])
        self.assertEqual(h.validity_checks, 1)
        self.assertIn("Bob", h.chat[0])
        self.assertNotIn("TEST", h.chat[0])
        self.assertNotIn("#1", h.chat[0])
        self.assertEqual(h.discord, [])
        self.assertFalse(h.bot._guard_comment_lock.locked())

    def test_only_one_guard_worker_or_undelivered_reply_can_exist(self):
        h = self.h
        self.assertTrue(h.start())
        self.assertFalse(h.start())
        self.assertEqual(len(h.workers), 1)
        h.worker()
        self.assertFalse(h.start())
        h.frame()
        self.assertTrue(h.start())

    def test_busy_chat_is_not_waited_on_and_does_not_call_api(self):
        h = self.h
        h.bot.queue_lock.acquire()
        h.start()
        h.complete()
        self.assertEqual(h.requests, [])
        self.assertEqual(h.finished, [False])
        self.assertTrue(h.bot.queue_lock.locked())
        h.bot.queue_lock.release()
        self.assertFalse(h.bot._guard_comment_lock.locked())

    def test_missing_guard_or_chat_role_skips_request_without_template_fallback(self):
        for missing in ("role_balance_guard", "role_chat"):
            h = Harness()
            read_context = h.bot.read_context_for
            h.bot.read_context_for = lambda event: "" if event == missing else read_context(event)
            h.start()
            h.complete()
            self.assertEqual(h.requests, [])
            self.assertEqual(h.chat, [])
            self.assertEqual(h.finished, [False])

    def test_none_empty_or_invalid_reply_is_silent_and_releases_locks(self):
        for text in (
            None,
            "",
            "   ",
            "x" * 241,
            "[TEST #1] Nope",
            "Das ist eine Simulation.",
            "Ich habe den Switch gestoppt.",
        ):
            with self.subTest(text=text):
                h = Harness()
                h.result = text
                h.start()
                h.complete()
                self.assertEqual(h.chat, [])
                self.assertEqual(h.finished, [False])
                self.assertFalse(h.bot.queue_lock.locked())
                self.assertFalse(h.bot._guard_comment_lock.locked())

    def test_api_error_logs_only_exception_category(self):
        h = self.h
        h.api_error = SyntheticAPIError("do-not-log-this-synthetic-exception-detail")
        h.start()
        h.complete()
        self.assertEqual(h.chat, [])
        self.assertEqual(h.finished, [False])
        self.assertNotIn("do-not-log-this", repr(h.logs))
        self.assertIn("SyntheticAPIError", repr(h.logs))

    def test_timeout_is_bounded_and_silent(self):
        h = self.h

        async def immediate_timeout(coroutine, *, timeout):
            self.assertEqual(timeout, 8)
            return await asyncio.wait_for(coroutine, timeout=0)

        h.namespace["asyncio"] = SimpleNamespace(run=asyncio.run, wait_for=immediate_timeout)
        h.start()
        h.complete()
        self.assertEqual(h.chat, [])
        self.assertEqual(h.finished, [False])
        self.assertFalse(h.bot._guard_comment_lock.locked())

    def test_stale_guard_or_reloaded_bob_discards_response(self):
        for replace_bob in (False, True):
            h = Harness()
            h.start()
            h.worker()
            if replace_bob:
                h.bot.plugins["openai_bot"] = object()
            else:
                h.current = False
            h.frame()
            self.assertEqual(h.chat, [])
            self.assertEqual(h.finished, [False])

    def test_prompt_format_failure_logs_no_content_and_makes_no_request(self):
        h = self.h
        h.bot.system_context = lambda: "synthetic-private-content {unsupported_field}"
        h.start()
        h.complete()
        self.assertEqual(h.requests, [])
        self.assertNotIn("synthetic-private-content", repr(h.logs))
        self.assertIn("KeyError", repr(h.logs))
        self.assertEqual(h.finished, [False])

    def test_dispatch_and_frame_scheduler_failures_release_gate(self):
        h = self.h
        self.assertFalse(h.bot.comment_balance_guard({"invalid": object()}, h.is_current, h.finished.append))
        self.assertFalse(h.bot._guard_comment_lock.locked())

        def cannot_schedule(function):
            def fail():
                raise RuntimeError("synthetic-scheduler-detail")

            return fail

        h.namespace["minqlx"].next_frame = cannot_schedule
        h.start()
        h.worker()
        self.assertFalse(h.bot._guard_comment_lock.locked())
        self.assertNotIn("synthetic-scheduler-detail", repr(h.logs))
        self.assertEqual(h.chat, [])

    def test_existing_chat_send_still_relays_by_default(self):
        h = self.h
        h.bot._send_message(h.namespace["CHAT_CHANNEL"], "Existing chat response")
        self.assertEqual(len(h.chat), 1)
        self.assertEqual(len(h.discord), 1)


if __name__ == "__main__":
    unittest.main()
