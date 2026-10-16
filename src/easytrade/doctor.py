"""First-machine checks and local configuration. Never submits or confirms orders."""
from __future__ import annotations

import argparse
import copy
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import struct
import sys
import time


def environment_report(root, *, live=True):
    """Default report omits paths, account contents, titles and credentials."""
    checks = []
    def add(name, status, detail):
        checks.append({"item": name, "status": status, "detail": detail})
    add("Python", "ok" if sys.version_info >= (3, 11) else "error", platform.python_version())
    add("Python architecture", "ok" if struct.calcsize("P") == 8 else "warning",
        f"{struct.calcsize('P') * 8}-bit; 64-bit Python is the tested setup")
    add("Windows", "ok" if os.name == "nt" else ("error" if live else "warning"), platform.system())
    try:
        import tkinter
        add("Tcl/Tk", "ok", str(tkinter.Tcl().eval("info patchlevel")))
    except Exception as error:
        add("Tcl/Tk", "error", f"Initialization failed: {type(error).__name__}; install Python with Tcl/Tk")
    packages = ["pywinauto", "pywin32", "rapidocr", "onnxruntime", "Pillow", "opencv-python-headless", "numpy"] if live else []
    installed = set()
    for package in packages:
        try:
            add(package, "ok", importlib.metadata.version(package))
            installed.add(package)
        except importlib.metadata.PackageNotFoundError:
            add(package, "error", "Missing; run Setup.cmd in this project")
    for package, module in (("pywinauto","pywinauto"), ("pywin32","win32gui"), ("onnxruntime","onnxruntime"),
                            ("opencv-python-headless","cv2"), ("rapidocr","rapidocr")):
        if package in installed:
            try:
                importlib.import_module(module)
                add(f"{module} import", "ok", "Runtime import succeeded")
            except Exception as error:
                add(f"{module} import", "error", f"Runtime import failed: {type(error).__name__}; check the Python/environment and binary runtime installation")
    if live:
        path = Path(root) / "config/ths.local.json"
        if not path.is_file():
            add("Local config", "warning", "Not created; run Configure-Client.cmd after opening the logged-in client")
        else:
            try:
                profile = json.loads(path.read_text(encoding="utf-8-sig"))["ths_profile"]
                fragments = profile.get("title_contains", [])
                placeholders = (not fragments or any(not part or "请填写" in part or "请替换" in part for part in fragments))
                add("Account identity config", "warning" if placeholders else "ok",
                    "Fill broker/account identity markers" if placeholders else "Present; accuracy still needs local verification")
                add("Client executable", "ok" if Path(profile.get("exe_path", "")).is_file() else "warning",
                    "Path exists" if Path(profile.get("exe_path", "")).is_file() else "Path missing; configure this computer's trading executable")
            except (ValueError, KeyError, TypeError, OSError):
                add("Local config", "error", "Invalid JSON or missing ths_profile; original file was not modified")
    return {"ok": not any(c["status"] == "error" for c in checks), "checks": checks,
            "client_verified": False, "contains_account_data": False}


