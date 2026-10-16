"""Small strategy-side client. Transport errors never create a new signal ID."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeliveryUncertain(RuntimeError):
    pass


class Client:
    def __init__(self, token: str, base_url: str = "http://127.0.0.1:8765", timeout: float = 60):
        self.token, self.base_url, self.timeout = token, base_url.rstrip("/"), timeout

    def request(self, path, payload=None):
        request = Request(self.base_url + path, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            raise RuntimeError(f"执行端返回HTTP {error.code}: {error.read().decode('utf-8')}") from error
        except (URLError, TimeoutError, ConnectionError) as error:
            raise DeliveryUncertain("请求响应不确定。先按原signal_id查询；重发也必须保留原signal_id，不能创建新单号。") from error

    def signal(self, signal):
        return self.request("/signals", signal)

    def order(self, signal_id):
        from urllib.parse import quote
        return self.request("/orders/" + quote(signal_id, safe=""))

    def events(self):
        return self.request("/events")["events"]

    def acknowledge(self, event_ids):
        return self.request("/events/ack", {"event_ids": event_ids})

    def account(self):
        return self.request("/account")

    def instruments(self):
        return self.request("/instruments")

    def quote(self, symbol):
        from urllib.parse import quote
        return self.request("/quotes/" + quote(symbol, safe=""))

    def broker_orders(self):
        return self.request("/broker-orders")

    def query(self, signal_id, broker_order_id=None):
        from urllib.parse import quote
        return self.request("/orders/" + quote(signal_id, safe="") + "/query",
                            {"broker_order_id": broker_order_id} if broker_order_id else {})

    def cancel(self, signal_id):
        from urllib.parse import quote
        return self.request("/orders/" + quote(signal_id, safe="") + "/cancel", {})
