from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any


class ValidationError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_symbol(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}(?:\.(SH|SZ|BJ))?", symbol):
        raise ValidationError("symbol必须为六位证券代码，可附.SH/.SZ/.BJ市场后缀")
    return symbol


def decimal_value(value: Any, field: str, *, positive: bool = True) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValidationError(f"{field}必须是十进制字符串或整数，不能使用浮点数")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValidationError(f"{field}不是合法金额") from error
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise ValidationError(f"{field}必须是{'正数' if positive else '非负数'}")
    if len(str(value)) > 40 or (result and not -12 <= result.adjusted() <= 12):
        raise ValidationError(f"{field}超出支持的十进制范围")
    return result


def timestamp(value: Any, field: str = "expires_at") -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field}必须是带时区的ISO时间") from error
    if result.tzinfo is None:
        raise ValidationError(f"{field}不能省略时区")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class Signal:
    signal_id: str
    account: str
    symbol: str
    side: str
    limit_price: str
    expires_at: str
    quantity: int | None = None
    budget: str | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Signal":
        if not isinstance(data, dict):
            raise ValidationError("信号必须是JSON对象")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValidationError(f"未知字段: {', '.join(sorted(unknown))}")
        for field in ("signal_id", "account", "symbol", "side", "limit_price", "expires_at"):
            if field not in data:
                raise ValidationError(f"缺少字段: {field}")
        if not isinstance(data["signal_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", data["signal_id"]):
            raise ValidationError("signal_id必须是1至128位字母、数字或_.:-")
        if not isinstance(data["account"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data["account"]):
            raise ValidationError("account必须是已配置的账户别名")
        validate_symbol(data["symbol"])
        if data["side"] not in ("buy", "sell"):
            raise ValidationError("side必须是buy或sell")
        price = decimal_value(data["limit_price"], "limit_price")
        expires = timestamp(data["expires_at"])
        quantity, budget = data.get("quantity"), data.get("budget")
        if (quantity is None) == (budget is None):
            raise ValidationError("quantity和budget必须且只能提供一个")
        if quantity is not None and (type(quantity) is not int or quantity <= 0):
            raise ValidationError("quantity必须是正整数")
        if budget is not None:
            if data["side"] != "buy":
                raise ValidationError("卖出信号必须指定quantity")
            budget = format(decimal_value(budget, "budget").normalize(), "f")
        return cls(data["signal_id"], data["account"], data["symbol"], data["side"], format(price.normalize(), "f"), expires.isoformat(), quantity, budget)

    def payload(self) -> dict:
        return asdict(self)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Instrument:
    name: str
    lot_size: int
    price_tick: Decimal

    @classmethod
    def parse(cls, value: dict) -> "Instrument":
        if type(value.get("lot_size")) is not int or value["lot_size"] <= 0:
            raise ValidationError("lot_size必须是正整数")
        if not value.get("name"):
            raise ValidationError("品种必须配置name，供客户端名称核对")
        return cls(str(value["name"]), value["lot_size"], decimal_value(value["price_tick"], "price_tick"))


@dataclass(frozen=True)
class OrderPlan:
    signal_id: str
    account: str
    symbol: str
    side: str
    quantity: int
    limit_price: str
    notional: str
    fee_reserve: str
    budget: str | None

    def payload(self) -> dict:
        return asdict(self)


def build_plan(signal: Signal, instrument: Instrument, fee_reserve: Decimal) -> OrderPlan:
    price = Decimal(signal.limit_price)
    if price % instrument.price_tick:
        raise ValidationError("限价不符合该品种配置的价格最小单位")
    if signal.budget is not None:
        spendable = Decimal(signal.budget) - fee_reserve
        lots = (spendable / (price * instrument.lot_size)).to_integral_value(rounding=ROUND_FLOOR)
        quantity = int(lots) * instrument.lot_size
        if quantity <= 0:
            raise ValidationError("预算扣除费用预留后不足一个配置交易单位")
    else:
        quantity = signal.quantity
    assert quantity is not None
    if quantity % instrument.lot_size:
        raise ValidationError("数量不符合配置交易单位；零股卖出需另行配置规则")
    return OrderPlan(signal.signal_id, signal.account, signal.symbol, signal.side, quantity,
                     signal.limit_price, str(price * quantity), str(fee_reserve), signal.budget)


TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "PREPARATION_FAILED"}
BROKER_STATUSES = {"BROKER_ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"}


def validate_receipt(order: dict, data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValidationError("回执必须是JSON对象")
    plan = order.get("plan")
    if not plan:
        raise ValidationError("订单没有已核对的委托计划")
    required = {"receipt_id", "account", "symbol", "side", "broker_order_id", "status", "filled_quantity", "average_price", "observed_at", "source"}
    if set(data) != required:
        raise ValidationError("回执字段必须完整且无未知字段: " + ", ".join(sorted(required)))
    for field in ("account", "symbol", "side"):
        if data[field] != plan[field]:
            raise ValidationError(f"回执{field}与订单不匹配")
    for field in ("receipt_id", "broker_order_id"):
        if not isinstance(data[field], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", data[field]):
            raise ValidationError(f"{field}必须是非空字符串标识")
    if data["status"] not in BROKER_STATUSES:
        raise ValidationError("回执状态不受支持")
    if data["source"] not in {"paper", "manual", "ocr", "clipboard", "win32"}:
        raise ValidationError("回执source不受支持")
    if order["mode"] == "paper" and data["source"] != "paper":
        raise ValidationError("模拟订单不能接受真实回执")
    if order["mode"] != "paper" and data["source"] == "paper":
        raise ValidationError("真实客户端订单不能接受模拟回执")
    quantity = data["filled_quantity"]
    if type(quantity) is not int or not 0 <= quantity <= plan["quantity"]:
        raise ValidationError("累计成交数量超出订单范围")
    average = decimal_value(data["average_price"], "average_price", positive=False)
    if (quantity == 0) != (average == 0):
        raise ValidationError("成交数量与成交均价不一致")
    if data["status"] == "FILLED" and quantity != plan["quantity"]:
        raise ValidationError("全部成交状态必须达到委托数量")
    if data["status"] == "PARTIALLY_FILLED" and not 0 < quantity < plan["quantity"]:
        raise ValidationError("部分成交状态必须有未成交剩余数量")
    if data["status"] == "CANCELLED" and quantity == plan["quantity"]:
        raise ValidationError("已全部成交的数量不能标记撤销")
    if data["status"] in {"BROKER_ACCEPTED", "REJECTED"} and quantity:
        raise ValidationError("受理/拒单回执不能包含成交")
    if quantity:
        limit = Decimal(plan["limit_price"])
        if (plan["side"] == "buy" and average > limit) or (plan["side"] == "sell" and average < limit):
            raise ValidationError("成交均价违背限价，需要重新核对回执")
    observed = timestamp(data["observed_at"], "observed_at")
    if observed > utc_now() + timedelta(minutes=1):
        raise ValidationError("回执时间超过允许的时钟偏差")
    return {**data, "average_price": str(average), "observed_at": observed.isoformat()}
