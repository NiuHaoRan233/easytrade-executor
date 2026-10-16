"""Window contract tests use in-memory controls; they never connect to Windows."""
import unittest
import re
from unittest.mock import patch
from decimal import Decimal
from types import SimpleNamespace

from easytrade.domain import Instrument, Signal, build_plan, ValidationError
from easytrade.tables import TableSnapshot
from easytrade.ths import ThsDraftAdapter
from test_executor import signal


class Control:
    def __init__(self, value="", setter_works=True, changed=None):
        self.value, self.setter_works, self.changed = value, setter_works, changed
        self.key_log = []

    def wrapper_object(self):
        return self

    def exists(self, **kwargs):
        return True

    def is_visible(self):
        return True

    def window_text(self):
        return self.value

    def set_focus(self):
        pass

    def set_keyboard_focus(self):
        pass

    def has_keyboard_focus(self):
        return True

    def line_count(self):
        return 1

    def get_line(self, index):
        return self.value

    def set_edit_text(self, value):
        if self.setter_works:
            self.value = value
            if self.changed:
                self.changed(value)

    def type_keys(self, value, **kwargs):
        self.key_log.append(value)
        if value == "{HOME}+{END}{BACKSPACE}":
            self.value = ""
        elif value.startswith("{VK_"):
            for key in re.findall(r"\{VK_(NUMPAD[0-9]|DECIMAL)\}", value):
                self.value += "." if key == "DECIMAL" else key[-1]
        else:
            raise AssertionError("unexpected key or trade shortcut")

    def select(self, value):
        self.value = value


class TreeItem:
    def __init__(self, main, path):
        self.main, self.path = main, path

    def select(self):
        self.main.page = self.path

    def is_selected(self):
        return self.main.page == self.path


class Tree(Control):
    def __init__(self, main):
        self.main = main

    def get_item(self, path):
        return TreeItem(self.main, path)


class Reset(Control):
    def __init__(self, main):
        self.main = main

    def window_text(self):
        return "重填"

    def click(self):
        for field in (1032, 1033, 1034):
            self.main.controls[field].value = ""


class Window:
    def __init__(self):
        self.page = []
        self.controls = {i: Control() for i in (1032, 1033, 1034, 1036, 1016, 1003)}
        self.controls[1016].value = "10000"
        self.controls[1032].changed = lambda value: setattr(self.controls[1036], "value", "盈峰环境" if value else "")
        self.controls[129] = Tree(self)
        self.controls[1007] = Reset(self)
        for key, control in self.controls.items():
            control.control_id = lambda key=key: key

    def window_text(self):
        return "网上股票交易系统5.0 - 国泰君安证券(TEST)"

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def has_focus(self):
        return True

    def restore(self):
        pass

    def set_focus(self):
        pass

    def child_window(self, control_id, class_name, **kwargs):
        if control_id == 1006:
            raise AssertionError("draft adapter must never access submit button")
        return self.controls[control_id]

    def descendants(self, control_id, class_name):
        return [self.controls[control_id]] if control_id in self.controls else []


