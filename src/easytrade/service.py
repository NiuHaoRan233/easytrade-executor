from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .domain import ValidationError
from .engine import Executor
from .store import ConflictError


def create_server(executor: Executor, token: str, port: int = 8765) -> HTTPServer:
    if len(token) < 24:
        raise ValidationError("EASYTRADE_TOKEN至少需要24个字符")
    from .user_session import UserSession
    session = executor if isinstance(executor, UserSession) else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Avoid tokens and private account contents in access logs.
            return

        def send_json(self, status, data):
            content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                # The order is durable. A disconnected response never triggers replay.
                pass

        def authorize(self):
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, "Bearer " + token):
                self.send_json(401, {"error": "unauthorized"})
                return False
            return True

        def body(self):
            if self.headers.get("Transfer-Encoding"):
                raise ValidationError("不接受分块请求体")
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValidationError("请求长度不合法") from error
            if not 0 < size <= 65536:
                raise ValidationError("请求体必须在1至65536字节之间")
            self.connection.settimeout(5)
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def dispatch(self, method):
            if not self.authorize():
                return
            parsed = urlsplit(self.path)
            parts = [unquote(x) for x in parsed.path.strip("/").split("/")]
            try:
                if method == "GET" and parts == ["health"]:
                    self.send_json(200, {"mode": session.mode if session else executor.adapter.mode, "status": "running",
                                         "client_ready": None, "real_submit_supported": bool(session and session.live),
                                         "account": session.account if session else None})
                elif method == "POST" and parts == ["signals"]:
                    self.send_json(200, executor.execute(self.body()))
                elif session and method == "GET" and parts == ["instruments"]:
                    self.send_json(200, {"instruments": session.config["instruments"], "default_symbol": session.symbol})
                elif session and method == "GET" and parts == ["account"]:
                    self.send_json(200, session.account_snapshot())
                elif session and method == "GET" and parts == ["broker-orders"]:
                    self.send_json(200, session.broker_orders_snapshot())
                elif session and method == "GET" and len(parts) == 2 and parts[0] == "quotes":
                    self.send_json(200, session.quote(parts[1]).payload())
                elif session and method == "GET" and parts == ["orders"]:
                    self.send_json(200, {"orders": session.orders()})
                elif method == "GET" and len(parts) == 2 and parts[0] == "orders":
                    order = executor.store.get(parts[1])
                    if session and order["signal"]["account"] != session.account:
                        raise KeyError(parts[1])
                    self.send_json(200, order)
                elif session and method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "query":
                    data = self.body()
                    if set(data) - {"broker_order_id"}:
                        raise ValidationError("query只接受可选的broker_order_id")
                    self.send_json(200, session.query(parts[1], data.get("broker_order_id")))
                elif session and method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "cancel":
                    if self.body():
                        raise ValidationError("cancel请求体必须是空对象")
                    self.send_json(200, session.cancel(parts[1]))
                elif not session and method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "receipts":
                    self.send_json(200, executor.store.record_receipt(parts[1], self.body()))
                elif method == "GET" and parts == ["events"]:
                    after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                    self.send_json(200, {"events": executor.store.pending_events(after)})
                elif method == "POST" and parts == ["events", "ack"]:
                    data = self.body()
                    if set(data) != {"event_ids"}:
                        raise ValidationError("ack只接受event_ids")
                    executor.store.acknowledge(data["event_ids"])
                    self.send_json(200, {"acknowledged": True})
                else:
                    self.send_json(404, {"error": "not_found"})
            except ConflictError as error:
                self.send_json(409, {"error": str(error)})
            except KeyError:
                self.send_json(404, {"error": "order_not_found"})
            except (ValidationError, ValueError, TypeError, TimeoutError) as error:
                self.send_json(400, {"error": str(error)})
            except Exception:
                self.send_json(500, {"error": "internal_error; query order state before retrying"})

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

    # One thread owns the Windows adapter for its entire lifetime. Requests queue
    # in the socket backlog; no GUI operations from concurrent request threads.
    return HTTPServer(("127.0.0.1", port), Handler)

