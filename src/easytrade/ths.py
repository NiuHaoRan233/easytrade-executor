from __future__ import annotations

import math
import hashlib
import re
import time
from decimal import Decimal
from pathlib import Path

from .adapters import DraftCleanupError, PreparationError, ForegroundChangedError
from .domain import Instrument, OrderPlan, utc_now, decimal_value, ValidationError, validate_symbol
from .dialogs import classify_dialog
from .tables import OcrTableReader
from .read_metrics import read_operation, read_step, measure


class TableScrollBar:
    """A separate Win32 scroll control sends notifications to its own parent."""

    def __init__(self, control):
        self.control = control
        self.handle = control.handle
        self.scroll_bar_type = 2  # SB_CTL; window nonclient bars use SB_VERT=1

    def scroll(self, direction, amount, **kwargs):
        codes = {("up", "end"): 6, ("down", "line"): 1}
        self.control.parent().send_message(0x0115, codes[(direction, amount)], self.handle)  # WM_VSCROLL


class ThsDraftAdapter:
    """Prepares fields and captures read-only tables. Has no submit/cancel methods."""

    mode = "ths-draft"

    def __init__(self, profile: dict):
        self.profile = profile
        self.app = None
        self.main = None
        self._verified_caption_digest = None
        self.reader = OcrTableReader(float(profile.get("ocr_min_confidence", 0.85)))
        self.timeout = float(profile.get("timeout_seconds", 8))
        self.ocr_scales = profile.get("ocr_scale_by_role", {"orders": 2, "trades": 2})
        if (not isinstance(self.ocr_scales, dict) or set(self.ocr_scales) - {"positions", "orders", "trades"}
                or any(type(value) is not int or value not in (1, 2, 3) for value in self.ocr_scales.values())):
            raise PreparationError("ocr_scale_by_role必须为表格类型到1、2或3的映射")
        # Like easytrader's editor switch, allow known clients to skip an
        # unsupported setter. Keep our clear/read-back checks in every mode.
        self.editor_input_mode = profile.get("editor_input_mode", "auto")
        if self.editor_input_mode not in {"auto", "set_text", "keys"}:
            raise PreparationError("editor_input_mode必须为auto、set_text或keys")
        self.editor_set_timeout = float(profile.get("editor_set_timeout_seconds", 0.3))
        if not math.isfinite(self.editor_set_timeout) or self.editor_set_timeout <= 0:
            raise PreparationError("editor_set_timeout_seconds必须为有限正数")

    @read_step("attach")
    def _attach(self):
        from pywinauto import Application
        from pywinauto.application import process_module
        import win32process
        path = self.profile["exe_path"]
        if not Path(path).is_file():
            raise PreparationError("客户端路径不存在")
        # Search the unique main window first. connect(path=...) enumerates the
        # executable of every process on the machine on every refresh.
        handles = measure(self, "window_search", self._main_window_handles)
        if len(handles) != 1:
            raise PreparationError("交易主窗口不唯一或进程不匹配")
        handle = handles[0]
        process = win32process.GetWindowThreadProcessId(handle)[1]
        actual_path = measure(self, "exe_verify", lambda: process_module(process))
        if Path(actual_path).resolve() != Path(path).resolve():
            raise PreparationError("交易主窗口程序路径与配置不匹配")
        app = measure(self, "window_bind", lambda: Application(backend="win32").connect(handle=handle))
        if app.process != process or win32process.GetWindowThreadProcessId(handle)[1] != process:
            raise PreparationError("交易主窗口不唯一或进程不匹配")
        self.app = app
        self.main = app.window(handle=handle)

    def _main_window_handles(self):
        import win32gui
        pattern = re.compile(self.profile["title_regex"])
        matches = []
        def inspect(handle, _):
            # Filter hidden windows before reading text, without constructing
            # wrappers/reading rich text for every application on the desktop.
            if win32gui.IsWindowVisible(handle) and pattern.match(win32gui.GetWindowText(handle)):
                matches.append(handle)
            return True
        win32gui.EnumWindows(inspect, None)
        return matches

    @read_step("connect")
    def connect(self):
        self._attach()
        self.main.restore()
        self.main.set_focus()
        self._assert_identity()

    def observe_dialogs(self):
        """Inspect same-process top-level windows, without activating or clicking.

        This works even when a modal prevents the normal identity check. Such
        observations are explicitly unbound and cannot confirm any local order.
        """
        from dataclasses import asdict
        from pywinauto import Desktop
        import win32gui
        if self.app is None or self.main is None:
            self._attach()
        observations = []
        for window in Desktop(backend="win32").windows(process=self.app.process, visible_only=True):
            if window.handle == self.main.handle:
                continue
            entry = {"handle": window.handle, "account_verified": False, "observed_at": utc_now().isoformat()}
            try:
                rect = window.rectangle()
                entry.update(window_class=window.class_name(), size=[rect.width(), rect.height()],
                             owner=win32gui.GetWindow(window.handle, 4))
                title = window.window_text()
                text = "\n".join(control.window_text().strip() for control in window.descendants(class_name="Static")
                                 if control.is_visible() and control.window_text().strip())
                source = "win32"
                if not text and win32gui.GetAncestor(win32gui.GetForegroundWindow(), 2) == window.handle:
                    text = self.reader.read_text(window.capture_as_image())
                    source = "ocr"
                entry.update(title=title, text=text, source=source,
                             buttons=[{"id": button.control_id(), "text": button.window_text()}
                                      for button in window.descendants(class_name="Button") if button.is_visible()],
                             **asdict(classify_dialog(title, text)))
            except Exception as error:
                entry.update(kind="UNKNOWN", read_error=type(error).__name__, requires_user_action=True,
                             receipt_binding_verified=False)
            observations.append(entry)
        return {"dialogs": observations, "submitted": False, "account_verified": False}

    def _is_foreground(self):
        if self.main.has_focus():
            return True
        # Some THS controls become the foreground HWND themselves. Accept only
        # descendants of this exact main window, never an unrelated app/dialog.
        handle = getattr(self.main, "handle", None)
        if handle is None:
            return False
        import win32gui
        return win32gui.GetAncestor(win32gui.GetForegroundWindow(), 2) == handle

    @read_step("identity")
    def _assert_identity(self):
        if not self.main.is_enabled():
            raise PreparationError("交易窗口被弹窗阻塞；请人工处理后重新核对")
        if not self.main.is_visible():
            raise PreparationError("交易窗口不可见")
        if not self._is_foreground():
            raise ForegroundChangedError("交易窗口不是前台窗口；停止输入和截图")
        self._assert_caption_identity()
        if not self._is_foreground():
            raise ForegroundChangedError("身份核对期间前台窗口已变化；查询时请暂不切换窗口")

    def _assert_caption_identity(self):
        """Verify account caption. Caller separately proves foreground/ownership."""
        title = self.main.window_text()
        fragments = self.profile["title_contains"]
        if not fragments or any(not fragment for fragment in fragments):
            raise PreparationError("必须配置账户和券商身份片段")
        if not all(fragment in title for fragment in fragments):
            box = self.profile.get("caption_identity_box")
            if box is None:
                raise PreparationError("交易窗口账户/券商与配置不匹配")
            from .screen_capture import capture_region
            rect = self.main.rectangle()
            if (not isinstance(box, list) or len(box) != 4 or any(type(x) is not int for x in box)
                    or not 0 <= box[0] < box[2] <= rect.width()
                    or not 0 <= box[1] < box[3] <= rect.height()):
                raise PreparationError("身份标题截图范围无效")
            caption = measure(self, "caption_capture", lambda: capture_region(
                (rect.left + box[0], rect.top + box[1], rect.left + box[2], rect.top + box[3])))
            digest = hashlib.sha256(caption.tobytes()).hexdigest()
            if digest != self._verified_caption_digest:
                self._verified_caption_digest = None
                text = measure(self, "caption_ocr", lambda: self.reader.read_text(
                    caption.resize((caption.width * 2, caption.height * 2))))
                if not all(fragment in text for fragment in fragments):
                    raise PreparationError("截图标题账户/券商与配置不匹配")
                self._verified_caption_digest = digest

    @read_step("control_lookup")
    def _control(self, control_id: int, class_name: str | None):
        # A fresh lookup is intentional: never retain handles after changing pages.
        criteria = {"class_name": class_name} if class_name else {}
        return self.main.child_window(control_id=control_id, **criteria,
                                      visible_only=True, enabled_only=True).wrapper_object()

    def _has_visible_control(self, control_id: int, class_name: str):
        # WindowSpecification.exists() explicitly ignores visible_only.
        return any(control.is_visible() for control in
                   self.main.descendants(control_id=control_id, class_name=class_name))

    def _read_edit(self, control_id: int):
        control = self._control(control_id, "Edit")
        # This THS build returns an empty WM_GETTEXT even for populated edits.
        # EM_GETLINE exposes the actual single-line order field.
        if control.line_count() != 1:
            raise PreparationError("交易输入框不是单行文本")
        return control.get_line(0).strip()

    def _focus_edit(self, control_id: int):
        self._assert_identity()
        control = self._control(control_id, "Edit")
        control.set_keyboard_focus()
        self._wait(lambda: self._control(control_id, "Edit").has_keyboard_focus(), "输入框未取得键盘焦点")

    def _wait(self, condition, message, *, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else min(timeout, self.timeout))
        while time.monotonic() < deadline:
            self._assert_identity()
            try:
                if condition():
                    return
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        raise PreparationError(message)

    @read_step("page")
    def _page(self, path: list[str]):
        if self.main is None:
            self.connect()
        self._assert_identity()
        tree = self._control(self.profile.get("tree_control_id", 129), "SysTreeView32")
        if not tree.get_item(path).is_selected():
            tree.get_item(path).select()
        self._wait(lambda: tree.get_item(path).is_selected(), "查询页没有选中")

    def read_table(self, role: str):
        self._page(self.profile["pages"][role])
        last_error = None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self._assert_identity()
            try:
                grid = self._control(self.profile.get("grid_control_id", 1047), self.profile.get("grid_class", "CVirtualGridCtrl"))
                picture = measure(self, "table_capture", grid.capture_as_image)
                scale = self.ocr_scales.get(role, 1)
                if scale != 1:
                    picture = picture.resize((picture.width * scale, picture.height * scale))
                result = measure(self, "table_ocr_parse", lambda: self.reader.read(picture, role, utc_now().isoformat()))
                return result
            except Exception as error:
                last_error = error
                time.sleep(0.15)
        raise PreparationError(f"表格未就绪或不能可靠识别: {last_error}")

    def read_quote(self, symbol, instrument):
        """Read the native quote panel; only fill the symbol, never a submit action."""
        from .native_quotes import parse_panel
        validate_symbol(symbol)
        self.connect()
        tree = self._control(self.profile.get("tree_control_id", 129), "SysTreeView32")
        if not any(tree.get_item(self.profile["pages"][side]).is_selected() for side in ("buy", "sell")):
            self._page(self.profile["pages"]["buy"])
        code = self._read_edit(1032)
        if code != symbol[:6]:
            if any(self._read_edit(i) for i in (1033, 1034)):
                raise PreparationError("当前有其他标的或未清空草稿，行情刷新未覆盖；请先核对客户端字段")
            self.clear_draft()
            self._control(1003, "ComboBox").select({"SH": "上海A股", "SZ": "深圳A股", "BJ": "北京A股"}[symbol[-2:]])
            self._set_text(1032, symbol[:6])
            self._focus_edit(1033)  # blur the symbol; no Enter or submission
        self._wait(lambda: instrument.name in self._control(1036, "Static").window_text(), "客户端行情证券名称未匹配")
        last_error = None
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self._assert_identity()
            try:
                # The quote book is windowless in this THS build. Anchor the
                # observed adjacent 250px book strip to the native chart pane,
                # then require OCR labels/geometry; never use screen coordinates.
                # The chart exposes a UIA name, not a Win32 class name.
                from pywinauto import Desktop
                chart = Desktop(backend="uia").window(handle=self.main.handle).child_window(
                    title="EmbChart", control_type="Pane", visible_only=True).wrapper_object()
                outer, rect = self.main.rectangle(), chart.rectangle()
                panel_width = int(self.profile.get("quote_panel_width", 250))
                box = (rect.left - outer.left - panel_width, rect.top - outer.top,
                       rect.left - outer.left - 4, rect.bottom - outer.top)
                if not (0 <= box[0] < box[2] <= outer.width() and 0 <= box[1] < box[3] <= outer.height()):
                    raise PreparationError("客户端报价控件超出窗口范围")
                image = self.main.capture_as_image().crop(box)
                if self.profile.get("console_diagnostics"):
                    destination = Path(self.profile["console_diagnostics"]).parent / "diagnostics" / "native-quote-last.png"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    image.save(destination)
                picture = image.resize((image.width * 3, image.height * 3))
                result = self.reader._recognize(picture)
                quote = parse_panel(result, picture.width, picture.height, symbol, utc_now(), confidence=self.reader.confidence)
                if self._read_edit(1032) != symbol[:6] or instrument.name not in self._control(1036, "Static").window_text():
                    raise PreparationError("读取报价期间证券代码或名称变化")
                self._assert_identity()
                return quote
            except ForegroundChangedError:
                raise
            except Exception as error:
                last_error = error
                time.sleep(.1)
        raise PreparationError(f"客户端报价区未就绪：{last_error}；未切换到Web行情")

    def _set_text(self, control_id: int, value: str):
        self._assert_identity()
        if not value or any(char not in "0123456789." for char in value):
            raise PreparationError("交易字段只接受数字与小数点")
        control = self._control(control_id, "Edit")
        def matches():
            actual = self._read_edit(control_id)
            return Decimal(actual) == Decimal(value) if control_id == 1033 else actual == value
        if self.editor_input_mode != "keys":
            try:
                control.set_edit_text(value)
                self._wait(matches, "控件赋值未被客户端接受", timeout=self.editor_set_timeout)
                return
            except Exception:
                self._assert_identity()
                if self.editor_input_mode == "set_text":
                    raise
        # Numeric order fields only: virtual keys avoid Unicode packet input that
        # some THS builds ignore. Never use Enter to commit the code or order.
        control = self._control(control_id, "Edit")
        self._focus_edit(control_id)
        control.type_keys("{HOME}+{END}{BACKSPACE}", set_foreground=False, vk_packet=False)
        self._wait(lambda: self._read_edit(control_id) == "", "无法完整清空输入框")
        prefix = ""
        for char in value:
            self._assert_identity()
            if not control.has_keyboard_focus():
                raise PreparationError("输入期间键盘焦点改变")
            key = "{VK_DECIMAL}" if char == "." else "{VK_NUMPAD" + char + "}"
            control.type_keys(key, pause=0.03, set_foreground=False, vk_packet=False)
            prefix += char
            self._wait(lambda: self._read_edit(control_id) == prefix, "字符输入未被客户端接受")
        self._wait(matches, "键盘输入后回读仍不匹配")

    @read_operation("account")
    def read_account(self):
        """Read cash from controls and visible positions by OCR; never copy a grid."""
        self.connect()
        positions = self.read_positions()
        self._assert_identity()
        balances = {}
        for field, control_id in {"cash_balance": 1012, "frozen_cash": 1013, "available_cash": 1016,
                                  "stock_value": 1014, "total_assets": 1015}.items():
            balances[field] = str(decimal_value(self._control(control_id, "Static").window_text().strip(), field, positive=False))
        for row in positions.rows:
            if "position_quantity" not in row:
                raise ValidationError("持仓表未取得股票余额，不能把可卖数量当总持仓")
        self._assert_identity()
        return {"account": self.profile["account_alias"], "observed_at": utc_now().isoformat(),
                "source": "win32+ocr", "balances": balances, "positions": positions.rows,
                "positions_complete": positions.complete,
                "positions_scope": "all_scroll_pages" if positions.complete else "visible_rows",
                "positions_incomplete_reason": positions.incomplete_reason,
                "positions_observed_at": positions.observed_at}

    @staticmethod
    def _scroll_info(control):
        import win32gui
        # pywin32 MakeSCROLLINFOTuple returns a reserved zero then five values.
        reserved, minimum, maximum, page, position, _ = win32gui.GetScrollInfo(
            control.handle, getattr(control, "scroll_bar_type", 1), 23)
        if any(type(value) is not int for value in (minimum, maximum, page, position)) or maximum < minimum or page < 0:
            raise ValidationError("滚动条范围信息不完整，不能确认全部记录")
        return minimum, maximum, page, position

    @read_step("scroll_range")
    def _position_scroller(self):
        """Inspect only the grid's actual ancestor chain, never sibling tables."""
        import win32gui
        grid = self._control(self.profile.get("grid_control_id", 1047), self.profile.get("grid_class", "CVirtualGridCtrl"))
        candidates, control = [], grid
        for _ in range(12):
            if control.handle == self.main.handle:
                break
            if win32gui.GetWindowLong(control.handle, -16) & 0x00200000:  # WS_VSCROLL
                candidates.append(control)
            # THS wraps some grids in HexinScrollWnd plus a standard ScrollBar.
            for child in control.children(class_name="ScrollBar"):
                rect = child.rectangle()
                if child.is_visible() and rect.height() > rect.width():
                    scrollbar = TableScrollBar(child)
                    minimum, maximum, page, position = self._scroll_info(scrollbar)
                    if maximum > minimum and page < maximum - minimum + 1:
                        if page <= 0:
                            raise ValidationError("表格独立滚动条未提供有效页大小，目前只能确认可见行")
                        candidates.append(scrollbar)
            control = control.parent()
            if control is None:
                raise ValidationError("表格滚动容器未核对到交易主窗口")
        else:
            raise ValidationError("表格滚动容器超出已核对范围，目前只能确认可见行")
        if len(candidates) > 1:
            raise ValidationError("持仓表存在多个滚动范围，无法确认完整性")
        return candidates[0] if candidates else None

    def read_positions(self):
        """Collect all verified vertical pages without clipboard access or trading."""
        return self.read_all_table("positions")

    def read_all_table(self, role):
        from .tables import TableSnapshot
        if role not in {"positions", "orders"}:
            raise ValidationError("不支持此表格的滚动读取")
        label = "持仓" if role == "positions" else "委托"
        self._page(self.profile["pages"][role])
        try:
            scroller = self._position_scroller()
        except Exception as error:
            # Unknown scroll implementations remain explicitly incomplete.
            snapshot = self.read_table(role)
            snapshot.complete = False
            snapshot.incomplete_reason = str(error)
            return snapshot
        if scroller is None:
            snapshot = self.read_table(role)
            snapshot.complete = True  # verified grid/container chain has no scrollbar
            return snapshot
        self._assert_identity()
        scroller.scroll("up", "end")
        self._wait(lambda: self._scroll_info(scroller)[3] == self._scroll_info(scroller)[0], f"{label}表未滚动到首行")
        rows, seen_positions, extent = {}, set(), None
        for _ in range(100):
            self._assert_identity()
            before = self._scroll_info(scroller)
            if extent is None:
                extent = before[:3]
            if before[:3] != extent or before[3] in seen_positions:
                raise ValidationError(f"{label}滚动范围变化或重复，请重新刷新")
            snapshot = self.read_table(role)
            if self._scroll_info(scroller) != before:
                raise ValidationError(f"读取期间{label}表发生滚动，请重新刷新")
            page_symbols = set()
            for row in snapshot.rows:
                symbol = row["symbol"] if role == "positions" else row["order_id"]
                if symbol in page_symbols:
                    raise ValidationError(f"同一{label}页出现重复记录，无法确认完整性")
                page_symbols.add(symbol)
                previous = rows.get(symbol)
                identity_fields = ("position_quantity", "available_quantity") if role == "positions" else (
                    "symbol", "side", "quantity", "limit_price", "filled_quantity", "status", "average_price")
                if previous and any(previous.get(k) != row.get(k) for k in identity_fields):
                    raise ValidationError(f"分页期间{label}记录变化，请重新刷新")
                rows[symbol] = row
            seen_positions.add(before[3])
            minimum, maximum, page, position = before
            if page <= 0:
                raise ValidationError(f"{label}滚动条未提供有效页大小，不能确认完整性")
            if position >= max(minimum, maximum - page + 1):
                return TableSnapshot(role, list(rows.values()), snapshot.observed_at, snapshot.source, complete=True)
            self._assert_identity()
            # One line at a time keeps the previous viewport overlapping even
            # when THS expresses scrollbar units in pixels rather than rows.
            scroller.scroll("down", "line", retry_interval=0)
            self._wait(lambda: self._scroll_info(scroller)[3] > position, f"{label}表未继续滚动，无法确认完整性")
            if self._scroll_info(scroller)[3] - position > max(1, page - 1):
                raise ValidationError(f"{label}滚动跨度无法核对，不能省略中间记录")
        raise ValidationError(f"{label}分页超过100页，本次未取得完整记录")

    def clear_draft(self):
        if self.main is None:
            return False
        self._assert_identity()
        tree = self._control(self.profile.get("tree_control_id", 129), "SysTreeView32")
        if not any(tree.get_item(self.profile["pages"][side]).is_selected() for side in ("buy", "sell")):
            return False
        if not self._has_visible_control(1032, "Edit"):
            raise PreparationError("交易页未找到可见代码框，无法确认清空")
        reset = self.main.child_window(control_id=1007, class_name="Button", title="重填", visible_only=True)
        # Query filters reuse all three edit IDs and button 1007. Only the
        # selected trade page and exact reset button are valid cleanup targets.
        reset.click()
        self._wait(lambda: all(self._read_edit(i) == "" for i in (1032, 1033, 1034)),
                   "草稿未清空；请人工清空表单，不能继续准备下一单")
        return True

    def _check_resources(self, plan):
        if plan.side == "buy":
            self._page(self.profile["pages"]["positions"])
            self._wait(lambda: self._has_visible_control(1016, "Static"), "资金控件没有加载")
            cash = Decimal(self._control(1016, "Static").window_text())
            if cash < Decimal(plan.notional) + Decimal(plan.fee_reserve):
                raise PreparationError("可用资金不足以覆盖计划与费用预留")
        else:
            snapshot = self.read_table("positions")
            matches = [row for row in snapshot.rows if row["symbol"] == plan.symbol[:6]]
            if len(matches) != 1 or matches[0]["available_quantity"] < plan.quantity:
                raise PreparationError("无法确认足够的可卖持仓")

    def prepare(self, plan: OrderPlan, instrument: Instrument) -> dict:
        if self.main is None:
            self.connect()
        if plan.account != self.profile["account_alias"]:
            raise PreparationError("信号账户别名不匹配")
        self._check_resources(plan)
        side_path = self.profile["pages"][plan.side]
        try:
            self._page(side_path)
            self._wait(lambda: self._has_visible_control(1032, "Edit"), "交易输入框未就绪")
            self.clear_draft()
            market = {"SH": "上海A股", "SZ": "深圳A股", "BJ": "北京A股"}[plan.symbol[-2:]]
            self._control(1003, "ComboBox").select(market)
            self._assert_identity()
            self._set_text(1032, plan.symbol[:6])
            # Moving focus emits the code edit change/blur notification; no Enter.
            self._focus_edit(1033)
            self._wait(lambda: instrument.name in self._control(1036, "Static").window_text(), "证券名称未匹配，可能报价未刷新")
            self._set_text(1033, plan.limit_price)
            self._set_text(1034, str(plan.quantity))
            observed = {"code": self._read_edit(1032),
                        "price": self._read_edit(1033),
                        "quantity": self._read_edit(1034),
                        "name": self._control(1036, "Static").window_text().strip()}
            if observed["code"] != plan.symbol[:6] or Decimal(observed["price"]) != Decimal(plan.limit_price) or observed["quantity"] != str(plan.quantity):
                raise PreparationError("交易字段回读与计划不一致")
            self._assert_identity()
            # No reference to the buy/sell submit button (1006) is used here.
            return {"verified": observed, "submitted": False, "manual_submit_required": True}
        except Exception as preparation_error:
            try:
                if self.clear_draft() is False:
                    raise PreparationError("不在交易草稿页，不能确认失败后的清理")
            except Exception as cleanup_error:
                raise DraftCleanupError(f"准备失败且无法确认草稿清空: {cleanup_error}") from preparation_error
            raise