def discover_clients():
    """Read window metadata only. Returned values are PRIVATE local diagnostics."""
    if os.name != "nt":
        raise ValueError("Client discovery requires Windows")
    import ctypes
    import win32gui
    import win32process
    from pywinauto.application import process_module
    candidates = []
    def inspect(handle, _):
        if not win32gui.IsWindowVisible(handle):
            return
        title = win32gui.GetWindowText(handle)
        if not title:
            return
        try:
            pid = win32process.GetWindowThreadProcessId(handle)[1]
            # Filter by title before opening process metadata for unrelated apps.
            if not re.match(r"^网上股票交易系统", title):
                return
            exe_path = str(process_module(pid))
            controls = []
            def child(child_handle, _):
                if win32gui.IsWindowVisible(child_handle):
                    controls.append((child_handle, win32gui.GetDlgCtrlID(child_handle), win32gui.GetClassName(child_handle)))
            win32gui.EnumChildWindows(handle, child, None)
            trees = [c for c in controls if c[2] == "SysTreeView32"]
            if len(trees) != 1:
                return
            left, top, right, bottom = win32gui.GetWindowRect(handle)
            boxes = [c for c in controls if c[1] == 2322 and c[2] == "ComboBox"]
            hint, box = "", None
            if len(boxes) == 1:
                hint = win32gui.GetWindowText(boxes[0][0]).strip()
                l, t, r, b = win32gui.GetWindowRect(boxes[0][0])
                box = [max(0, l-left-10), max(0, t-top-8), min(right-left, r-left+12), min(bottom-top, b-top+8)]
            try:
                dpi = ctypes.windll.user32.GetDpiForWindow(handle) or None
            except (AttributeError, OSError):
                dpi = None
            candidates.append({"window_id": handle, "pid": pid, "title": title, "exe_path": exe_path,
                               "window_size": [right-left, bottom-top], "dpi": dpi,
                               "enabled": bool(win32gui.IsWindowEnabled(handle)),
                               "tree_control_id": trees[0][1],
                               "grid_control_ids": sorted({c[1] for c in controls if c[2] == "CVirtualGridCtrl"}),
                               "identity_hint": hint, "account_control_box": box})
        except Exception:
            # Inaccessible or disappearing windows are not auto-selected.
            return
    win32gui.EnumWindows(inspect, None)
    return candidates


def choose_client(candidates, window_id=None):
    matches = candidates if window_id is None else [c for c in candidates if c["window_id"] == window_id]
    if len(matches) != 1:
        raise ValueError("需要唯一交易窗口；先用 --discover 查看，再用 --window-id 指定。未选择或操作任何窗口。")
    if not matches[0]["enabled"]:
        raise ValueError("交易主窗口被弹窗阻塞，请自行处理登录/确认提示后再生成配置")
    return matches[0]


