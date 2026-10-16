"""Double-click desktop console. Startup and simulation never connect to THS."""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from pathlib import Path
from uuid import uuid4

from .user_session import UserSession
from .domain import TERMINAL, validate_symbol
from .diagnostics import CONSOLE_BUILD, record_action
from .quotes import QuoteUnavailable
from .configuration import load_config

ROOT = Path.cwd()
STATUSES = {"FILLED": "全部成交", "BROKER_ACCEPTED": "已受理/待成交", "PARTIALLY_FILLED": "部分成交",
            "UNKNOWN": "状态未知，禁止重发", "CANCELLED": "已撤", "REJECTED": "已拒绝",
            "READY_FOR_MANUAL_SUBMIT": "旧草稿，须核对", "PREPARATION_FAILED": "准备失败/已作废"}
ACTION_LABELS = {"list": "读取本地记录", "quote": "刷新盘口", "buy": "买入", "sell": "卖出",
                 "account": "刷新账户/持仓", "refresh": "刷新委托状态", "check": "客户端适配检查",
                 "query": "查询回执", "resume": "接续原回执", "cancel": "撤单", "discard": "核对未提交",
                 "resolve_unsubmitted": "核对未提交"}


class Console:
    def __init__(self, root, project_root=ROOT, *, live=False):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.project_root = root, Path(project_root)
        self.messages, self.tasks = queue.Queue(), queue.Queue()
        self.busy, self.closing, self.stop = False, False, threading.Event()
        record_action(self.project_root, "console_started")
        self.mode = tk.StringVar(value="实盘（测试中）" if live else "模拟（不连接客户端）")
        self.price_mode = tk.StringVar(value="手填限价")
        self.manual_price, self.broker_id = tk.StringVar(value=""), tk.StringVar(value="")
        self.quantity = tk.StringVar(value="")
        initial_config = load_config(self.project_root, live=live)
        self.catalog = {}
        self.holding_choices = {}
        self.symbol = tk.StringVar(value="")
        self.instrument_text = tk.StringVar()
        self.quote_source = tk.StringVar(value="同花顺客户端（默认）")
        self.banner = tk.StringVar()
        self.pending_hint = tk.StringVar(value="")
        self.result_text = tk.StringVar(value="就绪 · 操作结果会显示在这里，详细过程见下方运行日志。")
        self.quote_text = tk.StringVar(value="输入代码后可刷新客户端盘口；明确限价可直接执行。")
        self.account_text = tk.StringVar(value="账户与持仓尚未读取")
        self.broker_text = tk.StringVar(value="券商当日委托尚未读取")
        self.check_text = tk.StringVar(value="首次使用：先固定客户端窗口及列宽，切换实盘后点击“客户端适配检查”。\n只读检查，不发送买卖或撤单；完成后仍需人工逐项对照。")
        root.title("Easytrade 通用交易测试台")
        width = min(1140, root.winfo_screenwidth() - 60)
        height = min(900, root.winfo_screenheight() - 90)
        root.geometry(f"{width}x{height}")
        root.minsize(min(1000, width), min(740, height))
        frame = ttk.Frame(root, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Easytrade · 模型指令买卖 / 资金持仓 / 委托查询", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"调试版本 {CONSOLE_BUILD}", foreground="#666666").pack(anchor="w")
        self.banner_label = tk.Label(frame, textvariable=self.banner, anchor="w", justify="left", padx=10, pady=7,
                                     font=("Microsoft YaHei UI", 11))
        self.banner_label.pack(fill="x", pady=(8, 6))
        instrument_row = ttk.Frame(frame)
        instrument_row.pack(fill="x", pady=(0, 6))
        ttk.Label(instrument_row, text="证券：").pack(side="left")
        self.symbol_box = ttk.Combobox(instrument_row, textvariable=self.symbol, state="normal", width=19,
                                        values=tuple(self.catalog))
        self.symbol_box.pack(side="left")
        self.symbol_box.bind("<<ComboboxSelected>>", self.change_symbol)
        self.symbol_box.bind("<Return>", self.change_symbol)
        self.symbol_box.bind("<FocusOut>", self.change_symbol)
        ttk.Label(instrument_row, text="输入模型传入的六位代码，可附.SH/.SZ/.BJ；无需预先配置证券。").pack(side="left", padx=8)
        settings = ttk.Frame(frame)
        settings.pack(fill="x")
        self.mode_box = ttk.Combobox(settings, textvariable=self.mode, state="readonly", width=29,
                                     values=("模拟（不连接客户端）", "实盘（测试中）"))
        self.mode_box.pack(side="left")
        self.mode_box.bind("<<ComboboxSelected>>", self.change_mode)
        ttk.Label(settings, text="  定价：").pack(side="left")
        self.price_box = ttk.Combobox(settings, textvariable=self.price_mode, state="readonly", width=12,
                                      values=("盘口限价", "手填限价"))
        self.price_box.pack(side="left")
        self.price_entry = ttk.Entry(settings, textvariable=self.manual_price, width=10)
        self.price_entry.pack(side="left", padx=6)
        ttk.Label(settings, text="  数量（股/份）：").pack(side="left")
        self.quantity_entry = ttk.Entry(settings, textvariable=self.quantity, width=9)
        self.quantity_entry.pack(side="left")
        source_row = ttk.Frame(frame)
        source_row.pack(fill="x", pady=(6, 0))
        ttk.Label(source_row, text="行情来源：").pack(side="left")
        self.source_box = ttk.Combobox(source_row, textvariable=self.quote_source, state="readonly", width=25,
                                      values=("同花顺客户端（默认）", "腾讯Web（对比）"))
        self.source_box.pack(side="left")
        self.source_box.bind("<<ComboboxSelected>>", self.change_quote_source)
        ttk.Label(source_row, text="  客户端读取失败会报明原因；可手动切换来源。").pack(side="left")
        ttk.Label(frame, textvariable=self.quote_text, wraplength=1010).pack(anchor="w", pady=(6, 2))
        ttk.Label(frame, text="盘口限价：买用卖一，卖用买一；刷新报价后再提交。限价委托不保证成交。", foreground="#555555").pack(anchor="w")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(8, 6))
        self.buttons = []
        self.trade_buttons = []
        self.action_buttons = {}
        for label, action in (("刷新盘口", "quote"), ("买入", "buy"), ("卖出", "sell"),
                              ("刷新账户/持仓", "account"), ("刷新委托状态", "refresh"), ("客户端适配检查", "check")):
            button = ttk.Button(actions, text=label, command=lambda a=action: self.start(a))
            button.pack(side="left", padx=(0, 8))
            self.buttons.append(button)
            self.action_buttons[action] = button
            if action in {"buy", "sell"}:
                self.trade_buttons.append(button)
        ttk.Button(actions, text="停止后续步骤", command=self.stop_requested).pack(side="right")
        ttk.Label(frame, textvariable=self.pending_hint, foreground="#975700", wraplength=1010).pack(anchor="w", fill="x", pady=(0, 7))
        self.result_label = tk.Label(frame, textvariable=self.result_text, anchor="w", justify="left",
                                     bg="#edf2f7", fg="#243746", padx=10, pady=6,
                                     font=("Microsoft YaHei UI", 10, "bold"), wraplength=1000)
        self.result_label.pack(fill="x", pady=(0, 8))
        self.result_label.bind("<Configure>", lambda event: self.result_label.configure(wraplength=max(200, event.width - 24)))
        # Reserve space for both panes; pack's earlier expanding tables used to
        # consume the window and squeeze the last-created log out of view.
        self.workspace = tk.PanedWindow(frame, orient="vertical", sashwidth=8, sashrelief="raised",
                                         bd=0, opaqueresize=True)
        self.workspace.pack(fill="both", expand=True)
        data_pane, self.log_pane = ttk.Frame(self.workspace), ttk.Frame(self.workspace)
        self.workspace.add(data_pane, minsize=140, stretch="always")
        self.workspace.add(self.log_pane, minsize=145, height=195, stretch="never")
        self.tabs = ttk.Notebook(data_pane)
        self.tabs.pack(fill="both", expand=True)
        orders_tab, account_tab, broker_tab, quote_tab = (ttk.Frame(self.tabs) for _ in range(4))
        self.account_tab = account_tab
        self.broker_tab, self.quote_tab = broker_tab, quote_tab
        self.tabs.add(orders_tab, text="本地执行记录")
        self.tabs.add(broker_tab, text="券商当日委托")
        self.tabs.add(account_tab, text="账户与持仓")
        self.tabs.add(quote_tab, text="行情与五档")
        self.check_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.check_tab, text="首次适配检查")
        recovery = ttk.Frame(orders_tab, padding=(6, 4))
        recovery.pack(side="bottom", fill="x")
        ttk.Label(recovery, text="选中记录后核对回执或撤单；UNKNOWN不自动重试，查询核对完整委托号。", foreground="#555555").pack(anchor="w")
        row = ttk.Frame(recovery)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="补录完整委托号：").pack(side="left")
        self.broker_entry = ttk.Entry(row, textvariable=self.broker_id, width=22)
        self.broker_entry.pack(side="left", padx=(0, 8))
        for label, action in (("查询选中回执", "query"), ("接续原确认/回执", "resume"), ("撤销选中委托", "cancel"), ("核对未提交", "discard")):
            button = ttk.Button(row, text=label, command=lambda a=action: self.start(a))
            button.pack(side="left", padx=(0, 6))
            self.buttons.append(button)
            self.action_buttons[action] = button
        self.check_view = tk.Text(self.check_tab, wrap="word", height=10, padx=12, pady=12, state="disabled")
        check_scroll = ttk.Scrollbar(self.check_tab, orient="vertical", command=self.check_view.yview)
        self.check_view.configure(yscrollcommand=check_scroll.set)
        check_scroll.pack(side="right", fill="y")
        self.check_view.pack(side="left", fill="both", expand=True)
        self.check_text.trace_add("write", self.render_check_text)
        self.render_check_text()
        ttk.Label(broker_tab, textvariable=self.broker_text, wraplength=1000).pack(anchor="w", padx=8, pady=8)
        self.broker_table = ttk.Treeview(broker_tab, columns=("symbol", "side", "quantity", "price", "status", "filled", "average", "broker"), show="headings")
        for key, label, width in zip(("symbol", "side", "quantity", "price", "status", "filled", "average", "broker"),
                                    ("代码", "方向", "委托数量", "委托价", "券商状态", "成交数量", "成交均价", "完整委托编号"),
                                    (100, 55, 90, 90, 160, 90, 100, 230)):
            self.broker_table.heading(key, text=label)
            self.broker_table.column(key, width=width)
        broker_scroll = ttk.Scrollbar(broker_tab, orient="vertical", command=self.broker_table.yview)
        self.broker_table.configure(yscrollcommand=broker_scroll.set)
        broker_scroll.pack(side="right", fill="y")
        self.broker_table.pack(side="left", fill="both", expand=True)
        ttk.Label(quote_tab, text="价格与数量来自所选来源；数量单位保留客户端/接口原值。— 表示未取得可靠字段。", wraplength=1000).pack(anchor="w", padx=8, pady=8)
        self.book_table = ttk.Treeview(quote_tab, columns=("level", "bid", "bid_qty", "ask", "ask_qty"), show="headings", height=5)
        for key, label in zip(("level", "bid", "bid_qty", "ask", "ask_qty"), ("档位", "买价", "买量", "卖价", "卖量")):
            self.book_table.heading(key, text=label)
            self.book_table.column(key, width=170)
        self.book_table.pack(fill="both", expand=True)
        ttk.Label(account_tab, textvariable=self.account_text, wraplength=1000).pack(anchor="w", padx=8, pady=8)
        positions_frame = ttk.Frame(account_tab)
        positions_frame.pack(fill="both", expand=True)
        self.positions_table = ttk.Treeview(positions_frame, columns=("symbol", "name", "total", "available", "price", "value"), show="headings", height=6)
        for key, label, width in zip(("symbol", "name", "total", "available", "price", "value"),
            ("代码", "名称", "总持仓", "可卖数量", "客户端市价", "市值"), (100, 260, 130, 130, 130, 140)):
            self.positions_table.heading(key, text=label)
            self.positions_table.column(key, width=width)
        position_scroll = ttk.Scrollbar(positions_frame, orient="vertical", command=self.positions_table.yview)
        self.positions_table.configure(yscrollcommand=position_scroll.set)
        position_scroll.pack(side="right", fill="y")
        self.positions_table.pack(side="left", fill="both", expand=True)
        self.positions_table.bind("<Double-1>", self.select_position)
        columns = ("symbol", "side", "quantity", "price", "status", "broker", "filled")
        self.table = ttk.Treeview(orders_tab, columns=columns, show="headings", height=8, selectmode="browse")
        for name, title, width in zip(columns, ("代码", "方向", "份数", "限价", "状态", "完整委托编号", "累计成交"), (100, 55, 55, 85, 230, 250, 85)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=50)
        self.table.tag_configure("unknown", foreground="#b3261e")
        self.table.tag_configure("pending", background="#fff3d1")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.select_order)
        ttk.Label(self.log_pane, text="运行日志 · 最新记录在下方（可拖动上方分隔条调整高度）",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(3, 6))
        log_body = ttk.Frame(self.log_pane)
        log_body.pack(fill="both", expand=True)
        self.log_box = tk.Text(log_body, height=8, wrap="word", state="disabled", padx=8, pady=6,
                               bg="#f6f8fa", fg="#243746", font=("Microsoft YaHei UI", 10))
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)
        self.log_box.tag_configure("error", foreground="#b3261e")
        self.rows = {}
        self.broker_id.trace_add("write", lambda *args: self.update_recovery_buttons())
        self.change_banner()
        self.change_symbol()
        threading.Thread(target=self.worker, daemon=True, name="easytrade-user-console").start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(80, self.poll)
        self.start("list")

    def render_check_text(self, *args):
        self.check_view.configure(state="normal")
        self.check_view.delete("1.0", "end")
        self.check_view.insert("1.0", self.check_text.get())
        self.check_view.configure(state="disabled")

    def change_banner(self):
        live = self.mode.get().startswith("实盘")
        self.banner.set("实盘：按钮将提交所选证券、数量与限价；资金和可卖数量最终由券商核验。\n首次先做客户端适配检查；运行时保持客户端大小/列宽/缩放，调整后重新检查。" if live else
                        "模拟模式：独立模拟资金及固定样本报价，买卖仅修改本地模拟账本。\n模拟不验证实际T+1可卖限制；切换实盘后，按钮才操作交易客户端。")
        self.banner_label.configure(bg="#fce8e6" if live else "#e7f1ea", fg="#9c2018" if live else "#225a34")

    def change_mode(self, event=None):
        try:
            config = load_config(self.project_root, live=self.mode.get().startswith("实盘"))
            self.catalog = {}
            self.holding_choices = {}
            self.symbol_box.configure(values=tuple(self.catalog))
            self.symbol.set("")
            self.change_symbol()
        except Exception as error:
            self.append_log(str(error))
        self.change_banner()
        for item in self.table.get_children():
            self.table.delete(item)
        self.rows.clear()
        for table in (self.broker_table, self.book_table):
            for item in table.get_children():
                table.delete(item)
        self.broker_text.set("券商当日委托尚未读取")
        self.account_text.set("账户与持仓尚未读取")
        self.check_text.set("切换模式后需重新检查。客户端适配检查只读取账户与当日委托，不会发单。\n新电脑、客户端升级、窗口/列宽/DPI调整后，请检查并人工对照价格小数点与完整编号。")
        for item in self.positions_table.get_children():
            self.positions_table.delete(item)
        self.pending_hint.set("")
        self.broker_id.set("")
        self.quote_text.set("输入代码后可刷新客户端盘口" if self.mode.get().startswith("实盘") else "模拟执行请填写模型传入的明确限价")
        self.start("list")

    def change_symbol(self, event=None):
        if self.busy:
            return
        self.symbol.set(self.symbol.get().strip().upper())
        self.quote_text.set((f"{self.symbol.get()} 行情尚未刷新" if self.symbol.get() else "输入代码后可刷新客户端盘口")
                            if self.mode.get().startswith("实盘") else "模拟执行请填写模型传入的明确限价")
        if hasattr(self, "book_table"):
            for item_id in self.book_table.get_children():
                self.book_table.delete(item_id)

    def update_symbol_choices(self, positions):
        self.holding_choices = {}
        for row in positions:
            symbol = row["symbol"]
            self.holding_choices[symbol] = row
        self.symbol_box.configure(values=tuple(self.holding_choices))

    def select_position(self, event=None):
        if self.busy:
            return
        selected = self.positions_table.selection()
        if not selected:
            return
        code = str(self.positions_table.item(selected[0], "values")[0])
        self.symbol.set(code)
        self.change_symbol()

    def change_quote_source(self, event=None):
        self.quote_text.set(f"已选择{self.quote_source.get()}；请刷新行情，发单时也使用该来源")
        for item in self.book_table.get_children():
            self.book_table.delete(item)

    def select_order(self, event=None):
        selected = self.table.selection()
        if selected and selected[0] in self.rows:
            order = self.rows[selected[0]]
            self.broker_id.set(order["broker_order_id"] or (order["detail"] or {}).get("broker_order_id_hint") or "")
        self.update_recovery_buttons()

    def show_result(self, message, level="info"):
        colors = {"info": ("#edf2f7", "#243746"), "busy": ("#e8f0fe", "#174ea6"),
                  "success": ("#e7f1ea", "#225a34"), "error": ("#fce8e6", "#9c2018")}
        background, foreground = colors[level]
        self.result_text.set(message)
        self.result_label.configure(bg=background, fg=foreground)

    def append_log(self, message, level="info"):
        from datetime import datetime
        record_action(self.project_root, "ui_message", message=message)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", datetime.now().strftime("%H:%M:%S ") + message + "\n", (level,))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_busy(self, busy):
        self.busy = busy
        for button in self.buttons:
            button.configure(state="disabled" if busy else "normal")
        for box in (self.mode_box, self.price_box, self.source_box, self.symbol_box):
            box.configure(state="disabled" if busy else "readonly")
        self.symbol_box.configure(state="disabled" if busy else "normal")
        self.price_entry.configure(state="disabled" if busy else "normal")
        self.quantity_entry.configure(state="disabled" if busy else "normal")
        self.broker_entry.configure(state="disabled" if busy else "normal")
        if not busy:
            self.update_pending_hint()
        self.update_recovery_buttons()

    def update_recovery_buttons(self):
        selected = self.table.selection()
        order = self.rows.get(selected[0]) if selected else None
        live = self.mode.get().startswith("实盘")
        status = order["status"] if order else None
        broker_id = order["broker_order_id"] if order else None
        hint = (order["detail"] or {}).get("broker_order_id_hint") if order else None
        user_unknown = bool(order and order["mode"] == "ths-user" and status == "UNKNOWN" and not broker_id)
        states = {
            "query": bool(order and (status in TERMINAL or broker_id or hint or self.broker_id.get().strip())),
            "resume": live and user_unknown,
            "cancel": bool(live and order and broker_id and status not in TERMINAL),
            "discard": bool(live and order and not broker_id and not hint and (user_unknown or
                (order["mode"] == "ths-draft" and status == "READY_FOR_MANUAL_SUBMIT"))),
        }
        for action, enabled in states.items():
            self.action_buttons[action].configure(state="normal" if enabled and not self.busy else "disabled")

    def update_pending_hint(self):
        pending = next((order for order in self.rows.values()
                        if self.mode.get().startswith("实盘") and order["mode"] != "paper"
                        and order["status"] not in TERMINAL), None)
        for button in self.trade_buttons:
            button.configure(state="disabled" if self.busy else "normal")
        if pending is None:
            self.pending_hint.set("")
            return
        if pending["status"] == "READY_FOR_MANUAL_SUBMIT":
            self.pending_hint.set("历史记录有未核对草稿，可选中后查询或核对未提交；新买卖指令独立执行，不自动补发旧单。")
        elif pending["status"] == "UNKNOWN":
            self.pending_hint.set("历史委托结果未知，可选中后查询或接续原回执；不自动重发旧单。新买卖指令独立执行，客户端有未处理弹窗时会返回原因。")
        else:
            self.pending_hint.set("历史委托仍显示待成交，可选中后查询最新回执；不影响发起新的买入、卖出或查询账户。")
        if not self.table.selection():
            self.table.selection_set(pending["signal_id"])
            self.table.see(pending["signal_id"])

    def start(self, action):
        from tkinter import messagebox
        if self.busy:
            return
        selected = self.table.selection()
        signal_id = selected[0] if selected else None
        if action in {"query", "resume", "cancel", "discard"} and not signal_id:
            self.show_result("未开始 · 先在本地执行记录中选中一笔订单。", "error")
            self.append_log("先在表格中选中一笔订单。")
            return
        declared_no_confirmation = False
        if action == "discard":
            order = self.rows[signal_id]
            if order["status"] == "UNKNOWN":
                plan = order["plan"]
                prompt = (f"{plan['symbol']}，{'买入' if plan['side'] == 'buy' else '卖出'}{plan['quantity']}份，原限价{plan['limit_price']}元。\n\n"
                          "只有你确定这笔从未点过“是/确认”提交，或只点了“否/关闭”，才可解除本地记录。\n"
                          "已点过是或不确定时，请选“否”，按完整委托号查询。\n\n"
                          "我确定这笔从未确认提交，解除本地记录？（不发送撤单、不重新买卖）")
                if not messagebox.askyesno("核对未提交", prompt, default="no", parent=self.root):
                    return
                action, declared_no_confirmation = "resolve_unsubmitted", True
            elif not messagebox.askyesno("核对旧草稿", f"只有你确认从未在客户端提交过这笔草稿，才可清空。\n\n{signal_id}\n\n确认它从未提交？", default="no", parent=self.root):
                return
        manual = self.manual_price.get().strip() if self.price_mode.get() == "手填限价" else None
        if action in {"quote", "buy", "sell"}:
            self.symbol.set(self.symbol.get().strip().upper())
            try:
                validate_symbol(self.symbol.get())
            except ValueError as error:
                self.show_result("未开始 · " + str(error), "error")
                return
        quantity = 100
        if action in {"buy", "sell"}:
            raw_quantity = self.quantity.get().strip()
            if not raw_quantity.isascii() or not raw_quantity.isdigit() or int(raw_quantity) <= 0:
                self.show_result("未开始 · 数量请填写正整数，交易单位由券商核验。", "error")
                self.append_log("数量请填写正整数。")
                return
            quantity = int(raw_quantity)
        if action in {"buy", "sell"} and manual == "":
            self.show_result("未开始 · 请填入合法限价，或选择盘口限价。", "error")
            self.append_log("请填入合法限价，或选择盘口限价。")
            return
        self.stop.clear()
        self.set_busy(True)
        self.show_result(f"进行中 · {ACTION_LABELS.get(action, action)}，请等待结果…", "busy")
        self.tasks.put({"action": action, "live": self.mode.get().startswith("实盘"), "manual": manual,
                        "quote_source": "tencent_web" if self.quote_source.get().startswith("腾讯") else "ths_client",
                        "quantity": quantity,
                        "symbol": self.symbol.get(),
                        "signal_id": signal_id, "broker_id": self.broker_id.get().strip() or None,
                        "user_declared_no_confirmation": declared_no_confirmation,
                        "request_id": "console-" + uuid4().hex})

    def stop_requested(self):
        self.stop.set()
        self.append_log("已要求停止后续步骤；已发出的委托不会自动撤销，请核对后使用撤单。")

    def worker(self):
        sessions = {}
        initialized_com = False
        try:
            import os
            if os.name == "nt":
                import pythoncom
                pythoncom.CoInitialize()
                initialized_com = True
            while True:
                task = self.tasks.get()
                if task is None:
                    return
                session = None
                started = time.monotonic()
                try:
                    live, action = task["live"], task["action"]
                    record_action(self.project_root, "action_started", **task)
                    config = load_config(self.project_root, live=live)
                    if live not in sessions or sessions[live].config != config:
                        sessions[live] = UserSession(self.project_root, config, live=live,
                            log=lambda message, live=live: self.messages.put(("log", ("[实盘] " if live else "[模拟] ") + message)))
                    session = sessions[live]
                    session.set_quote_source(task["quote_source"])
                    if action == "quote":
                        quote_started = time.monotonic()
                        quote = session.quote(task["symbol"])
                        record_action(self.project_root, "quote_snapshot", live=live, symbol=quote.symbol,
                            bid=str(quote.bid), ask=str(quote.ask), bid_quantity=quote.bid_quantity,
                            ask_quantity=quote.ask_quantity, last=str(quote.last) if quote.last is not None else None,
                            session=quote.session, source=quote.source, snapshot=quote.payload(),
                            elapsed_ms=round((time.monotonic() - quote_started) * 1000, 1), observed_at=quote.observed_at.isoformat())
                        self.messages.put(("quote", quote.describe()))
                        self.messages.put(("quote_snapshot", quote.payload()))
                    elif action in {"buy", "sell"}:
                        result = session.submit_and_follow(action, symbol=task["symbol"], quantity=task["quantity"], manual_price=task["manual"], signal_id=task["request_id"], stop=self.stop)
                        if session.last_account_snapshot:
                            self.messages.put(("account", session.last_account_snapshot))
                        if result["status"] == "FILLED" and not self.stop.is_set():
                            try:
                                snapshot = session.account_snapshot()
                                self.messages.put(("account", snapshot))
                                record_action(self.project_root, "account_snapshot", live=live, snapshot=snapshot)
                            except Exception as error:
                                self.messages.put(("log", "成交已确认，但账户快照更新失败：" + str(error)))
                                self.messages.put(("account_error", str(error)))
                    elif action == "query":
                        session.query(task["signal_id"], task["broker_id"])
                    elif action == "refresh":
                        snapshot = session.broker_orders_snapshot()
                        self.messages.put(("broker_orders", snapshot))
                        record_action(self.project_root, "broker_orders_snapshot", live=live, snapshot=snapshot)
                    elif action == "resume":
                        session.resume_confirmation(task["signal_id"])
                    elif action == "cancel":
                        session.cancel(task["signal_id"])
                    elif action == "discard":
                        session.discard_unsubmitted(task["signal_id"])
                    elif action == "resolve_unsubmitted":
                        session.resolve_unsubmitted(task["signal_id"], user_declared_no_confirmation=task["user_declared_no_confirmation"])
                    elif action == "account":
                        snapshot = session.account_snapshot()
                        self.messages.put(("account", snapshot))
                        record_action(self.project_root, "account_snapshot", live=live, snapshot=snapshot)
                    elif action == "check":
                        report = session.check_client()
                        if report["account_snapshot"] is not None:
                            self.messages.put(("account", report["account_snapshot"]))
                        if report["broker_snapshot"] is not None:
                            self.messages.put(("broker_orders", report["broker_snapshot"]))
                        self.messages.put(("client_check", report))
                    if action in {"buy", "sell"}:
                        status = result["status"]
                        self.messages.put(("result", (f"{ACTION_LABELS[action]} · {STATUSES.get(status, status)}；详情见记录与运行日志。",
                            "success" if status == "FILLED" else "error" if status in {"REJECTED", "UNKNOWN", "PREPARATION_FAILED"} else "info")))
                    elif action != "list":
                        self.messages.put(("result", (f"{ACTION_LABELS.get(action, action)} · 操作已返回，请核对下方数据与运行日志。", "info")))
                    else:
                        self.messages.put(("result", ("就绪 · 本地记录已读取；每次操作结果会显示在这里，详细过程见下方运行日志。", "info")))
                except Exception as error:
                    self.messages.put(("result", (f"{ACTION_LABELS.get(task['action'], task['action'])}未完成 · {error}", "error")))
                    self.messages.put(("error_log", str(error)))
                    if isinstance(error, QuoteUnavailable) or task["action"] == "quote":
                        self.messages.put(("quote", "盘口定价当前不可用：" + str(error)))
                        self.messages.put(("quote_snapshot", None))
                    if task["action"] == "refresh":
                        self.messages.put(("broker_error", str(error)))
                    if task["action"] == "account":
                        self.messages.put(("account_error", str(error)))
                    record_action(self.project_root, "action_error", action=task["action"], request_id=task["request_id"], error=str(error), traceback=traceback.format_exc())
                    log_path = self.project_root / "runtime/console-errors.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as file:
                        file.write(traceback.format_exc() + "\n")
                finally:
                    if session is not None:
                        try:
                            orders = session.orders()
                            self.messages.put(("orders", orders))
                            record_action(self.project_root, "order_snapshot", action=task["action"], request_id=task["request_id"],
                                orders=[{k: row[k] for k in ("signal_id", "status", "broker_order_id", "filled_quantity", "detail")} for row in orders])
                        except Exception as error:
                            self.messages.put(("log", str(error)))
                    record_action(self.project_root, "action_finished", action=task["action"], request_id=task["request_id"], elapsed_ms=round((time.monotonic()-started)*1000, 1))
                    self.messages.put(("done", None))
        finally:
            if initialized_com:
                pythoncom.CoUninitialize()

    def poll(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self.append_log(value)
                elif kind == "error_log":
                    self.append_log(value, "error")
                elif kind == "result":
                    self.show_result(*value)
                elif kind == "quote":
                    self.quote_text.set(value)
                elif kind == "quote_snapshot":
                    for item in self.book_table.get_children():
                        self.book_table.delete(item)
                    if value is not None:
                        bids = {row['level']: row for row in value['bids']}
                        asks = {row['level']: row for row in value['asks']}
                        def cell(rows, level, key):
                            raw = rows.get(level, {}).get(key)
                            return "—" if raw is None else raw
                        for level in range(1, 6):
                            self.book_table.insert("", "end", values=(level, cell(bids, level, 'price'), cell(bids, level, 'quantity'),
                                                                     cell(asks, level, 'price'), cell(asks, level, 'quantity')))
                elif kind == "broker_orders":
                    scope = "已核对完整滚动范围" if value['complete'] else "仅已读取的可见范围，可能还有其他委托"
                    self.broker_text.set(f"来源 {value['source']} | {value['observed_at']} | {scope} | 共{len(value['rows'])}条"
                                         + ("\n" + value['incomplete_reason'] if value.get('incomplete_reason') else ""))
                    for item in self.broker_table.get_children():
                        self.broker_table.delete(item)
                    for row in value['rows']:
                        self.broker_table.insert("", "end", values=(row['symbol'], "买入" if row['side'] == 'buy' else "卖出",
                            row['quantity'], row.get('limit_price', '—'), row.get('raw_status') or STATUSES.get(row['status'], row['status']),
                            row['filled_quantity'], row.get('average_price', '—'), row['order_id']))
                    self.tabs.select(self.broker_tab)
                elif kind == "broker_error":
                    self.broker_text.set("本次委托读取失败：" + value + "\n下方若有数据为上次快照。\n" + self.broker_text.get())
                    self.tabs.select(self.broker_tab)
                elif kind == "account":
                    self.update_symbol_choices(value["positions"])
                    balances = value["balances"]
                    scope = ("完整持仓" if value['source'] != 'paper' else "完整模拟账本") if value["positions_complete"] else "客户端可见持仓，尚不保证包含分页/滚动区外记录"
                    self.account_text.set(f"账户 {value['account']} | 资金余额 {balances.get('cash_balance', '—')} | 可用资金 {balances['available_cash']} | 冻结 {balances.get('frozen_cash', '—')} | "
                        f"股票市值 {balances.get('stock_value', '—')} | 总资产 {balances.get('total_assets', '—')}\n"
                        f"来源 {value['source']} | {value['observed_at']} | {scope}，共{len(value['positions'])}项"
                        + ("\n未确认完整性：" + value["positions_incomplete_reason"] if value.get("positions_incomplete_reason") else "")
                        + ("\n显示提示：" + "；".join(value["positions_read_warnings"][:3]) if value.get("positions_read_warnings") else ""))
                    for item in self.positions_table.get_children():
                        self.positions_table.delete(item)
                    for row in value["positions"]:
                        self.positions_table.insert("", "end", values=(row["symbol"], row.get("name", "—"), row["position_quantity"],
                            row["available_quantity"], row.get("current_price", "—"), row.get("market_value", "—")))
                    self.tabs.select(self.account_tab)
                elif kind == "client_check":
                    layout = value["layout"]
                    geometry = f"窗口大小 {layout.get('window_size', '未取得')}；DPI {layout.get('dpi') or '未取得'}；表格范围 {layout.get('grid_box', '未取得')}"
                    self.check_text.set(("模拟检查不代表实际客户端已适配。\n" if value['mode'] == 'paper' else "只读检查完成，未发送买卖或撤单。\n")
                        + geometry + "\n检查时间 " + value['observed_at'] + "\n\n"
                        + "\n\n".join(f"{row['item']}：{row['status']}\n{row['detail']}" for row in value['checks'])
                        + "\n\n人工核对一致后保持此布局；窗口、列宽、显示缩放、显示器或客户端版本变化后重新检查。报告仅保存在本机 runtime/client-check-*.json。")
                    self.tabs.select(self.check_tab)
                elif kind == "account_error":
                    self.account_text.set("本次读取失败：" + value + "\n下方若有数据，仅为上次成功快照，请核对其原时间。\n" + self.account_text.get())
                elif kind == "orders":
                    selected = self.table.selection()
                    for item in self.table.get_children():
                        self.table.delete(item)
                    self.rows = {order["signal_id"]: order for order in value}
                    for order in value:
                        plan = order["plan"] or order["signal"]
                        self.table.insert("", "end", iid=order["signal_id"], values=(plan["symbol"], "买入" if plan["side"] == "buy" else "卖出",
                            plan.get("quantity", ""), plan["limit_price"], STATUSES.get(order["status"], order["status"]),
                            order["broker_order_id"] or "尚未取得", order["filled_quantity"]), tags=("unknown",) if order["status"] == "UNKNOWN" else ("pending",) if order["status"] == "READY_FOR_MANUAL_SUBMIT" else ())
                    if selected and selected[0] in self.rows:
                        self.table.selection_set(selected[0])
                elif kind == "done":
                    self.set_busy(False)
                    if self.closing:
                        self.tasks.put(None)
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def close(self):
        if self.busy:
            self.closing = True
            self.stop_requested()
            self.append_log("当前原生调用结束后关闭；结果不明的订单将保留记录。")
        else:
            self.tasks.put(None)
            self.root.destroy()


def main():
    import argparse
    import tkinter as tk
    parser = argparse.ArgumentParser(description="Easytrade通用交易测试台")
    parser.add_argument("--root", default=str(ROOT), help="配置/运行数据目录")
    parser.add_argument("--mode", choices=("paper", "ths-live"), default="paper")
    args = parser.parse_args()
    root = tk.Tk()
    Console(root, args.root, live=args.mode == "ths-live")
    root.mainloop()


if __name__ == "__main__":
    main()
