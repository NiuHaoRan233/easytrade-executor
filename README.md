# Easytrade 本地量化执行端

把模型的买卖信号交给已登录的同花顺交易客户端执行，并读取资金、持仓与当日委托。模型决定买什么、卖什么、数量与限价；执行端负责执行、记录和反馈。

当前版本：**0.2.0**，测试台构建号 **20261016.10**。

## 功能

- 明确的买入、卖出指令独立执行，历史待成交记录不会阻止新指令。
- 查询资金、持仓、券商当日委托及指定订单的成交状态。
- 配置证券、交易单位、价格最小单位；保留通用桌面测试台。
- 默认读取同花顺交易控件右侧行情；腾讯 Web 为手动选择的另一条路径。
- 固定信号编号去重，不确定结果可查询，不会自动补单、追价或反向交易。

这是 Windows 桌面自动化执行端，通过原生控件和本地 OCR 读取客户端，**没有使用同花顺官方交易 API**。不同券商版本、控件布局、DPI 和证券交易规则需要分别配置与验收。

## 开始使用

1. Windows 10/11 安装 **Python 3.11+**，保留 Tcl/Tk；推荐 64 位 Python 3.12。
2. 下载项目到可写目录，双击 `Setup.cmd` 安装。
3. 双击 `Start-Test-Console.cmd`，先在默认模拟模式选择证券，分别测试买入、卖出、刷新持仓和委托。
4. 实盘前复制 `config/ths.example.json` 为 `config/ths.local.json`，填写本机路径与账户身份，按[安装与实盘配置](docs/quickstart.md)完成核对。

首次安装依赖及首次 OCR 初始化可能需要联网下载。模拟运行不要求交易客户端在线；模拟账户创建后保存在本地账本中。

## 模型接入

双击 `Start-Paper-Service.cmd` 启动模拟 HTTP 服务，另开终端运行只读示例：

```powershell
.\.venv\Scripts\python.exe -X utf8 examples/model_client.py
```

明确发送模拟买入：

```powershell
.\.venv\Scripts\python.exe -X utf8 examples/model_client.py --submit --signal-id demo-buy-001 --symbol 000001.SZ --side buy --quantity 100 --price 10.01
```

换成 `Start-Live-Service.cmd` 后，同样操作会执行真实委托。默认服务地址 `127.0.0.1:8765`，首次启动生成 `runtime/api-token.txt`。连接同一客户端时，测试台与服务应择一操作。

## 文档与目录

| 路径 | 内容 |
| --- | --- |
| [安装与实盘配置](docs/quickstart.md) | 从零安装、配置、只读验收、排错 |
| [测试台操作](docs/local-test-console.md) | 选择证券、报价、买卖、账户与委托 |
| [模型接入](docs/model-integration.md) | HTTP、Python SDK、状态与重试 |
| [实现与适配范围](docs/architecture.md) | 数据路径、性能与验证边界 |
| `src/easytrade/` | 执行端、适配器与测试台 |
| `config/*.example.json` | 可分享的配置模板 |
| `examples/model_client.py` | 持久化信号与原编号重试示例 |
| `tests/`、`tools/console_smoke.py` | 离线测试与模拟 GUI 验证 |
| `runtime/` | 本机账本、凭据、诊断与快照，运行时创建 |

## 开发验证与打包

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 tools/console_smoke.py
.\.venv\Scripts\python.exe -X utf8 tools/build_share.py
```

分享包输出到 `dist/`，带逐文件 SHA-256 清单。打包和 Git 跟踪使用相同公开文件清单，排除本机配置、账本、历史报告、截图、日志与凭据。给同事分享此包或仓库源码，每人在自己的机器填写配置。
