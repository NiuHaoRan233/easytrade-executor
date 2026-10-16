from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .domain import ValidationError


ALIASES = {
    "symbol": {"证券代码", "股票代码"},
    "name": {"证券名称", "股票名称"},
    "side": {"操作", "买卖标志"},
    "order_id": {"委托序号", "合同编号", "委托编号"},
    "quantity": {"委托数量"},
    "filled_quantity": {"成交数量"},
    "average_price": {"成交均价", "成交价格"},
    "limit_price": {"委托价格"},
    "status": {"状态", "备注"},
    "available_quantity": {"可用余额", "可卖数量"},
    "position_quantity": {"股票余额", "证券数量"},
    "current_price": {"市价", "当前价", "最新价"},
    "market_value": {"市值", "证券市值"},
    "trade_id": {"成交编号"},
}
REQUIRED = {"positions": {"symbol", "available_quantity"},
            "orders": {"symbol", "side", "order_id", "quantity", "filled_quantity", "status"},
            "trades": {"symbol", "side", "order_id", "filled_quantity", "average_price", "trade_id"}}
DISPLAY_FIELDS = {"current_price", "market_value"}


def parse_tsv(text: str) -> list[dict[str, str]]:
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("复制文本为空，不能解释成没有委托")
    lines = list(csv.reader(io.StringIO(text), delimiter="\t"))
    lines = [line for line in lines if any(x.strip() for x in line)]
    # A trailing tab is a formatting artifact. Other empty fields remain significant.
    lines = [line[:-1] if line and line[-1] == "" else line for line in lines]
    headers = lines[0]
    if any(not x.strip() for x in headers) or len(set(headers)) != len(headers):
        raise ValidationError("表头存在空列或重复列")
    if not any(x in headers for x in ALIASES["symbol"]):
        raise ValidationError("不是预期的证券表格")
    result = []
    for row in lines[1:]:
        if len(row) != len(headers):
            raise ValidationError("数据列数与表头不一致，不能静默跳过")
        result.append(dict(zip(headers, row)))
    return result


def normalize(row: dict[str, str], role: str) -> dict[str, str | int]:
    if role not in REQUIRED:
        raise ValidationError("未知表格类型")
    result = {}
    for field, aliases in ALIASES.items():
        if role == "positions" and field not in {"symbol", "name", "available_quantity", "position_quantity", "current_price", "market_value"}:
            continue
        # Remarks may be clipped while a separate status column is complete.
        selected_aliases = {"状态"} if field == "status" and "状态" in row else aliases
        matches = [row[name] for name in selected_aliases if name in row]
        if len(matches) > 1 and len(set(matches)) != 1:
            raise ValidationError(f"同义列内容冲突: {field}")
        if matches:
            result[field] = matches[0].strip()
    if not REQUIRED[role].issubset(result):
        raise ValidationError("表格缺少必需回执字段: " + ", ".join(sorted(REQUIRED[role] - set(result))))
    symbol = str(result["symbol"])
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValidationError("证券代码不完整；不能猜测或自动补零")
    if "side" in result:
        raw = str(result["side"])
        if raw in {"证券买入", "买入"}:
            result["side"] = "buy"
        elif raw in {"证券卖出", "卖出"}:
            result["side"] = "sell"
        else:
            raise ValidationError(f"不支持的委托类型: {raw}")
    for field in ("order_id", "trade_id"):
        if field in result:
            value = str(result[field])
            if not value or not value.isascii() or not value.isalnum():
                raise ValidationError(f"{field}不完整或含截断符号")
    for field in ("quantity", "filled_quantity", "available_quantity", "position_quantity"):
        if field in result:
            value = str(result[field])
            if not value.isascii() or not value.isdigit():
                raise ValidationError(f"{field}不是非负整数")
            result[field] = int(value)
    for field in ("limit_price", "average_price", "current_price", "market_value"):
        if field in result:
            try:
                value = Decimal(str(result[field]))
            except InvalidOperation as error:
                raise ValidationError(f"{field}无法解析") from error
            if not value.is_finite() or value < 0:
                raise ValidationError(f"{field}不是非负数")
            result[field] = str(value)
    if "status" in result:
        raw = str(result["status"])
        if "..." in raw or "…" in raw:
            raise ValidationError("状态列已截断，请扩展列宽")
        mapping = {"订单全部成": "FILLED", "订单全部成交": "FILLED", "全部成交": "FILLED", "已成": "FILLED",
                   "订单已撤销": "CANCELLED", "已撤": "CANCELLED", "废单": "REJECTED",
                   "已报": "BROKER_ACCEPTED", "待报": "BROKER_ACCEPTED", "未报": "BROKER_ACCEPTED",
                   "部分成交": "PARTIALLY_FILLED", "部成": "PARTIALLY_FILLED"}
        if raw not in mapping:
            raise ValidationError(f"未知状态文案，需要配置映射: {raw}")
        result["status"] = mapping[raw]
        if role == "orders":
            result["raw_status"] = raw  # broker queueing is not an exchange fill
        total, filled = result.get("quantity"), result.get("filled_quantity")
        if filled is not None and total is not None:
            if filled > total or (result["status"] == "FILLED" and filled != total):
                raise ValidationError("委托状态与累计成交数量矛盾")
    return result


