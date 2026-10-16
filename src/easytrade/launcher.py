"""Service launcher with a persistent local API token, never printed."""
import argparse
import os
from pathlib import Path
import secrets

from .cli import main as cli_main


def main():
    parser = argparse.ArgumentParser(description="启动Easytrade本地模型接口")
    parser.add_argument("--mode", choices=["paper", "ths-live"], default="paper")
    parser.add_argument("--root", default=".")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not os.environ.get("EASYTRADE_TOKEN"):
        path = root / "runtime/api-token.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        os.environ["EASYTRADE_TOKEN"] = path.read_text(encoding="utf-8").strip()
        print("API凭据位置：runtime/api-token.txt（不随项目分享）", flush=True)
    cli_main(["--root", str(root), "--mode", args.mode, "serve", "--port", str(args.port)])


if __name__ == "__main__":
    main()
