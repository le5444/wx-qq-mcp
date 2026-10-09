"""Installer checks use tiny synthetic archives; no package/model downloads."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent.parent / "scripts/setup_voice.py"
spec = importlib.util.spec_from_file_location("wxqq_setup_voice", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def digest(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class VoiceSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wxqq-setup-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = {"model.int8.onnx": b"synthetic model", "tokens.txt": b"synthetic tokens"}
        self.specs = {name: digest(data) for name, data in self.files.items()}

    def archive(self, extras=(), corrupt_model=False):
        path = self.root / "model.tar.bz2"
        with tarfile.open(path, "w:bz2") as archive:
            for name, data in self.files.items():
                entry = tarfile.TarInfo("upstream-model/" + name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            for entry, data in extras:
                archive.addfile(entry, io.BytesIO(data) if data is not None else None)
        return path, digest(path.read_bytes())

    def plan(self):
        home = self.root / "asr"
        with patch.multiple(setup, ASR_HOME=home, PYTHON=home / "venv/Scripts/python.exe",
                            SENSE_MODEL=home / "models/sensevoice", MODEL=home / "models/whisper"):
            return setup.make_plan("sensevoice")

    def test_plan_and_missing_model_have_no_side_effects(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("WXQQ_", "WX_UNIFIED_"))}
        env.update(WXQQ_DATA_DIR=str(self.root / "data"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), "--model", "both", "--plan"],
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan["plan_only"])
        self.assertEqual([m["name"] for m in plan["models"]], ["sensevoice", "whisper"])
        self.assertTrue(all(Path(m["directory"]).is_relative_to(self.root / "data") for m in plan["models"]))
        self.assertFalse((self.root / "data").exists())
        absent = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, timeout=20)
        self.assertEqual(absent.returncode, 2)
        self.assertFalse((self.root / "data").exists())

    def test_repo_local_model_destination_is_rejected(self):
        with patch.object(setup, "ASR_HOME", setup.CHECKOUT / "models"):
            with self.assertRaisesRegex(ValueError, "outside the source checkout"):
                setup.make_plan("sensevoice")

    def test_extracts_only_two_verified_files(self):
        entry = tarfile.TarInfo("upstream-model/unneeded.bin")
        entry.size = 4
        archive, archive_spec = self.archive([(entry, b"junk")])
        output = self.root / "selected"
        setup.extract_sensevoice(archive, output, self.specs, archive_spec)
        self.assertEqual({p.name for p in output.iterdir()}, set(self.files))
        self.assertTrue(setup.verified_files(output, self.specs))

    def test_tampered_archive_rejected_before_output_creation(self):
        archive, archive_spec = self.archive()
        archive_spec["sha256"] = "0" * 64
        output = self.root / "selected"
        with self.assertRaisesRegex(ValueError, "archive failed"):
            setup.extract_sensevoice(archive, output, self.specs, archive_spec)
        self.assertFalse(output.exists())

    def test_unsafe_tar_entries_are_rejected_without_extraction(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("C:/absolute", tarfile.REGTYPE), ("folder\\escape", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE)):
            with self.subTest(name=name):
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.linkname = "../escape"
                archive, archive_spec = self.archive([(entry, b"" if kind == tarfile.REGTYPE else None)])
                output = self.root / "selected"
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    setup.extract_sensevoice(archive, output, self.specs, archive_spec)
                self.assertFalse(output.exists())

    def test_duplicate_or_tampered_selected_file_is_rejected(self):
        duplicate = tarfile.TarInfo("other/tokens.txt")
        duplicate.size = len(self.files["tokens.txt"])
        archive, archive_spec = self.archive([(duplicate, self.files["tokens.txt"])])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            setup.extract_sensevoice(archive, self.root / "selected", self.specs, archive_spec)
        archive, archive_spec = self.archive()
        wrong = copy.deepcopy(self.specs)
        wrong["tokens.txt"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Extracted model files"):
            setup.extract_sensevoice(archive, self.root / "selected", wrong, archive_spec)

    def test_existing_verified_models_are_not_downloaded(self):
        plan = self.plan()
        item = plan["models"][0]
        item["files"] = self.specs
        target = Path(item["directory"])
        target.mkdir(parents=True)
        for name, data in self.files.items():
            (target / name).write_bytes(data)
        with patch.object(setup, "run_hidden") as runner, patch.object(setup, "download_archive") as download:
            result = setup.install(plan)
        self.assertEqual(result["models"][0]["status"], "already_verified")
        download.assert_not_called()
        self.assertTrue(any("--requirement" in call.args[0] for call in runner.call_args_list))

    def test_foreign_model_directory_preserved_before_pip_or_download(self):
        plan = self.plan()
        target = Path(plan["models"][0]["directory"])
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        with patch.object(setup, "run_hidden") as runner, patch.object(setup, "download_archive") as download:
            with self.assertRaisesRegex(ValueError, "preserved"):
                setup.install(plan)
        runner.assert_not_called()
        download.assert_not_called()
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_whisper_uses_pinned_official_snapshot_and_only_required_files(self):
        output = self.root / "whisper"
        with patch.object(setup, "run_hidden") as runner, patch.object(setup, "verified_files", return_value=True):
            setup.download_whisper(Path("isolated-python"), output, self.root / "cache")
        command = runner.call_args.args[0]
        self.assertIn(setup.WHISPER_REVISION, command)
        self.assertIn("endpoint='https://huggingface.co'", command[2])
        self.assertIn("token=False", command[2])
        self.assertEqual(set(json.loads(command[-1])), set(setup.WHISPER_FILES))

    def test_official_archive_host_policy_requires_https(self):
        for url in (setup.SENSE_ARCHIVE["url"], "https://release-assets.githubusercontent.com/example"):
            setup.checked_https(url)
        for url in ("http://github.com/example", "https://example.com/model", "https://github.com.evil.test/model", "https://user:pass@github.com/model"):
            with self.assertRaises(ValueError):
                setup.checked_https(url)

    def test_subprocesses_use_hidden_windows_creation_flag(self):
        with patch.object(setup.subprocess, "run") as runner:
            setup.run_hidden([sys.executable, "--version"])
        self.assertTrue(runner.call_args.kwargs["check"])
        self.assertEqual(runner.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    unittest.main()
