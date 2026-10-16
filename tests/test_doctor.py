import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from easytrade.doctor import build_config, choose_client, environment_report, write_local_config


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.template = {"accounts":{"broker":{}}, "instruments":{}, "ths_profile":{
            "exe_path":"placeholder", "title_contains":["placeholder"], "tree_control_id":129,
            "grid_control_id":1047, "caption_identity_box":[0,0,650,26], "pages":{"buy":["buy"]}}}
        self.candidate = {"window_id":10, "enabled":True, "title":"网上股票交易系统5.0", "exe_path":"C:/broker/xiadan.exe",
            "window_size":[1000,700], "tree_control_id":129, "grid_control_ids":[1047],
            "account_control_box":[200,40,450,100]}

    def test_generation_preserves_identity_and_has_no_security_catalog(self):
        original = copy.deepcopy(self.template)
        config = build_config(self.template, self.candidate, "Example Broker", "DEMO***42", "local")
        self.assertEqual(self.template, original)
        self.assertEqual(config["accounts"], {"local":{}})
        self.assertEqual(config["instruments"], {})
        self.assertEqual(config["ths_profile"]["title_contains"], ["Example Broker","DEMO***42"])
        self.assertEqual(config["ths_profile"]["caption_identity_box"], [200,40,450,100])
        self.assertEqual(config["ths_profile"]["exe_path"], self.candidate["exe_path"])

    def test_ambiguous_missing_or_blocked_windows_are_not_auto_selected(self):
        for candidates in ([], [self.candidate, {**self.candidate,"window_id":11}], [{**self.candidate,"enabled":False}]):
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                choose_client(candidates)
        self.assertEqual(choose_client([self.candidate,{**self.candidate,"window_id":11}],11)["window_id"],11)

    def test_identity_markers_and_calibrated_region_cannot_be_bypassed(self):
        for broker, marker in (("","DEMO"),("Broker",""),("same","same"),("请填写券商名称","DEMO")):
            with self.subTest(broker=broker,marker=marker), self.assertRaises(ValueError):
                build_config(self.template,self.candidate,broker,marker)
        for box in (None, [0,0,5000,100], [200,100,100,40]):
            with self.subTest(box=box), self.assertRaises(ValueError):
                build_config(self.template,{**self.candidate,"account_control_box":box},"Broker","DEMO")
        titled = {**self.candidate,"title":"Broker DEMO","account_control_box":None}
        self.assertEqual(build_config(self.template,titled,"Broker","DEMO")["ths_profile"]["title_regex"],"^Broker\\ DEMO$")

    def test_existing_local_config_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            config = build_config(self.template,self.candidate,"Broker","DEMO")
            path = write_local_config(directory,config)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_local_config(directory,{"wrong":True})
            self.assertEqual(path.read_bytes(),original)

    def test_default_environment_report_does_not_export_account_values_or_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"config").mkdir()
            config = build_config(self.template,self.candidate,"PRIVATE-BROKER","PRIVATE-ACCOUNT")
            (root/"config/ths.local.json").write_text(json.dumps(config),encoding="utf-8")
            with patch("easytrade.doctor.importlib.metadata.version",return_value="test-version"):
                report = environment_report(root)
            encoded = json.dumps(report)
            for value in ("PRIVATE-BROKER","PRIVATE-ACCOUNT",directory,"C:/broker/xiadan.exe"):
                self.assertNotIn(value,encoded)
            self.assertFalse(report["contains_account_data"])
            self.assertFalse(report["client_verified"])

    def test_missing_dependencies_are_reported_without_importing_the_native_adapter(self):
        from importlib.metadata import PackageNotFoundError
        with tempfile.TemporaryDirectory() as directory, patch("easytrade.doctor.importlib.metadata.version",side_effect=PackageNotFoundError):
            report = environment_report(directory)
        self.assertFalse(report["ok"])
        self.assertTrue(any(row["item"]=="rapidocr" and row["status"]=="error" for row in report["checks"]))
