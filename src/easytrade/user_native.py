"""Native execution used only by the user's local test console.

No AI calls in this path. Unknown modal text is deliberately left on screen.
The local buy-confirm and success texts have been observed. Success auto-close
needs another user-run validation; sell/cancel remain under test.
"""
from __future__ import annotations

import re
import time
import json
from decimal import Decimal
from uuid import uuid4

from .domain import OrderPlan, ValidationError, utc_now
from .ths import ThsDraftAdapter
from .read_metrics import read_operation
from .adapters import BrokerRejectedError
from .dialogs import explicit_submit_rejection


def positive_button_id(buttons, *, kind=None):
    candidates = []
    for button in buttons:
        label = re.sub(r"\s", "", button["text"].replace("&", ""))
        label = re.sub(r"[（(][Yy][）)]", "", label)
        allowed = {1, 6}
        if kind == "ORDER_ACCEPTED":
            allowed.add(2)  # observed success-information dialog, not a trade confirmation
        if button["id"] in allowed and label in {"确定", "确认", "是"}:
            candidates.append(button["id"])
    if len(candidates) != 1:
        raise ValidationError("未找到唯一已知的确认按钮；保留弹窗")
    return candidates[0]


def confirmation_matches(text: str, plan: OrderPlan) -> bool:
    """Require labelled fields, rather than finding unrelated numbers in a dialog."""
    # This client exposes its rich Static label as literal font markup.
    # Strip only that observed formatting; unknown markup is not silently accepted.
    text = re.sub(r"</?font\b[^>]*>", "", text, flags=re.IGNORECASE)
    if "<" in text or ">" in text:
        return False
    side, opposite = ("买入", "卖出") if plan.side == "buy" else ("卖出", "买入")
    if side not in text or opposite in text:
        return False
    fields = {
        "code": re.findall(r"(?:证券代码|股票代码)\s*[:：]\s*([0-9]{6})(?![0-9])", text),
        "price": re.findall(r"(?:委托价格|买入价格|卖出价格)\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)(?![0-9.,])", text),
        "quantity": re.findall(r"(?:委托数量|买入数量|卖出数量)\s*[:：]\s*([0-9]+)(?![0-9,.])", text),
    }
    return (fields["code"] == [plan.symbol[:6]]
            and len(fields["price"]) == 1 and Decimal(fields["price"][0]) == Decimal(plan.limit_price)
            and fields["quantity"] == [str(plan.quantity)])


def matching_row(snapshot, plan: dict, broker_id: str) -> dict:
    rows = [row for row in snapshot.rows if row.get("order_id") == broker_id]
    if len(rows) != 1:
        raise ValidationError("当前可见委托表没有唯一完整编号；不会按价格或行号猜单")
    row = rows[0]
    if (row.get("symbol") != plan["symbol"][:6] or row.get("side") != plan["side"]
            or row.get("quantity") != plan["quantity"] or "limit_price" not in row
            or Decimal(row["limit_price"]) != Decimal(plan["limit_price"])):
        raise ValidationError("委托编号对应的代码、方向、数量或价格与本地计划不符")
    return row