def build_config(template, candidate, broker, account_marker, alias="broker"):
    """Pure config generation; no account identity requirement can be omitted."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias):
        raise ValueError("账户别名只接受1至64位字母、数字、下划线或连字符")
    broker, account_marker = broker.strip(), account_marker.strip()
    if (not broker or not account_marker or broker == account_marker
            or any("请填写" in part or "请替换" in part for part in (broker, account_marker))):
        raise ValueError("必须分别填写真实券商标识和当前账户标识，不能留空或使用同一片段")
    fragments = [broker, account_marker]
    box = candidate.get("account_control_box")
    if not all(part in candidate["title"] for part in fragments) and box is None:
        raise ValueError("无法自动定位账户标识区域；请按 docs/client-adaptation.md 人工配置 caption_identity_box，不能省略身份核对")
    if box is not None:
        width, height = candidate["window_size"]
        if (len(box) != 4 or any(type(n) is not int for n in box)
                or not 0 <= box[0] < box[2] <= width or not 0 <= box[1] < box[3] <= height):
            raise ValueError("自动识别的账户截图区域无效，未生成配置")
    config = copy.deepcopy(template)
    config.pop("default_symbol", None)
    config.update(accounts={alias: {}}, instruments={})
    profile = config["ths_profile"]
    stable_title = re.match(r"^网上股票交易系统[0-9.]+", candidate["title"])
    profile.update(account_alias=alias, exe_path=candidate["exe_path"], title_contains=fragments,
                   title_regex="^" + re.escape(stable_title[0] if stable_title else candidate["title"]) + (".*" if stable_title else "$"),
                   tree_control_id=candidate["tree_control_id"])
    if box is not None:
        profile["caption_identity_box"] = box
    if len(candidate["grid_control_ids"]) == 1:
        profile["grid_control_id"] = candidate["grid_control_ids"][0]
    return config


def write_local_config(root, config):
    path = Path(root) / "config/ths.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: re-running the wizard never replaces a working config.
    with path.open("x", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return path


def warm_ocr():
    from PIL import Image
    from .tables import OcrTableReader
    started = time.perf_counter()
    OcrTableReader()._recognize(Image.new("RGB", (64, 64), "white"))
    return {"ok": True, "elapsed_ms": round((time.perf_counter()-started)*1000, 1),
            "detail": "OCR model load checked using a blank image; no client screenshot or input"}


def check_client(root):
    from .configuration import load_config
    from .user_session import UserSession
    report = UserSession(root, load_config(root, live=True), live=True).check_client()
    return {"read_success": not any(c["status"] in {"读取失败", "需调整"} for c in report["checks"]),
            "manual_review_required": True, "report": "runtime/client-check-live.json", "checks": report["checks"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机环境/配置检查；不提交买卖、不撤单、不确认弹窗")
    parser.add_argument("--root", default=".")
    parser.add_argument("--mode", choices=("paper", "ths-live"), default="ths-live")
    parser.add_argument("--json", action="store_true", help="默认环境报告不包含账户/窗口标题/凭据")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--discover", action="store_true", help="只读窗口发现；输出含本机隐私信息，不可原样公开")
    actions.add_argument("--init-config", action="store_true", help="交互生成本机配置；不覆盖已有文件，不操作客户端")
    actions.add_argument("--check-client", action="store_true", help="切换查询页读取账户和委托；不发单，报告仅存本机")
    parser.add_argument("--warm-ocr", action="store_true", help="使用空白图片检查OCR模型，不截取桌面")
    parser.add_argument("--window-id", type=int)
    parser.add_argument("--broker")
    parser.add_argument("--account-marker")
    parser.add_argument("--alias", default="broker")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.discover:
            result = {"private_local_data": True, "no_ui_input": True, "candidates": discover_clients()}
        elif args.init_config:
            if (root / "config/ths.local.json").exists():
                raise ValueError("config/ths.local.json 已存在，未覆盖；先备份后自行决定是否重新配置")
            candidate = choose_client(discover_clients(), args.window_id)
            hint = candidate.get("identity_hint", "").split("-", 1)
            defaults = hint if len(hint) == 2 else ["", ""]
            def field(value, label, default):
                if value is not None:
                    return value
                if not sys.stdin.isatty():
                    raise ValueError("非交互模式请同时提供 --broker 和 --account-marker")
                return input(f"{label}" + (f" [{default}]" if default else "") + ": ").strip() or default
            broker = field(args.broker, "窗口中的券商标识", defaults[0])
            marker = field(args.account_marker, "窗口中的账户标识（可用显示的掩码名称/尾号）", defaults[1])
            template = json.loads((root / "config/ths.example.json").read_text(encoding="utf-8-sig"))
            config = build_config(template, candidate, broker, marker, args.alias)
            write_local_config(root, config)
            result = {"created": "config/ths.local.json", "no_ui_input": True, "client_verified": False,
                      "next": "打开测试台切换实盘，运行客户端适配检查，核对账户/持仓/委托后单独检查盘口"}
        else:
            result = environment_report(root, live=args.mode == "ths-live")
            if args.warm_ocr and result["ok"]:
                result["ocr_warmup"] = warm_ocr()
            if args.check_client and result["ok"]:
                result["contains_account_data"] = True
                result["client_check"] = check_client(root)
                result["ok"] = result["client_check"]["read_success"]
        if args.json or args.discover:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif "checks" in result:
            for row in result["checks"]:
                print(f"[{row['status'].upper()}] {row['item']}: {row['detail']}")
            for key in ("ocr_warmup", "client_check"):
                if key in result:
                    print(json.dumps({key: result[key]}, ensure_ascii=False, indent=2))
            print("环境检查不代表已完成客户端或真实交易验收。")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error), "no_order_submitted": True}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
