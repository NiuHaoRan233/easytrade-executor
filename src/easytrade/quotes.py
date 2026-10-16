"""Read-only public quotes. A book limit is not an exchange market order."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.request import Request, urlopen

from .domain import ValidationError, decimal_value, utc_now, validate_symbol


class QuoteUnavailable(ValidationError):
    """A readable snapshot cannot currently supply an executable book price."""


def session_notice(moment):
    local = moment.astimezone(timezone(timedelta(hours=8)))
    minute = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return "休市时段"
    if minute >= 15 * 60:
        return "已收盘"
    if minute < 9 * 60 + 15:
        return "尚未开盘"
    if 11 * 60 + 30 < minute < 13 * 60:
        return "午间休市"
    return ""


@dataclass(frozen=True)
class BookLevel:
    level: int
    price: Decimal | None
    quantity: int | None


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    last: Decimal | None = None
    bid_quantity: int | None = None
    ask_quantity: int | None = None
    session: str = ""
    source: str = "injected"
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    change_percent: Decimal | None = None
    time_kind: str = "quote_time"

    def price(self, side: str, *, now=None, max_age=5) -> str:
        if side not in {"buy", "sell"}:
            raise ValidationError("未知方向")
        if self.session:
            raise QuoteUnavailable(f"{self.session}，不能取得可执行的实时盘口；可自行选择手填限价，是否受理由券商返回；未发出委托")
        age = (now or utc_now()) - self.observed_at
        if age < timedelta(seconds=-1) or age > timedelta(seconds=max_age):
            raise QuoteUnavailable("盘口报价已过期或时间异常；请刷新盘口或自行选择手填限价，未改用旧价格")
        if self.bid <= 0 or self.ask <= 0 or self.bid > self.ask:
            raise QuoteUnavailable("盘口为空或买卖价格矛盾；请刷新盘口或自行选择手填限价")
        volume = self.ask_quantity if side == "buy" else self.bid_quantity
        if volume is not None and volume <= 0:
            raise QuoteUnavailable(f"{'卖一' if side == 'buy' else '买一'}无挂单数量，不能按该盘口定价；可自行选择手填限价")
        return str(self.ask if side == "buy" else self.bid)

    def describe(self, *, now=None):
        """Display a snapshot even when it is unsuitable for new orders."""
        reasons = []
        for side in ("buy", "sell"):
            try:
                self.price(side, now=now)
            except QuoteUnavailable as error:
                reason = str(error)
                if reason not in reasons:
                    reasons.append(reason)
        status = self.session or ("盘口不可用" if reasons else "实时盘口")
        last = f"最新成交{self.last}（仅供查看） / " if self.last is not None else ""
        def shown(levels, fallback):
            return "—（未识别）" if levels and levels[0].price is None else str(fallback)
        book = f"买一{shown(self.bids, self.bid)} / 卖一{shown(self.asks, self.ask)}"
        if self.bid_quantity is not None and self.ask_quantity is not None:
            def shown_quantity(levels, fallback):
                return "—（未识别）" if levels and levels[0].quantity is None else str(fallback)
            book += f"（盘口数量：买{shown_quantity(self.bids, self.bid_quantity)} / 卖{shown_quantity(self.asks, self.ask_quantity)}）"
        source = {"ths_client": "同花顺客户端", "tencent_web": "腾讯Web", "paper": "模拟", "injected": "测试"}.get(self.source, self.source)
        time_label = "客户端显示时间" if self.time_kind == "client_display_time" else "报价时间"
        change = f"涨幅{self.change_percent}% / " if self.change_percent is not None else ""
        return (f"来源：{source}；{status}；{last}{change}{book}；{time_label}{self.observed_at.astimezone(timezone(timedelta(hours=8))).strftime('%H:%M:%S')}"
                + ("\n" + "；".join(reasons) if reasons else ""))

    def payload(self):
        from dataclasses import asdict
        import json
        return json.loads(json.dumps(asdict(self), default=str))


def parse_tencent(text: str, symbol="159570.SZ") -> Quote:
    lines = [line for line in text.splitlines() if line.strip()]
    prefix = symbol[-2:].lower() + symbol[:6]
    if len(lines) != 1 or not lines[0].startswith(f'v_{prefix}="') or lines[0].count('"') != 2:
        raise ValidationError("报价响应标的或格式不匹配")
    fields = lines[0].split('"')[1].split("~")
    if len(fields) < 31 or fields[2] != symbol[:6]:
        raise ValidationError("行情代码不匹配或字段不完整")
    try:
        observed = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone(timedelta(hours=8)))
        bid, ask = decimal_value(fields[9], "买一", positive=False), decimal_value(fields[19], "卖一", positive=False)
        bid_quantity, ask_quantity = int(fields[10]), int(fields[20])
        if bid_quantity < 0 or ask_quantity < 0:
            raise ValueError("盘口数量不能为负数")
    except (ValueError, IndexError) as error:
        raise ValidationError(f"报价无有效盘口: {error}") from error
    last = decimal_value(fields[3], "最新成交", positive=False) if fields[3] else None
    def levels(offset):
        result = []
        for index in range(5):
            p, q = fields[offset + 2 * index:offset + 2 * index + 2]
            price = decimal_value(p, "盘口价格", positive=False) if p else None
            quantity = int(q) if q else None
            if quantity is not None and quantity < 0:
                raise ValidationError("盘口数量不能为负数")
            result.append(BookLevel(index + 1, price, quantity))
        return tuple(result)
    return Quote(symbol, bid, ask, observed, last, bid_quantity, ask_quantity, session_notice(observed),
                 source="tencent_web", bids=levels(9), asks=levels(19))


def fetch_quote(symbol="159570.SZ") -> Quote:
    validate_symbol(symbol)
    prefix = symbol[-2:].lower() + symbol[:6]
    request = Request("https://qt.gtimg.cn/q=" + prefix, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=5) as response:
        text = response.read(65536).decode("gbk", errors="strict")
    return parse_tencent(text, symbol)
