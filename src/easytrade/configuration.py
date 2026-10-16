"""User-owned configuration; runtime paths are relative to the chosen data root."""
import json
from pathlib import Path


def load_config(root, *, live=False):
    root = Path(root)
    path = root / ("config/ths.local.json" if live else "config/paper.example.json")
    if not path.is_file():
        if live:
            raise ValueError("缺少config/ths.local.json；请复制ths.example.json并填写本机路径和账户身份")
        path = root / "config/ths.example.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))
