import unittest

from easytrade.dialogs import classify_dialog


class DialogTests(unittest.TestCase):
    def test_confirmation_is_never_acceptance(self):
        for title in ("委托确认", "买入确认", "卖出确认"):
            result = classify_dialog(title, "是否买入159570，100份，委托编号:000123")
            self.assertEqual(result.kind, "ORDER_CONFIRMATION")
            self.assertIsNone(result.broker_order_id)
        self.assertEqual(classify_dialog("撤单确认", "是否撤单？").kind, "CANCEL_CONFIRMATION")

    def test_explicit_success_preserves_alphanumeric_order_id(self):
        result = classify_dialog("提示", "委托成功！委托编号：00015O33")
        self.assertEqual(result.kind, "ORDER_ACCEPTED")
        self.assertEqual(result.broker_order_id, "00015O33")
        self.assertFalse(result.receipt_binding_verified)
        self.assertTrue(result.requires_user_action)

    def test_observed_submit_success_with_sentence_terminator(self):
        for side in ("买入", "卖出"):
            text = f"您的{side}委托已成功提交，合同编号：000123AB。\n提示"
            result = classify_dialog("", text)
            self.assertEqual(result.kind, "ORDER_ACCEPTED")
            self.assertEqual(result.broker_order_id, "000123AB")
            self.assertFalse(result.receipt_binding_verified)
        for token in ("123...。", "123…。", "123。。", "１２３。", "123.。"):
            self.assertEqual(classify_dialog("提示", "委托已成功提交，合同编号：" + token).kind, "UNKNOWN")

    def test_missing_truncated_and_conflicting_ids_remain_unknown(self):
        for body in ("委托成功", "操作成功", "委托成功 委托编号:123...", "委托成功 委托编号:123…",
                     "委托成功 委托编号:123，合同编号:456", "委托成功 委托编号:123，合同编号:123...",
                     "委托成功 委托编号:１２３", "委托成功 委托编号:123，合同编号 456"):
            with self.subTest(body=body):
                self.assertEqual(classify_dialog("提示", body).kind, "UNKNOWN")

    def test_rejection_takes_precedence_over_success(self):
        for body in ("可用资金不足", "委托失败", "未成功", "委托成功查询失败，合同编号:123"):
            self.assertEqual(classify_dialog("提示", body).kind, "REJECTION")

    def test_cancellation_request_is_distinct_from_result(self):
        self.assertEqual(classify_dialog("提示", "撤单申报成功").kind, "CANCEL_REQUEST_ACCEPTED")
        self.assertEqual(classify_dialog("提示", "撤单成功").kind, "CANCEL_RESULT")

    def test_authentication_and_captcha_are_handed_back(self):
        self.assertEqual(classify_dialog("保护验证码", "请输入验证码").kind, "CAPTCHA")
        self.assertEqual(classify_dialog("重新登录", "请输入密码").kind, "AUTHENTICATION")


if __name__ == "__main__":
    unittest.main()
