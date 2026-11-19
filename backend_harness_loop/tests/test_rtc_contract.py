import unittest

from backend_harness_loop.rtc_contract import ContractError, extract_interrupt, validate_callback


def callback(text="请开始验证。", round_id=0):
    return {"round_id": round_id, "messages": [{"role": "user", "content": text}]}


def interrupt(round_id=0):
    return {"TaskId": "owned-task", "UserID": "client-user", "RoundID": round_id, "Stage": {"Code": 4}}


class RTCContractTests(unittest.TestCase):
    def test_zero_round_is_valid_and_normalized_consistently(self):
        self.assertEqual(validate_callback(callback()), ("0", "请开始验证。"))
        self.assertEqual(validate_callback(callback(round_id="0"))[0], "0")
        self.assertEqual(extract_interrupt("conv", interrupt(), "owned-task", "client-user"), "0")

    def test_only_final_user_text_is_ingested_and_provider_settings_are_ignored(self):
        payload = callback("当前输入", "round-2")
        payload["messages"] = [
            {"role": "system", "content": "UNTRUSTED PROVIDER INSTRUCTIONS"},
            {"role": "user", "content": "earlier-input"},
            {"role": "assistant", "content": "earlier-output"},
            {"role": "user", "content": "当前输入"},
        ]
        payload.update(model={"untrusted": "model-selection"}, tools=["not-runtime-tools"], stream=False)
        self.assertEqual(validate_callback(payload), ("round-2", "当前输入"))

    def test_does_not_search_backwards_and_replay_user_after_assistant_or_tool(self):
        for role in ("assistant", "tool", "system", None, 1):
            payload = callback()
            payload["messages"].append({"role": role, "content": "PRIVATE CONTENT"})
            with self.subTest(role=role), self.assertRaises(ContractError) as error:
                validate_callback(payload)
            self.assertEqual(error.exception.code, "callback_requires_final_user")
            self.assertNotIn("PRIVATE CONTENT", repr(error.exception))

    def test_message_shape_and_count_are_bounded(self):
        for messages in (None, {}, "messages", [], [None], [{}], [callback()["messages"][0]] * 129):
            with self.subTest(kind=type(messages).__name__), self.assertRaises(ContractError):
                validate_callback({"round_id": 0, "messages": messages})
        payload = callback()
        payload["messages"] = [{}] * 127 + payload["messages"]
        self.assertEqual(validate_callback(payload)[0], "0")

    def test_user_text_is_nonempty_string_and_keeps_original_whitespace(self):
        for content in (None, False, 1, {}, [], "", "  \n\t", "\ud800"):
            with self.subTest(kind=type(content).__name__), self.assertRaises(ContractError):
                validate_callback(callback(content))
        self.assertEqual(validate_callback(callback("  原始格式\n"))[1], "  原始格式\n")

    def test_user_text_limit_counts_utf8_bytes(self):
        self.assertEqual(len(validate_callback(callback("x" * 8192))[1]), 8192)
        self.assertEqual(validate_callback(callback("你" * 2730))[1], "你" * 2730)
        for content in ("x" * 8193, "你" * 2731):
            with self.subTest(size=len(content)), self.assertRaises(ContractError) as error:
                validate_callback(callback(content))
            self.assertEqual(error.exception.status, 413)
            self.assertNotIn(content, str(error.exception))

    def test_invalid_round_types_and_bounds_are_safe(self):
        for value in (None, False, True, 0.0, float("nan"), [], {}, "", " ", " round ", "\n", "x" * 129, -1, 10**1000, "\ud800"):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ContractError) as error:
                validate_callback(callback(round_id=value))
            self.assertEqual(error.exception.code, "invalid_round_id")
        self.assertEqual(validate_callback(callback(round_id="provider:round-1"))[0], "provider:round-1")

    def test_non_object_callback_uses_safe_machine_error(self):
        for value in (None, [], "PRIVATE CONTENT", False):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ContractError) as error:
                validate_callback(value)
            self.assertEqual(error.exception.status, 400)
            self.assertNotIn("PRIVATE CONTENT", str(error.exception))

    def test_interrupt_requires_exact_task_and_client_user_not_agent(self):
        payload = interrupt()
        self.assertIsNone(extract_interrupt("conv", payload, "other-task", "client-user"))
        self.assertIsNone(extract_interrupt("conv", payload, "owned-task", "agent-user"))
        self.assertIsNone(extract_interrupt("conv", {**payload, "UserID": "agent-user"}, "owned-task", "client-user"))
        self.assertIsNone(extract_interrupt("conv", payload, "", "client-user"))

    def test_only_stage_four_conv_is_interrupt(self):
        for tag in ("subv", None, {}, []):
            self.assertIsNone(extract_interrupt(tag, interrupt(), "owned-task", "client-user"))
        for code in (None, 0, 1, 2, 3, 5, True, "4", 4.0, [], {}):
            with self.subTest(code=type(code).__name__):
                self.assertIsNone(extract_interrupt("conv", {**interrupt(), "Stage": {"Code": code}}, "owned-task", "client-user"))

    def test_missing_or_invalid_interrupt_fields_never_become_wildcard_cancel(self):
        for field in ("TaskId", "UserID", "RoundID", "Stage"):
            payload = interrupt()
            del payload[field]
            self.assertIsNone(extract_interrupt("conv", payload, "owned-task", "client-user"))
        for value in (None, False, [], {}, -1, 10**1000):
            self.assertIsNone(extract_interrupt("conv", interrupt(value), "owned-task", "client-user"))
        self.assertIsNone(extract_interrupt("conv", None, "owned-task", "client-user"))


if __name__ == "__main__":
    unittest.main()