@dataclass
class TableSnapshot:
    role: str
    rows: list[dict]
    observed_at: str
    source: str
    complete: bool = False
    incomplete_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    layout: dict = field(default_factory=dict)


class OcrTableReader:
    """Uses table grid lines, not fixed desktop coordinates. No mouse/keyboard actions."""

    def __init__(self, confidence: float = 0.85, *, unclip_ratio: float | None = None):
        if unclip_ratio is not None and (type(unclip_ratio) not in (int, float)
                or not math.isfinite(unclip_ratio) or not 1 <= unclip_ratio <= 2):
            raise ValidationError("ocr_unclip_ratio必须为1至2之间的有限数值")
        self.confidence = confidence
        self.unclip_ratio = unclip_ratio
        self._engine = None

    def _recognize(self, image):
        import numpy as np
        from rapidocr import RapidOCR
        if self._engine is None:
            # Tune detection-box expansion for the client's font/DPI. The
            # full-box cell boundaries and confidence checks below still apply.
            self._engine = (RapidOCR() if self.unclip_ratio is None else
                            RapidOCR(params={"Det.unclip_ratio": self.unclip_ratio}))
        return self._engine(np.asarray(image.convert("RGB")))

    def read_text(self, image) -> str:
        result = self._recognize(image)
        if result.txts is None or any(score < self.confidence for score in result.scores):
            raise ValidationError("身份标题未识别或置信度不足")
        return "".join(str(text).strip() for text in result.txts)

    @staticmethod
    def _lines(mask, axis):
        import numpy as np
        projection = (mask > 0).sum(axis=axis)
        threshold = mask.shape[axis] * 0.40
        locations = np.where(projection > threshold)[0].tolist()
        groups = []
        for value in locations:
            if not groups or value - groups[-1][-1] > 2:
                groups.append([value])
            else:
                groups[-1].append(value)
        return [round(sum(group) / len(group)) for group in groups]

    def read(self, image, role: str, observed_at: str) -> TableSnapshot:
        import bisect
        import cv2
        import numpy as np

        if role not in REQUIRED:
            raise ValidationError("未知表格类型")
        rgb = np.asarray(image.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        # Selected rows and blue headers are darker than white; a global threshold
        # would mistake the whole background for a grid line. Use local contrast.
        mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY_INV, 31, 5)
        vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, rgb.shape[0] // 4))))
        horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, rgb.shape[1] // 5), 1)))
        xs = sorted(set([0, *self._lines(vertical, 0), rgb.shape[1] - 1]))
        ys = sorted(set([0, *self._lines(horizontal, 1), rgb.shape[0] - 1]))
        xs = [x for i, x in enumerate(xs) if i == 0 or x - xs[i - 1] > 3]
        ys = [y for i, y in enumerate(ys) if i == 0 or y - ys[i - 1] > 3]
        if len(xs) < 3 or len(ys) < 3:
            raise ValidationError("没有检测到完整表格网格")
        result = self._recognize(image)
        if result.txts is None:
            raise ValidationError("OCR没有读到表头，不能解释成空表")
        cells, clipped_cells = {}, set()
        for text, score, box in zip(result.txts, result.scores, result.boxes):
            points = np.asarray(box)
            cx, cy = points.mean(axis=0)
            column, row = bisect.bisect_right(xs, cx) - 1, bisect.bisect_right(ys, cy) - 1
            if not (0 <= column < len(xs) - 1 and 0 <= row < len(ys) - 1):
                continue
            cells.setdefault((row, column), []).append((str(text).strip(), float(score)))
            # A center alone can assign clipped/merged text to the wrong column.
            # Require the full OCR box to stay in the detected cell and viewport.
            if (points[:, 0].min() < xs[column] - 2 or points[:, 0].max() > xs[column + 1] + 2
                    or points[:, 1].min() < ys[row] - 2 or points[:, 1].max() > ys[row + 1] + 2
                    or points[:, 0].min() <= 0 or points[:, 0].max() >= image.width - 1):
                clipped_cells.add((row, column))
        headers = {}
        warnings = []
        relevant = {"symbol", "name", "available_quantity", "position_quantity", "current_price", "market_value"} if role == "positions" else set(ALIASES)
        known_headers = set().union(*(ALIASES[field] for field in relevant))
        for column in range(len(xs) - 1):
            candidates = cells.get((0, column), [])
            # Sort arrows and unrelated display columns are not receipt data.
            # Required headers still fail closed when missing, clipped or uncertain.
            recognized = [text for text, score in candidates if text in known_headers]
            if not recognized:
                continue
            optional = role == "positions" and any(text in ALIASES[f] for text in recognized for f in DISPLAY_FIELDS)
            if optional and (len(candidates) != 1 or candidates[0][1] < self.confidence or (0, column) in clipped_cells):
                continue
            if len(candidates) > 1:
                raise ValidationError("表头跨列或包含多个识别片段")
            if (0, column) in clipped_cells:
                raise ValidationError("必需表头跨列或被截断；请展开窗口和列宽后重新读取")
            if candidates:
                text, score = candidates[0]
                if score < self.confidence:
                    raise ValidationError(f"OCR低置信度表头: {text}")
                headers[column] = text
        if len(set(headers.values())) != len(headers):
            raise ValidationError("OCR表头重复")
        present = {field for field, aliases in ALIASES.items() if aliases.intersection(headers.values())}
        if not REQUIRED[role].issubset(present):
            raise ValidationError("OCR缺少必需表头，可能尚未加载或列被隐藏")
        if role == "positions":
            for name, label in (("current_price", "市价"), ("market_value", "市值")):
                if name not in present:
                    warnings.append(f"未可靠识别{label}列，显示—；请展开客户端窗口及列宽，重新适配检查")
        rows = []
        for row in range(1, len(ys) - 1):
            tokens = [(text, score) for (cell_row, _), values in cells.items() if cell_row == row
                      for text, score in values]
            # This THS order grid has structural group rows above actual orders.
            # Only exact, isolated, high-confidence observed labels are skipped.
            if (role == "orders" and len(tokens) == 1 and tokens[0][1] >= self.confidence
                    and tokens[0][0] in {"当前没有可撤委托单", "已撤单", "全部成交"}):
                continue
            first_cell = cells.get((row, 0), [])
            symbol_columns = [column for column, header in headers.items() if header in ALIASES["symbol"]]
            symbol_texts = [text for column in symbol_columns for text, _ in cells.get((row, column), [])]
            if (len(first_cell) == 1 and first_cell[0][0] == "汇总" and first_cell[0][1] >= self.confidence
                    and all(text in {"", "汇总"} for text in symbol_texts)):
                continue
            raw, row_warnings = {}, []
            for column, header in headers.items():
                candidates = cells.get((row, column), [])
                optional = role == "positions" and any(header in ALIASES[f] for f in DISPLAY_FIELDS)
                if optional:
                    if (len(candidates) != 1 or candidates[0][1] < self.confidence or (row, column) in clipped_cells):
                        row_warnings.append(f"{header}缺失、低置信或跨列，显示—；请展开列宽")
                        continue
                    try:
                        number = Decimal(candidates[0][0])
                        if not number.is_finite() or number < 0:
                            raise InvalidOperation
                    except InvalidOperation:
                        row_warnings.append(f"{header}不能可靠解析，显示—；请重新读取")
                        continue
                if len(candidates) > 1:
                    raise ValidationError("单元格包含多个识别片段，拒绝猜测拼接")
                if (row, column) in clipped_cells:
                    raise ValidationError("持仓/委托字段跨列或被截断；请展开窗口和列宽后重新读取")
                if candidates and candidates[0][1] < self.confidence:
                    raise ValidationError(f"OCR低置信度字段: {candidates[0][0]}")
                raw[header] = candidates[0][0] if candidates else ""
            if not any(raw.values()) or "汇总" in raw.values():
                continue
            # Only grade/normalize columns needed for this role. Unrelated clipped columns
            # are retained in raw but do not participate in trade identifiers or status.
            normalized = normalize(raw, role)
            if role == "positions" and {"position_quantity", "current_price", "market_value"}.issubset(normalized):
                price, value = Decimal(normalized["current_price"]), Decimal(normalized["market_value"])
                quantity = normalized["position_quantity"]
                # Account for rounding of both displayed numbers. This catches
                # lost decimal points without fabricating a replacement price.
                tolerance = (Decimal(1).scaleb(price.as_tuple().exponent) * quantity
                             + Decimal(1).scaleb(value.as_tuple().exponent)) / 2 + Decimal("0.01")
                if abs(price * quantity - value) > tolerance:
                    normalized.pop("current_price")
                    normalized.pop("market_value")
                    row_warnings.append("市价×总持仓与市值不一致，市价/市值显示—；请核对客户端列宽与小数点")
            if row_warnings:
                normalized["read_warnings"] = row_warnings
                warnings.extend(f"{normalized['symbol']}：{message}" for message in row_warnings)
            rows.append(normalized)
        return TableSnapshot(role, rows, observed_at, "ocr", complete=False, warnings=list(dict.fromkeys(warnings)),
                             layout={"image_size": [image.width, image.height], "headers": list(headers.values())})