class ThsContractTests(unittest.TestCase):
    def test_attach_binds_unique_window_and_checks_exact_executable(self):
        from unittest.mock import Mock
        adapter = self.make_adapter()
        adapter.profile.update(exe_path="C:/broker/xiadan.exe", title_regex="broker")
        adapter._main_window_handles = Mock(return_value=[12])
        app = Mock(process=42)
        app.connect.return_value = app
        with patch("pathlib.Path.is_file", return_value=True), patch("win32process.GetWindowThreadProcessId", return_value=(1,42)), patch("pywinauto.Application", return_value=app), patch("pywinauto.application.process_module", return_value="C:/broker/xiadan.exe") as executable:
            adapter._attach()
        executable.assert_called_once_with(42)
        app.connect.assert_called_once_with(handle=12)
        app.window.assert_called_once_with(handle=12)

    def test_attach_rejects_wrong_executable_ambiguous_window_and_changed_pid(self):
        from unittest.mock import Mock
        for scenario in ("wrong_exe", "ambiguous", "missing", "changed_pid", "unreadable_exe"):
            with self.subTest(scenario=scenario):
                adapter = self.make_adapter()
                adapter.profile.update(exe_path="C:/broker/xiadan.exe", title_regex="broker")
                app = Mock(process=42)
                app.connect.return_value = app
                adapter._main_window_handles = Mock(return_value=[12,13] if scenario == "ambiguous" else ([] if scenario == "missing" else [12]))
                with patch("pathlib.Path.is_file", return_value=True), patch("win32process.GetWindowThreadProcessId", side_effect=[(1,42),(1,43)] if scenario == "changed_pid" else None, return_value=(1,42)), patch("pywinauto.Application", return_value=app), patch("pywinauto.application.process_module", return_value="C:/other/xiadan.exe" if scenario == "wrong_exe" else "C:/broker/xiadan.exe", side_effect=OSError("unreadable") if scenario == "unreadable_exe" else None):
                    with self.assertRaises((RuntimeError, OSError)):
                        adapter._attach()
                app.window.assert_not_called()
                if scenario != "changed_pid":
                    app.connect.assert_not_called()

    def test_main_window_search_checks_all_visible_matching_titles(self):
        from unittest.mock import Mock
        adapter = self.make_adapter()
        adapter.profile["title_regex"] = "^broker[0-9]$"
        gui = Mock()
        gui.EnumWindows.side_effect = lambda callback, context: [callback(handle, context) for handle in (1,2,3,4)]
        gui.IsWindowVisible.side_effect = lambda handle: handle != 2
        gui.GetWindowText.side_effect = lambda handle: {1:"broker1",3:"other",4:"broker4"}[handle]
        with patch.dict("sys.modules", {"win32gui": gui}):
            self.assertEqual(adapter._main_window_handles(), [1,4])
        self.assertEqual([call.args[0] for call in gui.GetWindowText.call_args_list], [1,3,4])

    def make_adapter(self):
        profile = {"account_alias": "guotai", "title_contains": ["国泰君安证券", "TEST"], "timeout_seconds": 0.01,
                   "pages": {"positions": ["查询[F4]", "资金股票"], "buy": ["买入[F1]"], "sell": ["卖出[F2]"]}}
        adapter = ThsDraftAdapter(profile)
        adapter.main = Window()
        adapter.main.page = profile["pages"]["buy"]
        adapter._has_visible_control = lambda control_id, class_name: any(
            c.is_visible() for c in adapter.main.descendants(control_id=control_id, class_name=class_name))
        return adapter

    def test_connect_does_not_restore_a_user_sized_or_maximized_window(self):
        adapter = self.make_adapter()
        adapter._attach = lambda: None
        with patch.object(adapter.main, "restore") as restore, patch.object(adapter.main, "set_focus") as focus:
            adapter.connect()
        restore.assert_not_called()
        focus.assert_called_once()

    def test_read_table_discards_a_capture_if_the_window_resizes_during_ocr(self):
        from PIL import Image
        from unittest.mock import Mock
        adapter = self.make_adapter()
        adapter.timeout = 1
        adapter._page = lambda path: None
        adapter._assert_identity = lambda: None
        grid = SimpleNamespace(capture_as_image=lambda: Image.new("RGB", (300, 100)))
        adapter._control = lambda *args: grid
        before, after = {"window_size": [800, 600]}, {"window_size": [1000, 700]}
        adapter._table_geometry = Mock(side_effect=[before, after, after, after])
        first, second = (TableSnapshot("positions", [], "now", "ocr") for _ in range(2))
        adapter.reader.read = Mock(side_effect=[first, second])
        result = adapter.read_table("positions")
        self.assertIs(result, second)
        self.assertEqual(result.layout["window_size"], [1000, 700])
        self.assertEqual(adapter.reader.read.call_count, 2)

    def test_pagination_keeps_price_warnings_from_all_pages(self):
        adapter, state, rows = self.position_reader()
        adapter.read_table = lambda role: TableSnapshot(role, rows[state["position"]:state["position"] + 2], "now", "ocr",
            warnings=[f"page-{state['position']} missing price"], layout={"window_size": [1000, 700]})
        result = adapter.read_positions()
        self.assertEqual(len(result.warnings), 3)
        self.assertTrue(result.complete)

    def test_pagination_rejects_a_changed_window_layout(self):
        adapter, state, rows = self.position_reader()
        adapter.read_table = lambda role: TableSnapshot(role, rows[state["position"]:state["position"] + 2], "now", "ocr",
            layout={"window_size": [1000 + state["position"], 700]})
        with self.assertRaisesRegex(ValidationError, "窗口布局变化"):
            adapter.read_positions()

    def test_buy_and_sell_prepare_without_submit(self):
        instrument = Instrument("盈峰环境", 100, Decimal("0.01"))
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                adapter = self.make_adapter()
                adapter.read_table = lambda role: TableSnapshot(role, [{"symbol": "000967", "available_quantity": 1400}], "", "ocr")
                plan = build_plan(Signal.parse(signal(side=side)), instrument, Decimal("5") if side == "buy" else Decimal("0"))
                result = adapter.prepare(plan, instrument)
                self.assertFalse(result["submitted"])
                self.assertEqual(result["verified"]["quantity"], "100")
                self.assertEqual(result["verified"]["price"], "7.42")

    def test_user_adapter_prepares_requested_fields_without_ocr_resource_gate(self):
        from easytrade.user_native import ThsUserAdapter
        instrument = Instrument("盈峰环境", 100, Decimal("0.01"))
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                original = self.make_adapter()
                adapter = ThsUserAdapter(original.profile)
                adapter.main = original.main
                adapter._has_visible_control = original._has_visible_control
                adapter.main.controls[1016].value = "0"
                adapter.read_table = lambda role: self.fail("submission must not depend on OCR positions")
                plan = build_plan(Signal.parse(signal(side=side)), instrument, Decimal("0"))
                self.assertEqual(adapter.prepare(plan, instrument)["verified"]["quantity"], "100")

    def test_quote_lookup_preserves_matching_draft_and_never_accesses_submit(self):
        from PIL import Image
        adapter = self.make_adapter()
        adapter.connect = lambda: None
        adapter._assert_identity = lambda: None
        adapter.main.controls[1032].value = "159570"
        adapter.main.controls[1033].value = "1.364"
        adapter.main.controls[1034].value = "200"
        adapter.main.controls[1036].value = "港股通创新药ETF"
        rect = SimpleNamespace(left=0, top=0, width=lambda: 1000, height=lambda: 500)
        adapter.main.rectangle = lambda: rect
        adapter.main.capture_as_image = lambda: Image.new("RGB", (1000, 500), "white")
        chart = SimpleNamespace(wrapper_object=lambda: SimpleNamespace(handle=20))
        adapter.main.handle = 10
        desktop = SimpleNamespace(window=lambda **kwargs: SimpleNamespace(child_window=lambda **kwargs: chart))
        adapter.reader._recognize = lambda image: None
        quote = object()
        gui = SimpleNamespace(IsWindow=lambda h: True, IsWindowVisible=lambda h: True, IsChild=lambda p,h: True, GetWindowRect=lambda h: (680,88,900,320))
        structures = SimpleNamespace(RECT=lambda *args: SimpleNamespace(left=680,top=88,right=900,bottom=320))
        with patch.dict("sys.modules", {"pywinauto": SimpleNamespace(Desktop=lambda **kwargs: desktop), "win32gui": gui, "pywinauto.win32structures": structures}), patch("easytrade.screen_capture.capture_region", return_value=Image.new("RGB", (250,232))), patch("easytrade.native_quotes.parse_panel", return_value=quote):
            self.assertIs(adapter.read_quote("159570.SZ", Instrument("港股通创新药ETF", 100, Decimal(".001"))), quote)
        self.assertEqual([adapter.main.controls[i].value for i in (1032,1033,1034)], ["159570", "1.364", "200"])
        adapter.main.controls[1032].value = "000967"
        with self.assertRaisesRegex(Exception, "未覆盖"):
            adapter.read_quote("159570.SZ", Instrument("港股通创新药ETF", 100, Decimal(".001")))

    def test_quote_symbol_change_selects_market_only_when_no_price_quantity_draft(self):
        from PIL import Image
        adapter = self.make_adapter()
        adapter.connect = lambda: None
        adapter._assert_identity = lambda: None
        adapter.main.controls[1032].value = "000001"
        adapter.main.controls[1032].changed = lambda value: setattr(adapter.main.controls[1036], "value", "沪深300")
        adapter.main.handle = 10
        adapter.main.rectangle = lambda: SimpleNamespace(left=0, top=0, width=lambda: 1000, height=lambda: 500)
        adapter.main.capture_as_image = lambda: Image.new("RGB", (1000, 500), "white")
        chart = SimpleNamespace(wrapper_object=lambda: SimpleNamespace(handle=20))
        desktop = SimpleNamespace(window=lambda **kwargs: SimpleNamespace(child_window=lambda **kwargs: chart))
        adapter.reader._recognize = lambda image: None
        gui = SimpleNamespace(IsWindow=lambda h: True, IsWindowVisible=lambda h: True, IsChild=lambda p,h: True, GetWindowRect=lambda h: (680,88,900,320))
        structures = SimpleNamespace(RECT=lambda *args: SimpleNamespace(left=680,top=88,right=900,bottom=320))
        with patch.dict("sys.modules", {"pywinauto": SimpleNamespace(Desktop=lambda **kwargs: desktop), "win32gui": gui, "pywinauto.win32structures": structures}), patch("easytrade.screen_capture.capture_region", return_value=Image.new("RGB", (250,232))), patch("easytrade.native_quotes.parse_panel", return_value="quote"):
            self.assertEqual(adapter.read_quote("510300.SH", Instrument("沪深300", 100, Decimal(".001"))), "quote")
        self.assertEqual(adapter.main.controls[1003].value, "上海A股")
        self.assertEqual(adapter.main.controls[1032].value, "510300")
        self.assertEqual([adapter.main.controls[i].value for i in (1033,1034)], ["", ""])

    def test_visible_control_lookup_rejects_unrelated_account_combo_and_hidden_market(self):
        adapter = ThsDraftAdapter({})
        adapter.main = SimpleNamespace(handle=10)
        gui = SimpleNamespace(EnumChildWindows=lambda h, callback, context: [callback(i, context) for i in (1,2)],
            GetDlgCtrlID=lambda h: {1:1003, 2:2322}[h], GetClassName=lambda h: "ComboBox", IsWindowVisible=lambda h: h==2)
        with patch.dict("sys.modules", {"win32gui": gui}):
            self.assertFalse(adapter._has_visible_control(1003, "ComboBox"))
            self.assertTrue(adapter._has_visible_control(2322, "ComboBox"))

    def test_modern_form_resolves_an_unconfigured_code_without_market_or_reset_buttons(self):
        from easytrade.user_native import ThsUserAdapter
        original = self.make_adapter()
        adapter = ThsUserAdapter(original.profile)
        adapter.main = original.main
        adapter._has_visible_control = original._has_visible_control
        del adapter.main.controls[1003]
        del adapter.main.controls[1007]
        adapter.main.controls[3001] = Control("证券代码")
        generic = Instrument("", 1, Decimal("0.000000000001"))
        raw = signal()
        raw.update(symbol="000967", quantity=101, limit_price="6.931")
        plan = build_plan(Signal.parse(raw), generic, Decimal("0"))
        result = adapter.prepare(plan, generic)
        self.assertEqual(result["verified"], {"code":"000967", "price":"6.931", "quantity":"101", "name":"盈峰环境"})
        self.assertFalse(result["submitted"])
        self.assertTrue(adapter.clear_draft())
        self.assertEqual([adapter._read_edit(i) for i in (1032,1033,1034)], ["", "", ""])

    def position_reader(self):
        adapter = self.make_adapter()
        adapter._assert_identity = lambda: None
        adapter._page = lambda path: None
        state = {"position": 2, "blocked": False}
        def scroll(direction, amount, **kwargs):
            if direction == "up":
                state["position"] = 0
            elif not state["blocked"]:
                state["position"] = min(2, state["position"] + 1)
        scroller = SimpleNamespace(scroll=scroll)
        adapter._position_scroller = lambda: scroller
        adapter._scroll_info = lambda control: (0, 3, 2, state["position"])
        rows = [{"symbol": f"{i:06}", "position_quantity": i * 100, "available_quantity": (i - 1) * 100}
                for i in range(1, 5)]
        adapter.read_table = lambda role: TableSnapshot(role, rows[state["position"]:state["position"] + 2], "now", "ocr")
        return adapter, state, rows

    def test_positions_collect_all_overlapping_scroll_pages(self):
        adapter, state, rows = self.position_reader()
        snapshot = adapter.read_positions()
        self.assertEqual(snapshot.rows, rows)
        self.assertTrue(snapshot.complete)
        self.assertEqual(state["position"], 2)

    def test_orders_pagination_keys_by_order_id_not_symbol(self):
        adapter, state, _ = self.position_reader()
        adapter.profile["pages"]["orders"] = ["orders"]
        rows = [{"symbol": "159570", "order_id": str(i), "side": "buy", "quantity": 100,
                 "limit_price": "1.4", "filled_quantity": 100, "average_price": "1.4", "status": "FILLED"}
                for i in range(4)]
        adapter.read_table = lambda role: TableSnapshot(role, rows[state["position"]:state["position"]+2], "now", "ocr")
        snapshot = adapter.read_all_table("orders")
        self.assertEqual(snapshot.rows, rows)
        self.assertTrue(snapshot.complete)

    def test_orders_changed_during_pagination_rejected(self):
        adapter, state, _ = self.position_reader()
        adapter.profile["pages"]["orders"] = ["orders"]
        rows = [{"symbol": "159570", "order_id": str(i), "filled_quantity": 0, "status": "BROKER_ACCEPTED"}
                for i in range(4)]
        def read(role):
            page = [dict(row) for row in rows[state["position"]:state["position"]+2]]
            if state["position"]:
                page[0]["filled_quantity"] = 100
            return TableSnapshot(role, page, "now", "ocr")
        adapter.read_table = read
        with self.assertRaisesRegex(Exception, "分页期间"):
            adapter.read_all_table("orders")

    def test_positions_scroll_failure_is_not_reported_as_complete(self):
        adapter, state, rows = self.position_reader()
        state["blocked"] = True
        with self.assertRaisesRegex(Exception, "未继续滚动"):
            adapter.read_positions()

    def test_positions_reject_duplicate_rows_and_changes_during_scan(self):
        for scenario in ("duplicate", "scroll-during-read"):
            with self.subTest(scenario=scenario):
                adapter, state, rows = self.position_reader()
                def read(role):
                    if scenario == "scroll-during-read":
                        state["position"] += 1
                    return TableSnapshot(role, [rows[0], rows[0]], "now", "ocr")
                adapter.read_table = read
                with self.assertRaises(Exception):
                    adapter.read_positions()

    def test_unrecognized_scroll_container_preserves_partial_scope(self):
        adapter, state, rows = self.position_reader()
        def unknown():
            raise ValueError("unknown scrollbar")
        adapter._position_scroller = unknown
        result = adapter.read_positions()
        self.assertFalse(result.complete)
        self.assertEqual(result.incomplete_reason, "unknown scrollbar")

    def test_no_scrollbar_is_complete_only_for_verified_container_chain(self):
        adapter = self.make_adapter()
        main = SimpleNamespace(handle=10)
        grid = SimpleNamespace(handle=20, class_name=lambda: "CVirtualGridCtrl", parent=lambda: main, children=lambda **kwargs: [])
        adapter.main = main
        adapter._control = lambda *args: grid
        with patch.dict("sys.modules", {"win32gui": SimpleNamespace(GetWindowLong=lambda *args: 0)}):
            self.assertIsNone(adapter._position_scroller())
            intermediate = SimpleNamespace(handle=30, parent=lambda: main, children=lambda **kwargs: [])
            grid.parent = lambda: intermediate
            self.assertIsNone(adapter._position_scroller())
            intermediate.parent = lambda: None
            with self.assertRaisesRegex(Exception, "滚动容器未核对"):
                adapter._position_scroller()

    def test_separate_scrollbar_uses_control_range_and_parent_notifications(self):
        from easytrade.ths import TableScrollBar
        adapter = self.make_adapter()
        messages = []
        main = SimpleNamespace(handle=10)
        parent = SimpleNamespace(handle=30, parent=lambda: main, send_message=lambda *args: messages.append(args))
        bar = SimpleNamespace(handle=40, parent=lambda: parent, is_visible=lambda: True,
                              rectangle=lambda: SimpleNamespace(height=lambda: 100, width=lambda: 15))
        parent.children = lambda **kwargs: [bar]
        grid = SimpleNamespace(handle=20, parent=lambda: parent, children=lambda **kwargs: [])
        adapter.main = main
        adapter._control = lambda *args: grid
        win32 = SimpleNamespace(GetWindowLong=lambda *args: 0, GetScrollInfo=lambda *args: (0, 0, 9, 3, 0, 0))
        with patch.dict("sys.modules", {"win32gui": win32}):
            scrollbar = adapter._position_scroller()
            self.assertIsInstance(scrollbar, TableScrollBar)
            self.assertEqual(adapter._scroll_info(scrollbar), (0, 9, 3, 0))
            scrollbar.scroll("up", "end")
            scrollbar.scroll("down", "line")
            self.assertEqual(messages, [(0x115, 6, 40), (0x115, 1, 40)])
            win32.GetScrollInfo = lambda *args: (0, 0, 2, 3, 0, 0)
            self.assertIsNone(adapter._position_scroller())
            win32.GetScrollInfo = lambda *args: (0, 0, 100, 0, 0, 0)
            with self.assertRaisesRegex(Exception, "有效页大小"):
                adapter._position_scroller()
            win32.GetScrollInfo = lambda *args: (0, None, None, 3, 0, 0)
            with self.assertRaisesRegex(Exception, "范围信息不完整"):
                adapter._position_scroller()

    def test_navigation_descendant_scrollbar_is_never_used_for_the_grid(self):
        # Real pywinauto children(class_name=...) includes nested descendants.
        # Reproduce the outer MDI frame returning both the grid and menu bars.
        from easytrade.ths import TableScrollBar
        adapter = self.make_adapter()
        main = SimpleNamespace(handle=10)
        frame = SimpleNamespace(handle=50, parent=lambda: main)
        menu = SimpleNamespace(handle=60, parent=lambda: frame)
        parent = SimpleNamespace(handle=30, parent=lambda: frame)
        def bar(handle, owner):
            return SimpleNamespace(handle=handle, parent=lambda: owner, is_visible=lambda: True,
                                   rectangle=lambda: SimpleNamespace(height=lambda: 100, width=lambda: 15))
        menu_bar, grid_bar = bar(70, menu), bar(40, parent)
        grid = SimpleNamespace(handle=20, parent=lambda: parent, children=lambda **kwargs: [])
        parent.children = lambda **kwargs: [grid_bar]
        frame.children = lambda **kwargs: [grid_bar, menu_bar]
        adapter.main, adapter._control = main, lambda *args: grid
        win32 = SimpleNamespace(GetWindowLong=lambda *args: 0, GetScrollInfo=lambda *args: (0, 0, 9, 3, 0, 0))
        with patch.dict("sys.modules", {"win32gui": win32}):
            self.assertEqual(adapter._position_scroller().handle, grid_bar.handle)
            parent.children = lambda **kwargs: []
            frame.children = lambda **kwargs: [menu_bar]
            self.assertIsNone(adapter._position_scroller())

    def test_empty_trade_form_does_not_require_a_reset_button(self):
        adapter = self.make_adapter()
        with patch.object(adapter.main, "child_window", wraps=adapter.main.child_window) as lookup:
            self.assertTrue(adapter.clear_draft())
        self.assertFalse(any(call.kwargs.get("control_id") == 1007 for call in lookup.call_args_list))

    def test_ignored_setter_uses_full_clear_and_virtual_keys(self):
        adapter = self.make_adapter()
        control = Control("7.931", setter_works=False)
        adapter.main.controls[1033] = control
        adapter._set_text(1033, "7.42")
        self.assertEqual(control.value, "7.42")
        self.assertEqual(control.key_log[0], "{HOME}+{END}{BACKSPACE}")
        self.assertTrue(any("{VK_DECIMAL}" in key for key in control.key_log))
        self.assertFalse(any("ENTER" in key or "RETURN" in key for key in control.key_log))

    def test_reset_clears_all_fields_before_next_draft(self):
        adapter = self.make_adapter()
        for i in (1032, 1033, 1034):
            adapter.main.controls[i].value = "100"
        adapter.clear_draft()
        self.assertTrue(all(adapter.main.controls[i].value == "" for i in (1032, 1033, 1034)))

    def test_explicit_keys_mode_skips_setter_and_checks_final_value(self):
        adapter = self.make_adapter()
        adapter.editor_input_mode = "keys"
        control = Control("7.931")
        adapter.main.controls[1033] = control
        with patch.object(control, "set_edit_text", side_effect=AssertionError("setter must be skipped")):
            adapter._set_text(1033, "7.42")
        self.assertEqual(control.value, "7.42")
        self.assertTrue(control.key_log)

    def test_auto_mode_bounds_failed_setter_wait(self):
        adapter = self.make_adapter()
        adapter.timeout = 8
        control = Control("7.931", setter_works=False)
        adapter.main.controls[1033] = control
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch("easytrade.ths.time.monotonic", side_effect=lambda: clock[0]), patch("easytrade.ths.time.sleep", side_effect=sleep):
            adapter._set_text(1033, "7.42")
        self.assertAlmostEqual(clock[0], 0.3)
        self.assertEqual(control.value, "7.42")

    def test_set_text_mode_rejects_ignored_setter_without_keys(self):
        adapter = self.make_adapter()
        adapter.editor_input_mode = "set_text"
        control = Control("7.931", setter_works=False)
        adapter.main.controls[1033] = control
        with self.assertRaises(RuntimeError):
            adapter._set_text(1033, "7.42")
        self.assertEqual(control.key_log, [])

    def test_keys_mode_stops_when_old_price_cannot_be_cleared(self):
        adapter = self.make_adapter()
        adapter.editor_input_mode = "keys"
        control = Control("7.931")
        adapter.main.controls[1033] = control
        with patch.object(control, "type_keys") as keys, self.assertRaises(RuntimeError):
            adapter._set_text(1033, "7.42")
        keys.assert_called_once()
        self.assertEqual(control.value, "7.931")

    def test_keys_mode_rejects_corrupted_price_after_typing(self):
        adapter = self.make_adapter()
        adapter.editor_input_mode = "keys"
        control = Control("7.931")
        adapter.main.controls[1033] = control
        original = control.type_keys
        def corrupted(value, **kwargs):
            original(value, **kwargs)
            if "{VK_NUMPAD2}" in value:
                control.value = "7.43"
        with patch.object(control, "type_keys", side_effect=corrupted), self.assertRaises(RuntimeError):
            adapter._set_text(1033, "7.42")

    def test_invalid_editor_configuration_is_rejected(self):
        for config in ({"editor_input_mode": "unknown"}, {"editor_set_timeout_seconds": 0},
                       {"editor_set_timeout_seconds": float("nan")}, {"editor_set_timeout_seconds": float("inf")}):
            with self.subTest(config=config), self.assertRaises(RuntimeError):
                ThsDraftAdapter(config)

    def test_wrong_account_identity_prevents_field_writes(self):
        adapter = self.make_adapter()
        adapter.profile["title_contains"] = ["另一券商"]
        with self.assertRaises(RuntimeError):
            adapter._set_text(1032, "000967")
        self.assertEqual(adapter.main.controls[1032].value, "")

    def test_background_window_prevents_field_writes(self):
        adapter = self.make_adapter()
        adapter.main.has_focus = lambda: False
        with self.assertRaises(RuntimeError):
            adapter._set_text(1033, "7.42")
        self.assertEqual(adapter.main.controls[1033].value, "")

    def test_edit_readback_works_when_wm_gettext_is_empty(self):
        adapter = self.make_adapter()
        control = adapter.main.controls[1033]
        control.window_text = lambda: ""
        adapter._set_text(1033, "7.42")
        self.assertEqual(adapter._read_edit(1033), "7.42")

    def test_hidden_edit_does_not_trigger_cleanup_on_query_page(self):
        adapter = self.make_adapter()
        adapter.main.page = adapter.profile["pages"]["positions"]
        for field in (1032, 1033, 1034):
            adapter.main.controls[field].value = "100"
            adapter.main.controls[field].is_visible = lambda: False
        adapter.clear_draft()
        self.assertEqual(adapter.main.controls[1032].value, "100")

    def test_query_filters_with_trade_ids_are_never_reset(self):
        adapter = self.make_adapter()
        adapter.main.page = adapter.profile["pages"]["positions"]
        for field in (1032, 1033, 1034):
            adapter.main.controls[field].value = "query-filter"
        with patch.object(adapter.main.controls[1007], "click", side_effect=AssertionError("query button is not draft reset")):
            self.assertFalse(adapter.clear_draft())
        self.assertEqual(adapter.main.controls[1033].value, "query-filter")

    def test_dialog_inspection_does_not_activate_or_confirm(self):
        from unittest.mock import Mock
        adapter = self.make_adapter()
        adapter.app = SimpleNamespace(process=42)
        adapter.main.handle = 1
        body = SimpleNamespace(is_visible=lambda: True, window_text=lambda: "是否确认买入159570，100份？")
        yes = SimpleNamespace(is_visible=lambda: True, window_text=lambda: "是(Y)", control_id=lambda: 6,
                              click=Mock(side_effect=AssertionError("must not confirm")))
        dialog = SimpleNamespace(handle=2, class_name=lambda: "#32770", window_text=lambda: "委托确认",
                                 rectangle=lambda: SimpleNamespace(width=lambda: 300, height=lambda: 200),
                                 descendants=lambda class_name: [body] if class_name == "Static" else [yes],
                                 set_focus=Mock(side_effect=AssertionError("must not activate")))
        desktop = Mock()
        desktop.windows.return_value = [adapter.main, dialog]
        with patch("pywinauto.Desktop", return_value=desktop), patch("win32gui.GetWindow", return_value=1):
            result = adapter.observe_dialogs()
        self.assertEqual(result["dialogs"][0]["kind"], "ORDER_CONFIRMATION")
        self.assertFalse(result["account_verified"])
        self.assertFalse(result["submitted"])
        yes.click.assert_not_called()
        dialog.set_focus.assert_not_called()

    def test_caption_cache_rechecks_changed_account_pixels(self):
        from PIL import Image
        adapter = self.make_adapter()
        adapter.profile["caption_identity_box"] = [0, 0, 650, 26]
        adapter.main.window_text = lambda: "网上股票交易系统5.0"
        adapter.main.rectangle = lambda: SimpleNamespace(left=0, top=0, width=lambda: 1000, height=lambda: 600)
        image = Image.new("RGB", (650, 26), "white")
        with patch("easytrade.screen_capture.capture_region", return_value=image), patch.object(adapter.reader, "read_text", return_value="国泰君安证券(TEST)") as read:
            adapter._assert_identity()
            adapter._assert_identity()
            read.assert_called_once()
        with patch("easytrade.screen_capture.capture_region", return_value=Image.new("RGB", (650, 26), "gray")), patch.object(adapter.reader, "read_text", return_value="另一券商(TEST)"):
            with self.assertRaises(RuntimeError):
                adapter._set_text(1033, "7.42")
        self.assertIsNone(adapter._verified_caption_digest)
        self.assertEqual(adapter.main.controls[1033].value, "")

    def test_foreground_child_must_belong_to_exact_main_window(self):
        adapter = self.make_adapter()
        adapter.main.has_focus = lambda: False
        adapter.main.handle = 123
        with patch("win32gui.GetForegroundWindow", return_value=456), patch("win32gui.GetAncestor", return_value=123):
            self.assertTrue(adapter._is_foreground())
        with patch("win32gui.GetForegroundWindow", return_value=456), patch("win32gui.GetAncestor", return_value=789):
            self.assertFalse(adapter._is_foreground())


if __name__ == "__main__":
    unittest.main()