class ThsUserAdapter(ThsDraftAdapter):
    mode = "ths-user"

    def _check_resources(self, plan):
        # This console executes explicit user instructions. Let the broker
        # validate current cash/sellable quantity, rather than blocking on a
        # potentially incomplete OCR position page or the local order ledger.
        pass

    def _active_dialog(self, *, activate=False):
        import win32gui
        foreground = win32gui.GetAncestor(win32gui.GetForegroundWindow(), 2)
        owned = []
        observations = self.observe_dialogs()
        diagnostic_path = self.profile.get("console_diagnostics")
        # Timestamp fields change on each observation; log state changes only.
        stable = [{k: v for k, v in row.items() if k != "observed_at"} for row in observations["dialogs"]]
        if diagnostic_path and stable != getattr(self, "_last_dialog_state", None):
            from pathlib import Path
            path = Path(diagnostic_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(json.dumps({"at": utc_now().isoformat(), "dialogs": observations["dialogs"]}, ensure_ascii=False) + "\n")
            self._last_dialog_state = stable
        for item in observations["dialogs"]:
            owner, seen = item.get("owner", 0), set()
            while owner and owner != self.main.handle and owner not in seen:
                seen.add(owner)
                owner = win32gui.GetWindow(owner, 4)
            if owner == self.main.handle:
                if (item.get("window_class") in {"CPPToolTip", "tooltips_class32"}
                        and item["handle"] != foreground and not item.get("buttons")
                        and not item.get("read_error")):
                    continue  # observed noninteractive hint, not another confirmation
                owned.append(item)
        if len(owned) > 1:
            raise ValidationError("存在多层交易弹窗，停止自动确认")
        if owned:
            if activate and owned[0]["handle"] != foreground:
                from pywinauto import Desktop
                Desktop(backend="win32").window(handle=owned[0]["handle"]).set_focus()
                foreground = win32gui.GetAncestor(win32gui.GetForegroundWindow(), 2)
            if owned[0]["handle"] != foreground:
                raise ValidationError("交易弹窗不是前台窗口")
            return owned[0]
        if not self.main.is_enabled():
            raise ValidationError("交易主窗口被阻塞，但未识别到其确认框；保留现场")
        if activate and not self._is_foreground():
            self.main.restore()
            self.main.set_focus()
        if not self._is_foreground():
            raise ValidationError("交易主窗口不在前台；没有点击确认或重发买卖")
        return None

    def resume_confirmation(self, plan: OrderPlan, persist_receipt, *, created_at=None, persist_hint=None):
        """User explicitly resumes the visible original confirmation; no new submit."""
        if plan.account != self.profile["account_alias"]:
            raise ValidationError("原确认的账户与配置不匹配")
        self._attach()  # connect() cannot activate a disabled modal parent
        item = self._active_dialog(activate=True)
        if item and item["kind"] == "ORDER_ACCEPTED":
            if not created_at or persist_hint is None:
                raise ValidationError("成功提示需保存编号线索后核对原委托记录，不能直接绑定成交")
            if not self.main.is_visible():
                raise ValidationError("交易主窗口不可见")
            self._assert_caption_identity()
            broker_id = item.get("broker_order_id")
            if not broker_id:
                raise ValidationError("成功提示没有完整合同编号")
            persist_hint(broker_id)  # a durable hint, not an accepted/fill receipt
            self._click_positive(item)
            order = {"plan": plan.payload(), "broker_order_id": None, "created_at": created_at}
            receipt = self.lookup_receipt(order, broker_id)
            persist_receipt(receipt)
            return
        if not item or item["kind"] != "ORDER_CONFIRMATION":
            raise ValidationError("原委托确认框已经不在，无法继续确认；若仅点过否/关闭且确定从未确认提交，可用“核对未提交”；已点是或不确定应查询完整委托号。没有重新下单")
        if not confirmation_matches(item.get("title", "") + "\n" + item.get("text", ""), plan):
            raise ValidationError("原确认框字段与选中订单不匹配，停止")
        if not self.main.is_visible():
            raise ValidationError("交易主窗口不可见，不能核对账户")
        self._assert_caption_identity()
        current = self._active_dialog()
        if not current or any(current.get(k) != item.get(k) for k in ("handle", "kind", "text", "title")):
            raise ValidationError("账户核对期间原确认框发生变化")
        receipt, dialog = self._await_submit_result(plan)
        persist_receipt(receipt)
        self._click_positive(dialog)

    def verify_unsubmitted_resolution(self, plan):
        """Validate account and an unblocked scene, not proof of broker absence.

        Only the user's explicit declaration permits local resolution. An empty
        visible table is never treated as proof that an order was not submitted.
        """
        if plan.account != self.profile["account_alias"]:
            raise ValidationError("待核对记录与客户端账户不匹配")
        self.connect()
        if self._active_dialog() is not None:
            raise ValidationError("客户端仍有交易弹窗，不能解除记录；先核对该原确认")
        self._assert_identity()
        return {"account_verified": True, "unblocked": True, "broker_absence_proven": False}

    def _click_positive(self, observation):
        from pywinauto import Desktop
        # Read again immediately before acting; do not trust a stale HWND/text.
        current = self._active_dialog()
        if not current or any(current.get(k) != observation.get(k) for k in ("handle", "text", "title", "kind")):
            raise ValidationError("弹窗核对期间内容改变")
        button_id = positive_button_id(current.get("buttons", []), kind=current.get("kind"))
        dialog = Desktop(backend="win32").window(handle=current["handle"])
        button = dialog.child_window(control_id=button_id, class_name="Button").wrapper_object()
        if not button.is_visible() or not button.is_enabled():
            raise ValidationError("弹窗按钮不可用")
        button.click()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            next_dialog = self._active_dialog()
            if not next_dialog or any(next_dialog.get(k) != current.get(k) for k in ("handle", "kind", "text", "title")):
                return
            time.sleep(0.05)
        raise ValidationError("确认后弹窗未关闭；不会重复点击")

    def _await_submit_result(self, plan):
        deadline, confirmed = time.monotonic() + self.timeout, False
        while time.monotonic() < deadline:
            item = self._active_dialog()
            if item is None:
                time.sleep(0.1)
                continue
            kind = item["kind"]
            if kind == "ORDER_CONFIRMATION" and not confirmed:
                if not confirmation_matches(item.get("title", "") + "\n" + item.get("text", ""), plan):
                    raise ValidationError("确认弹窗的代码、方向、价格或数量不能完整核对；保留弹窗")
                confirmed = True  # never confirm a second order prompt
                self._click_positive(item)
                continue
            if kind == "ORDER_ACCEPTED" and item.get("broker_order_id"):
                receipt = {"receipt_id": "native-" + uuid4().hex, "account": plan.account,
                           "symbol": plan.symbol, "side": plan.side,
                           "broker_order_id": item["broker_order_id"], "status": "BROKER_ACCEPTED",
                           "filled_quantity": 0, "average_price": "0", "observed_at": utc_now().isoformat(),
                           "source": item.get("source", "win32")}
                # Persist the ID in the caller before dismissing the success dialog.
                return receipt, item
            if kind == "REJECTION" and confirmed:
                rejection = explicit_submit_rejection(item.get("text", ""))
                if rejection:
                    raise BrokerRejectedError(item["text"], code=rejection["code"], source=item.get("source", "win32"))
            raise ValidationError(f"弹窗需要核对：{kind}；{item.get('text', '')[:250]}")
        raise ValidationError("提交后未取得含完整委托编号的回执；禁止重发")

    def submit_prepared(self, plan: OrderPlan, instrument, persist_receipt):
        self._assert_identity()
        if not instrument.name and getattr(self, "_prepared_security", None) != {
                "code": plan.symbol[:6], "name": self._security_name(instrument), "side": plan.side}:
            raise ValidationError("提交前客户端证券身份与本次准备记录不匹配")
        tree = self._control(self.profile.get("tree_control_id", 129), "SysTreeView32")
        if not tree.get_item(self.profile["pages"][plan.side]).is_selected():
            raise ValidationError("提交前交易页发生变化")
        if (self._read_edit(1032) != plan.symbol[:6] or self._read_edit(1034) != str(plan.quantity)
                or Decimal(self._read_edit(1033)) != Decimal(plan.limit_price)
                or not self._security_name(instrument)):
            raise ValidationError("提交前字段回读改变")
        button = self._control(1006, "Button")
        expected = "买入" if plan.side == "buy" else "卖出"
        if expected not in button.window_text() or ("卖出" if plan.side == "buy" else "买入") in button.window_text():
            raise ValidationError("提交按钮方向与计划不一致")
        self._assert_identity()
        button.click()  # The only buy/sell submit action. Called once, never retried.
        receipt, dialog = self._await_submit_result(plan)
        persist_receipt(receipt)
        self._click_positive(dialog)

    def lookup_receipt(self, order, broker_id=None):
        from .adapters import ForegroundChangedError
        # Only this read-only lookup can retry a focus interruption. Never
        # retry submission, confirmation, cancellation or draft preparation.
        for attempt in range(2):
            try:
                return self._lookup_receipt_once(order, broker_id)
            except ForegroundChangedError as error:
                if attempt:
                    raise ForegroundChangedError("回执查询因前台切换停止，已只读重试一次；请暂不切换窗口后再刷新委托状态") from error

    def _lookup_receipt_once(self, order, broker_id=None):
        if order["plan"]["account"] != self.profile["account_alias"]:
            raise ValidationError("订单账户与原生客户端配置不匹配")
        broker_id = broker_id or order["broker_order_id"]
        if not broker_id or not re.fullmatch(r"[A-Za-z0-9]{1,128}", broker_id):
            raise ValidationError("需要完整委托编号；未知订单不能靠相同价格猜测")
        # This page is 当日委托. Do not bind recycled IDs from another trading day.
        from datetime import timedelta, timezone
        from .domain import timestamp
        zone = timezone(timedelta(hours=8))
        if timestamp(order["created_at"]).astimezone(zone).date() != utc_now().astimezone(zone).date():
            raise ValidationError("跨日订单需要历史委托核对，不能使用当日表绑定")
        self.connect()
        # Force a harmless page change before reading, instead of trusting a
        # previously selected query tab to refresh itself during polling.
        self._page(self.profile["pages"][order["plan"]["side"]])
        snapshot = self.read_table("orders")
        return self.receipt_from_snapshot(order, broker_id, snapshot)

    @read_operation("orders")
    def read_orders(self):
        """Read current broker orders, including orders created outside this app."""
        from .adapters import ForegroundChangedError
        for attempt in range(2):
            try:
                return self._read_orders_once()
            except ForegroundChangedError as error:
                if attempt:
                    raise ForegroundChangedError("委托表查询因前台切换停止，已只读重试一次；请暂不切换窗口后再刷新") from error

    def _read_orders_once(self):
        self.connect()
        self._page(self.profile["pages"]["positions"])
        return self.read_all_table("orders")

    def receipt_from_snapshot(self, order, broker_id, snapshot):
        from datetime import timedelta, timezone
        from .domain import timestamp
        zone = timezone(timedelta(hours=8))
        if (order["plan"]["account"] != self.profile["account_alias"]
                or timestamp(order["created_at"]).astimezone(zone).date() != timestamp(snapshot.observed_at).astimezone(zone).date()):
            raise ValidationError("委托快照的账户或日期不能与本地订单绑定")
        row = matching_row(snapshot, order["plan"], broker_id)
        filled = row["filled_quantity"]
        average = "0"
        if filled:
            if Decimal(str(row.get("average_price", "0"))) > 0:
                average = row["average_price"]
            else:
                trades = self.read_table("trades")
                matches = [r for r in trades.rows if r["order_id"] == broker_id]
                if (not matches or len({r["trade_id"] for r in matches}) != len(matches)
                        or any(r["symbol"] != row["symbol"] or r["side"] != row["side"] for r in matches)
                        or sum(r["filled_quantity"] for r in matches) != filled):
                    raise ValidationError("可见成交明细不足以核对累计成交和均价")
                average = str(sum(Decimal(r["average_price"]) * r["filled_quantity"] for r in matches) / filled)
        return {"receipt_id": "query-" + uuid4().hex, "account": order["plan"]["account"],
                "symbol": order["plan"]["symbol"], "side": order["plan"]["side"],
                "broker_order_id": broker_id, "status": row["status"], "filled_quantity": filled,
                "average_price": str(average), "observed_at": snapshot.observed_at, "source": "ocr"}

    def cancel(self, order, persist_intent):
        """Select one exact OCR ID on the cancel page; never invoke '撤全部'."""
        if order["plan"]["account"] != self.profile["account_alias"]:
            raise ValidationError("撤单账户与原生客户端配置不匹配")
        broker_id = order["broker_order_id"]
        if not broker_id:
            raise ValidationError("没有完整委托编号，禁止撤单")
        self.connect()
        self._page(self.profile["pages"].get("cancel", ["撤单[F3]"]))
        grid = self._control(self.profile.get("grid_control_id", 1047), self.profile.get("grid_class", "CVirtualGridCtrl"))
        image = grid.capture_as_image()
        scale = self.ocr_scales.get("orders", 2)
        image = image.resize((image.width * scale, image.height * scale))
        snapshot = self.reader.read(image, "orders", utc_now().isoformat())
        row = matching_row(snapshot, order["plan"], broker_id)
        if row["status"] not in {"BROKER_ACCEPTED", "PARTIALLY_FILLED"}:
            raise ValidationError("该委托已经终结或不处于可撤状态")
        result = self.reader._recognize(image)
        boxes = [box for text, score, box in zip(result.txts, result.scores, result.boxes)
                 if str(text).strip() == broker_id and score >= self.reader.confidence]
        if len(boxes) != 1:
            raise ValidationError("没有唯一完整编号的屏幕位置，禁止按行序猜测")
        box = boxes[0]
        x = sum(float(p[0]) for p in box) / len(box) / scale
        y = sum(float(p[1]) for p in box) / len(box) / scale
        # Revalidate the entire image immediately before clicking to reject scrolling/updates.
        self._assert_identity()
        fresh = grid.capture_as_image().resize(image.size)
        if fresh.tobytes() != image.tobytes():
            raise ValidationError("撤单表格发生变化，请重新查询")
        persist_intent()  # durable UNKNOWN before the first possible cancellation action
        grid.double_click_input(coords=(round(x), round(y)))
        deadline, confirmed = time.monotonic() + self.timeout, False
        while time.monotonic() < deadline:
            item = self._active_dialog()
            if item is None:
                time.sleep(0.1)
                continue
            if item["kind"] == "CANCEL_CONFIRMATION" and not confirmed:
                ids = re.findall(r"(?:委托编号|合同编号|委托序号|合同号|委托号)\s*[:：]\s*([A-Za-z0-9]+)", item.get("text", ""))
                if ids != [broker_id]:
                    raise ValidationError("撤单弹窗未给出唯一匹配编号；保留弹窗")
                confirmed = True
                self._click_positive(item)
            elif confirmed and item["kind"] in {"CANCEL_REQUEST_ACCEPTED", "CANCEL_RESULT"}:
                self._click_positive(item)
                return  # still must query the actual order, not infer CANCELLED
            else:
                raise ValidationError(f"未知撤单弹窗：{item['kind']}，停止")
        raise ValidationError("撤单结果待核对；不会再次发送撤单")
