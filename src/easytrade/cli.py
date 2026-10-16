from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from .adapters import PaperAdapter
from .domain import Signal, utc_now
from .engine import Executor
from .lease import session_lease
from .service import create_server
from .store import Store


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def make_executor(config, mode, database):
    store = Store(database)
    if mode == "paper":
        adapter = PaperAdapter(store, config["accounts"])
    else:
        from .ths import ThsDraftAdapter
        adapter = ThsDraftAdapter(config["ths_profile"])
    executor = Executor(store, adapter, config)
    recovered = store.recover(mode)
    if recovered:
        print(json.dumps({"recovered_to_unknown": recovered}, ensure_ascii=False), flush=True)
    return executor


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地量化执行端：模拟、同花顺草稿或显式实盘执行")
    parser.add_argument("--config", default=None)
    parser.add_argument("--root", default=".", help="本地配置/运行数据目录，默认当前目录")
    parser.add_argument("--db", default=None)
    parser.add_argument("--mode", choices=["paper", "ths-draft", "ths-live"], default="paper")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("signal")
    submit.add_argument("file")
    submit.add_argument("--scenario", choices=["fill", "partial", "reject", "timeout_after_submit"], default="fill")
    get = commands.add_parser("order")
    get.add_argument("signal_id")
    commands.add_parser("events")
    commands.add_parser("account")
    commands.add_parser("broker-orders")
    quote = commands.add_parser("quote")
    quote.add_argument("symbol")
    ack = commands.add_parser("ack")
    ack.add_argument("event_ids", type=int, nargs="+")
    reconcile = commands.add_parser("reconcile-paper")
    reconcile.add_argument("signal_id")
    receipt = commands.add_parser("receipt")
    receipt.add_argument("signal_id")
    receipt.add_argument("file")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    commands.add_parser("demo")
    commands.add_parser("inspect-dialogs")
    read = commands.add_parser("read-table")
    read.add_argument("role", choices=["positions", "orders", "trades"])
    discard = commands.add_parser("discard-draft")
    discard.add_argument("signal_id")
    discard.add_argument("--not-submitted", action="store_true", required=True, help="明确确认尚未手动提交；如果已提交或不确定，必须核对回执")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    config = load_json(root / (args.config or ("config/ths.local.json" if args.mode in {"ths-live", "ths-draft"} else "config/paper.example.json")))
    database = root / args.db if args.db else None
    if args.mode == "ths-live" or args.command in {"account", "broker-orders", "quote"} or (args.mode == "paper" and args.command in {"serve", "signal", "order", "events", "ack"}):
        if args.mode == "ths-draft":
            parser.error("账户/行情查询与统一接口请使用paper或ths-live模式")
        from .user_session import UserSession
        session = UserSession(root, config, live=args.mode == "ths-live", database=database)
        if args.command == "serve":
            server = create_server(session, os.environ.get("EASYTRADE_TOKEN", ""), args.port)
            print(json.dumps({"listening": f"http://127.0.0.1:{server.server_port}", "mode": args.mode,
                              "real_submit_supported": session.live}, ensure_ascii=False), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return
        if args.command == "signal":
            if args.scenario != "fill":
                parser.error("通用接口不接受模拟故障参数；故障注入由离线测试覆盖")
            result = session.execute(load_json(root / args.file))
        elif args.command == "account":
            result = session.account_snapshot()
        elif args.command == "broker-orders":
            result = session.broker_orders_snapshot()
        elif args.command == "quote":
            result = session.quote(args.symbol).payload()
        elif args.command == "order":
            result = session.store.get(args.signal_id)
        elif args.command == "events":
            result = session.store.pending_events()
        elif args.command == "ack":
            session.store.acknowledge(args.event_ids)
            result = {"acknowledged": True}
        else:
            parser.error("该命令不支持ths-live；通过HTTP接口查询回执或撤单")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    database = database or root / f"runtime/{args.mode}.sqlite"
    lease_path = root / config.get("session_lock", f"runtime/{args.mode}.lock")
    with session_lease(lease_path):
        if args.command == "inspect-dialogs":
            if args.mode != "ths-draft":
                parser.error("inspect-dialogs需要--mode ths-draft")
            from .ths import ThsDraftAdapter
            print(json.dumps(ThsDraftAdapter(config["ths_profile"]).observe_dialogs(), ensure_ascii=False, indent=2))
            return
        executor = make_executor(config, args.mode, database)
        if args.command == "serve":
            server = create_server(executor, os.environ.get("EASYTRADE_TOKEN", ""), args.port)
            print(json.dumps({"listening": f"http://127.0.0.1:{server.server_port}", "mode": args.mode, "real_submit_supported": False}), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return
        if args.command == "signal":
            result = executor.execute(load_json(args.file), scenario=args.scenario)
        elif args.command == "order":
            result = executor.store.get(args.signal_id)
        elif args.command == "events":
            result = executor.store.pending_events()
        elif args.command == "ack":
            executor.store.acknowledge(args.event_ids)
            result = {"acknowledged": True}
        elif args.command == "receipt":
            result = executor.store.record_receipt(args.signal_id, load_json(args.file))
        elif args.command == "reconcile-paper":
            result = executor.reconcile_paper(args.signal_id)
        elif args.command == "read-table":
            if args.mode != "ths-draft":
                parser.error("read-table需要--mode ths-draft")
            from dataclasses import asdict
            result = asdict(executor.adapter.read_table(args.role))
        elif args.command == "discard-draft":
            if args.mode != "ths-draft":
                parser.error("discard-draft需要--mode ths-draft")
            result = executor.discard_draft(args.signal_id)
        else:
            if args.mode != "paper":
                parser.error("demo仅在paper模式运行")
            expires = (utc_now() + timedelta(minutes=10)).isoformat()
            demo_symbol = next(iter(config["instruments"]))
            base = {"account": next(iter(config["accounts"])), "symbol": demo_symbol,
                    "limit_price": str(executor.instruments[demo_symbol].price_tick * 1000), "expires_at": expires}
            demo_id = "demo-" + utc_now().strftime("%Y%m%dT%H%M%S%f")
            buy = {**base, "signal_id": demo_id + "-buy", "side": "buy", "budget": "10000"}
            sell = {**base, "signal_id": demo_id + "-sell", "side": "sell", "quantity": 100}
            result = {"buy": executor.execute(buy), "buy_replay": executor.execute(buy), "sell": executor.execute(sell)}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

