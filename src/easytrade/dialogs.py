"""Read-only dialog classification. Observations never submit or confirm orders."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class DialogMeaning:
    kind: str
    broker_order_id: str | None = None
    requires_user_action: bool = True
    # A success prompt does not identify the originating signal or confirm fills.
    receipt_binding_verified: bool = False


def classify_dialog(title: str, text: str) -> DialogMeaning:
    title = unicodedata.normalize("NFKC", title)
    body = unicodedata.normalize("NFKC", text)
    combined = title + "\n" + body
    if re.search(r"验证码|校验码", combined):
        return DialogMeaning("CAPTCHA")
    if re.search(r"登录|登陆|密码|身份认证", combined):
        return DialogMeaning("AUTHENTICATION")
    if re.search(r"撤单确认|确认撤单|是否.*撤单", combined):
        return DialogMeaning("CANCEL_CONFIRMATION")
    if re.search(r"委托确认|买入确认|卖出确认|是否.*(?:买入|卖出|委托)", combined):
        return DialogMeaning("ORDER_CONFIRMATION")
    if re.search(r"失败|错误|不足|不允许|超出|无法|拒绝|未成功|未通过|废单", combined):
        return DialogMeaning("REJECTION")
    if re.search(r"撤单.*(?:已提交|已受理|申报成功)|撤单请求.*(?:提交|受理)", combined):
        return DialogMeaning("CANCEL_REQUEST_ACCEPTED")
    if re.search(r"撤单成功|委托已撤销", combined):
        return DialogMeaning("CANCEL_RESULT")
    if re.search(r"委托成功|委托已受理|申报成功|委托已提交|委托(?:已)?成功提交", combined):
        labels = re.findall(r"委托编号|合同编号|委托序号|合同号|委托号", text)
        tokens = re.findall(r"(?:委托编号|合同编号|委托序号|合同号|委托号)\s*[:：]\s*([^\s,，;；)）\]】]+)", text)
        # The observed client ends the complete contract ID with a Chinese full
        # stop. Remove exactly that terminator, never ellipses or an ASCII dot.
        tokens = [token[:-1] if token.endswith("。") else token for token in tokens]
        # Refuse missing, truncated, contradictory or unexpected identifiers.
        if tokens and len(labels) == len(tokens) and len(set(tokens)) == 1 and re.fullmatch(r"[A-Za-z0-9]{1,128}", tokens[0]):
            return DialogMeaning("ORDER_ACCEPTED", tokens[0])
    return DialogMeaning("UNKNOWN")
