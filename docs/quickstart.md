# 安装、配置与首次运行

## 安装与模拟

Windows 10/11，推荐 64 位 Python 3.12（最低 3.11），安装 pip 和 Tcl/Tk。解压到可写目录，双击 `Setup.cmd`，创建 `.venv` 并安装本项目、pywinauto、pywin32、RapidOCR、ONNX Runtime、Pillow 和 OpenCV。首次安装需要联网；首次 OCR 初始化可能下载模型并慢于后续读取。OCR 在本机运行，无需远程大模型。

手动安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[windows,ocr]"
```

双击 `Start-Test-Console.cmd`，默认模拟账户 `demo`，初始资金100000。输入模型传入的代码、数量、明确限价，分别测试买卖与查询；没有预置证券名单，也不自动生成模拟盘口。

模拟账本会持久化，修改初始资金不覆盖旧账本。另起环境可把分享包解到新目录，或备份后移走 `runtime/console-paper.sqlite` 再启动；不要删除整个实盘运行目录。

## 本机实盘配置

优先使用 `Check-Environment.cmd`检查环境，自行登录交易端后运行 `Configure-Client.cmd`。它只读发现实际程序路径和常见账户区域，询问身份片段并生成本机配置，不覆盖已有文件。随后仍需只读验收。发现不了窗口或身份区域时按下表人工配置，详见[新电脑快速适配指南](first-machine.md)。

复制 `config/ths.example.json` 为 `config/ths.local.json`，修改：

| 配置 | 要求 |
| --- | --- |
| `ths_profile.exe_path` | 实际交易程序完整路径，JSON 反斜杠写成 `\\`，也可使用 `/` |
| `ths_profile.account_alias` | 逻辑别名，与 `accounts` 中的键一致，示例 `broker` |
| `ths_profile.title_regex` | 匹配实际主窗口，同屏只能匹配一个可见窗口 |
| `ths_profile.title_contains` | 实际标题中的券商与账户身份片段，替换全部占位文本 |
| `caption_identity_box` | 标题身份截图区域；标题/DPI 变化需校准，不要删除身份要求绕过核对 |
| `ths_profile.pages` | 左侧页面树路径，与实际客户端文字一致 |
| `tree_control_id`、`grid_control_id`、`grid_class` | 原生控件标识，其他布局需适配 |
| `instruments` | 兼容字段，可留空；统一实盘接口不以它限制证券，无需逐只配置 |
| `quote_source` | 默认 `ths_client`，主动选择 Web 为 `tencent_web` |
| `pretrade_account` | 默认 `false`，明确指令直接执行；`true` 可按需启用委托前账户读取 |
| `buy_fee_reserve` | 买入预算的费用预留，不是券商真实费率模型 |

证券名称由客户端识别，显式数量和限价直接传递，实际申报单位及价格单位由券商核验。代码支持六位数字及附.SH/.SZ/.BJ的格式；旧版市场选择框和Web行情需要市场后缀。不要把持仓市价自动当作委托限价。

用户先打开并登录同花顺交易客户端。两者使用相同 Windows 权限级别，桌面解锁、窗口可见、DPI 与布局稳定。执行时不要切换前台或干预键鼠。

## 首次实盘只读核对

1. 测试台切换实盘，核对账户别名。
   先调整交易客户端，使关键列完整可见，点击“客户端适配检查”；按[窗口适配流程](client-adaptation.md)查看诊断与逐项对照。
2. 刷新账户，人工逐项对照资金、每只证券总数与可用数量，确认完整性。
3. 刷新券商委托，核对完整编号、方向、数量、状态。
4. 选择证券，刷新客户端行情，核对市场、最新价和五档。
5. 核对完成后，由使用者自行决定小单测试；遵守实际券商与交易所规则。

切换实盘不自动发单。通用流程经过模拟和假客户端测试；其他券商、DPI、多页账户、新标的实盘买卖、撤单和恢复确认都需各自现场验收。

核对后保持客户端大小、列宽及显示缩放。连接时保留正常/最大化窗口状态；移动到不同缩放的显示器、修改窗口或列宽、升级客户端后重新检查。

## 模型服务与目录

`Start-Paper-Service.cmd` 启动模拟 HTTP；`Start-Live-Service.cmd` 启动实盘，需要本机配置。模型与服务在同一电脑，默认只监听回环地址。服务和测试台共用各模式账本，建议实盘择一操作。不要通过不同根目录/锁文件同时控制同一个客户端。

从其他目录启动指定数据根目录：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m easytrade.launcher --root C:/Projects/easytrade --mode paper --port 8765
```

首次服务生成 `runtime/api-token.txt`；可用环境变量 `EASYTRADE_TOKEN` 覆盖，至少 24 字符。不要分享运行目录。

## 排错

安装失败检查 Python 版本/位数、网络与 pip 错误；缺少 tkinter 时在安装器补装 Tcl/Tk。导入依赖失败时在同一虚拟环境重跑 Setup。

窗口未找到检查可见窗口、exe 与标题正则；身份失败核对标题片段、截图区域和 DPI。不要放宽身份要求强行下单。诊断保存在本机 `runtime/`，提供给维护者前先脱敏。

端口占用先关闭另一服务，或使用 `--port` 并同步模型 URL。HTTP 超时先查原信号与券商委托；重试复用原始内容与编号，不换编号补发。
