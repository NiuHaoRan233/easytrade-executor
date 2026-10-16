# 模型接入协议

模型决定证券、方向、数量/预算、限价和时效；测试台与 HTTP 共用 `UserSession`。执行端没有选股、仓位比例、自动往返或追价策略。

## 启动与鉴权

用 `Start-Paper-Service.cmd` 或 `Start-Live-Service.cmd` 启动。默认 `http://127.0.0.1:8765`，每个请求带 `Authorization: Bearer <token>`，JSON 为 UTF-8。凭据来自环境变量 `EASYTRADE_TOKEN` 或 `runtime/api-token.txt`。

`GET /health` 只表明进程运行；`client_ready: null` 不表示客户端已登录。实盘响应 `mode` 为内部账本标识 `ths-user`，对应启动模式 `ths-live`；模拟为 `paper`。

## 信号

向 `POST /signals` 发送：

```json
{
  "signal_id": "strategyA-20261016-0001",
  "account": "broker",
  "symbol": "000001.SZ",
  "side": "buy",
  "quantity": 100,
  "limit_price": "10.01",
  "expires_at": "2026-10-16T06:59:00+00:00"
}
```

时间仅示意，实际须生成未来且带时区的时间。新交易意图用新编号，重试复用完整原始信号，包括原到期时间。

| 字段 | 规则 |
| --- | --- |
| `signal_id` | 1–128 位字母、数字或 `_.:-`，同账本内唯一 |
| `account` | 当前会话逻辑别名，模拟例子是 `demo` |
| `symbol` | 六位 ASCII 代码加 `.SH/.SZ/.BJ`，必须已配置 |
| `side` | `buy` 或 `sell` |
| `quantity` | 正整数，符合配置交易单位 |
| `budget` | 仅买入可用，十进制字符串，与 quantity 二选一 |
| `limit_price` | 正十进制字符串或整数，禁止 JSON 浮点数，符合 price_tick |
| `expires_at` | 带时区 ISO 时间，过期后不发送新委托 |

预算数量按 `(budget - buy_fee_reserve) / limit_price` 向下取整到交易单位；不会改变明确给出的数量。实盘费用以券商为准；模拟把该预留当固定买入费用扣除，默认模拟模板为 0。

每个新实盘请求默认先新读账户，失败停止；不会用不完整 OCR 仓位推导额外交易策略。券商最终校验资金、可卖数量和交易时段。信号送出后，到期不会自动撤单。

## 接口

| 方法与路径 | 行为 |
| --- | --- |
| `GET /health` | 模式、账户、是否支持真实提交；不核对客户端 |
| `GET /instruments` | 配置证券与规则 |
| `POST /signals` | 一次限价委托，返回本地记录，不无限等待成交 |
| `GET /account` | 新读资金、全部可读取持仓、时间和完整性 |
| `GET /quotes/{symbol}` | 新读所选来源报价，可能切换客户端证券准备区 |
| `GET /broker-orders` | 新读券商当日委托并同步可匹配的本地记录 |
| `GET /orders` | 当前账户本地执行记录 |
| `GET /orders/{signal_id}` | 本地持久化状态，不访问券商 |
| `POST /orders/{signal_id}/query` | `{}` 查询回执；或 `{"broker_order_id":"完整编号"}` |
| `POST /orders/{signal_id}/cancel` | `{}`，核对后撤指定活动委托，实际交易操作 |
| `GET /events?after=0` | 未确认的本地状态事件，可按编号过滤 |
| `POST /events/ack` | `{"event_ids":[1,2]}` 确认消费，不执行交易 |

URI 中编号须 URL 编码。当前每个服务/账本对应一个配置账户，事件也是账本级事件；不要共用数据库配置多个账户。统一接口不接受外部上传成交回执。旧 `ths-draft` 仅为历史草稿兼容，不用于模型实盘执行。

账户重点字段：`balances.available_cash`、`positions`、`positions_complete`、`positions_scope`、`observed_at`。实盘持仓代码通常为六位原始代码，模型应结合实际市场映射，不要假设都有后缀。委托快照有 `rows`、`complete`、`incomplete_reason`；不完整不代表空账户。

## 状态与超时

- `BROKER_ACCEPTED`：已受理，不等于成交。
- `PARTIALLY_FILLED` / `FILLED`：部分/全部成交，核对 filled_quantity 与 average_price。
- `REJECTED` / `CANCELLED` / `EXPIRED` / `PREPARATION_FAILED`：拒单、撤销、过期、准备失败，原因见 detail。撤单可能仍含已成交数量。
- `UNKNOWN`：提交或回执不确定，查询券商，不自动重发。

同编号同内容重发返回原记录，不重复提交；同编号改参数或到期时间返回 HTTP 409。不能为了绕过 UNKNOWN 或网络超时换新编号。

400 为参数/查询校验失败，401 鉴权失败，404 记录不存在，409 信号冲突。500、连接中断与超时均不能推断未下单；先查原编号，再查券商。SDK 默认超时 60 秒，以 `DeliveryUncertain` 表示响应不确定。

没有交易所推送，模型自行轮询账户/回执。原生操作串行，过密查询会拖延下一指令；执行期间不要手动操作客户端。

## Python SDK

```python
from datetime import timedelta
from pathlib import Path
from easytrade.client import Client, DeliveryUncertain
from easytrade.domain import utc_now

client = Client(Path("runtime/api-token.txt").read_text().strip())
print(client.account())  # 每次新读
payload = {
    "signal_id": "my-model-buy-0001", "account": "demo",
    "symbol": "000001.SZ", "side": "buy", "quantity": 100,
    "limit_price": "10.01",
    "expires_at": (utc_now() + timedelta(minutes=2)).isoformat(),
}
# 发送前持久化 payload，重启后继续复用完整内容。
try:
    order = client.signal(payload)
except DeliveryUncertain:
    order = client.order(payload["signal_id"])
print(order)
# 已受理后按需 client.query(payload["signal_id"]) 查券商回执。
# 卖出使用新的明确指令，side="sell"、quantity 和新的编号。
```

若查询也失败或 404，先确认服务恢复与券商状态；重试仍用原 payload。`examples/model_client.py` 默认只查询，加 `--submit` 才发送，原子保存信号到 `runtime/model-outbox/`，并发重试也复用相同到期时间。
