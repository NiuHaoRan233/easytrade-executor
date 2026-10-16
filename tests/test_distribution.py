import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


spec = importlib.util.spec_from_file_location("share_builder", Path(__file__).resolve().parents[1] / "tools/build_share.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class DistributionTests(unittest.TestCase):
    def test_share_allowlist_includes_onboarding_and_excludes_local_account_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in builder.FIXED:
                path = root / name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('public placeholder',encoding="utf-8")
            (root/"pyproject.toml").write_text('[project]\nversion="0.3.0"\n',encoding="utf-8")
            for name in ("src/easytrade/doctor.py", "tests/test_doctor.py"):
                path = root/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('# public source',encoding="utf-8")
            for name in ("runtime/api-token.txt","runtime/account.json","config/ths.local.json", ".venv/private.txt", "artifacts/screenshot.png"):
                path = root/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('PRIVATE-ACCOUNT-MARKER',encoding="utf-8")
            names = builder.public_files(root)
            self.assertIn("docs/first-machine.md",names)
            self.assertIn("Configure-Client.cmd",names)
            self.assertIn("src/easytrade/doctor.py",names)
            result = builder.build(root)
            with zipfile.ZipFile(result["archive"]) as archive:
                contents = {name:archive.read(name) for name in archive.namelist()}
            self.assertFalse(any(b'PRIVATE-ACCOUNT-MARKER' in body for body in contents.values()))
            self.assertFalse(any('/runtime/' in name or '.local.' in name or '/.venv/' in name for name in contents))
            manifest = json.loads(contents['easytrade-executor-0.3.0/SHARE-MANIFEST.json'])
            self.assertEqual(set(manifest['files']),set(names))
