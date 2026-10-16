"""Build a source-only share archive from an explicit public allowlist."""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile


FIXED = (
    ".gitignore", ".gitattributes", ".github/workflows/test.yml", "README.md", "CHANGELOG.md", "pyproject.toml",
    "Setup.cmd", "Start-Test-Console.cmd", "Start-Paper-Service.cmd", "Start-Live-Service.cmd",
    "Check-Environment.cmd", "Configure-Client.cmd",
    "constraints/windows-py313-tested.txt",
    "config/paper.example.json", "config/ths.example.json",
    "docs/quickstart.md", "docs/local-test-console.md", "docs/model-integration.md", "docs/architecture.md",
    "docs/client-adaptation.md",
    "docs/first-machine.md", "tools/check_environment.py",
    "examples/model_client.py", "tools/console_smoke.py", "tools/build_share.py",
)


def public_files(root):
    root = Path(root).resolve()
    names = set(FIXED)
    names.update(p.relative_to(root).as_posix() for p in (root / "src/easytrade").glob("*.py"))
    names.update(p.relative_to(root).as_posix() for p in (root / "tests").glob("test_*.py")
                 if p.name != "test_ocr_samples.py")
    for name in sorted(names):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing public file or symlink: {name}")
        if any(part in {"runtime", "artifacts", ".git", "__pycache__"} for part in path.relative_to(root).parts):
            raise ValueError(f"Private file in public list: {name}")
        if ".local." in name:
            raise ValueError(f"Local configuration in public list: {name}")
    return sorted(names)


def build(root):
    root = Path(root).resolve()
    names = public_files(root)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    prefix = f"easytrade-executor-{version}"
    output = root / "dist"
    output.mkdir(exist_ok=True)
    archive = output / f"{prefix}.zip"
    contents = {name: (root / name).read_bytes() for name in names}
    manifest = {"version": version, "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as file:
        for name, data in contents.items():
            file.writestr(f"{prefix}/{name}", data)
        file.writestr(f"{prefix}/SHARE-MANIFEST.json", encoded)
    (output / f"{prefix}-manifest.json").write_bytes(encoded)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / f"{prefix}.zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return {"archive": str(archive), "files": len(names), "sha256": digest}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--list", action="store_true", help="Print exact paths for Git staging")
    args = parser.parse_args()
    if args.list:
        print("\n".join(public_files(args.root)))
    else:
        print(json.dumps(build(args.root), ensure_ascii=False))


if __name__ == "__main__":
    main()
