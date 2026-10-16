"""Parse the observed THS Custom1 quote panel. No UI or network calls here."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .domain import ValidationError, decimal_value
from .quotes import BookLevel, Quote, session_notice


def parse_panel(result, width, height, symbol, captured_at, *, confidence=0.85):
    tokens = []
    if result.txts is None:
        raise ValidationError("客户端报价区未识别到文字")
    for text, score, box in zip(result.txts, result.scores, result.boxes):
        x = sum(float(p[0]) for p in box) / len(box)
        y = sum(float(p[1]) for p in box) / len(box)
        tokens.append((str(text).strip(), float(score), x, y))

    def unique(label):
        matches = [t for t in tokens if t[0] == label and t[1] >= confidence]
        if len(matches) != 1:
            raise ValidationError(f"客户端报价区未找到唯一高置信字段：{label}")
        return matches[0]

    latest, change = unique("最新"), unique("涨幅")
    sell, buy = unique("卖1"), unique("买1")
    if not (latest[3] < sell[3] < buy[3] and latest[2] < change[2] < width * .85):
        raise ValidationError("客户端报价区布局与已验证结构不一致")
    tolerance = max(3, (buy[3] - sell[3]) * .28)

    def row_values(label, left, right, *, strict=True):
        values = [t for t in tokens if abs(t[3] - label[3]) <= tolerance and left < t[2] < right]
        if len(values) > 1 or any(t[1] < confidence for t in values):
            if strict:
                raise ValidationError("客户端报价单元格存在低置信或多个片段")
            return None
        return values[0][0] if values else None

    raw_last = row_values(latest, width * .2, change[2] - width * .04)
    if raw_last is None:
        raise ValidationError("客户端最新价未读取，不能沿用旧价格")
    last = decimal_value(raw_last, "客户端最新价", positive=False)
    raw_change = row_values(change, change[2] + width * .05, width)
    percent = None
    if raw_change is not None:
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", raw_change):
            raise ValidationError("客户端涨幅格式不完整")
        percent = Decimal(raw_change[:-1])

    clocks = [t for t in tokens if re.fullmatch(r"\d{2}:\d{2}:\d{2}", t[0])
              and t[1] >= confidence and t[3] < latest[3]]
    if len(clocks) != 1:
        raise ValidationError("客户端显示时间未读取；不把截图时间冒充行情时间")
    zone = timezone(timedelta(hours=8))
    day = captured_at.astimezone(zone).date()
    observed = datetime.combine(day, datetime.strptime(clocks[0][0], "%H:%M:%S").time(), zone)

    def levels(side):
        levels = []
        for index in range(1, 6):
            labels = [t for t in tokens if t[0] == f"{side}{index}" and t[1] >= confidence and t[2] < width * .2]
            price, quantity = None, None
            if len(labels) == 1:
                label = labels[0]
                # Empty/missing OCR cells remain unknown, never fabricated as 0.
                raw_price = row_values(label, width * .3, width * .67, strict=False)
                raw_quantity = row_values(label, width * .72, width, strict=False)
                if raw_price not in (None, "-", "—"):
                    raw_price = raw_price.rstrip("◀▶▲▼")
                    price = decimal_value(raw_price, "客户端盘口价格", positive=False)
                if raw_quantity not in (None, "-", "—"):
                    if not raw_quantity.isascii() or not raw_quantity.isdigit():
                        raise ValidationError("客户端盘口数量不是完整整数")
                    quantity = int(raw_quantity)
            levels.append(BookLevel(index, price, quantity))
        return tuple(levels)

    # Sparse dash glyphs can be missed by OCR after close. Preserve unknowns
    # in the displayed five levels; execution still requires a verified top row.
    bids, asks = levels("买"), levels("卖")
    return Quote(symbol, bids[0].price or Decimal("0"), asks[0].price or Decimal("0"), observed,
                 last=last, bid_quantity=bids[0].quantity or 0, ask_quantity=asks[0].quantity or 0,
                 session=session_notice(captured_at), source="ths_client", bids=bids, asks=asks,
                 change_percent=percent, time_kind="client_display_time", captured_at=captured_at)
