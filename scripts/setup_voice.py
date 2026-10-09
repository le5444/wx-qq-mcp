"""Provision optional local ASR outside the checkout. No action without --model.

Example: python scripts/setup_voice.py --model sensevoice --plan
Remove --plan to create the dedicated environment and obtain verified weights.
Model terms are separate from this project's source license; see plan links.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

CHECKOUT = Path(__file__).resolve().parent.parent
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))

from unified_mcp.voice_transcription import ASR_HOME, MODEL, PYTHON, SENSE_MODEL


SENSE_ARCHIVE = {
    "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
    "bytes": 163002883,
    "sha256": "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e",
}
SENSE_FILES = {
    "model.int8.onnx": {"bytes": 239233841, "sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"},
    "tokens.txt": {"bytes": 315894, "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"},
}
WHISPER_REPO = "Systran/faster-whisper-small"
WHISPER_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
WHISPER_FILES = {
    "config.json": {"bytes": 2370, "sha256": "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828"},
    "model.bin": {"bytes": 483546902, "sha256": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"},
    "tokenizer.json": {"bytes": 2203239, "sha256": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"},
    "vocabulary.txt": {"bytes": 459861, "sha256": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"},
}
LICENSE_LINKS = {
    "sensevoice": ["https://github.com/QwenAudio/SenseVoice#license",
                   "https://huggingface.co/FunAudioLLM/SenseVoiceSmall",
                   "https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html"],
    "whisper": [f"https://huggingface.co/{WHISPER_REPO}/blob/{WHISPER_REVISION}/README.md",
                "https://huggingface.co/openai/whisper-small"],
}
GITHUB_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


def external_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path == CHECKOUT or path.is_relative_to(CHECKOUT):
        raise ValueError("ASR environments and weights must be outside the source checkout; change WX_UNIFIED_ASR_HOME/model settings.")
    return path


def make_plan(model: str, requirements=None) -> dict:
    if model not in {"sensevoice", "whisper", "both"}:
        raise ValueError("Choose sensevoice, whisper or both")
    home = external_path(ASR_HOME)
    expected_python = home / "venv/Scripts/python.exe"
    if PYTHON.expanduser().resolve() != expected_python:
        raise ValueError("This helper creates ASR_HOME/venv. Set WX_UNIFIED_ASR_PYTHON to that venv's Scripts/python.exe, or unset it before setup.")
    selected = ["sensevoice", "whisper"] if model == "both" else [model]
    models = []
    for name in selected:
        directory = external_path(SENSE_MODEL if name == "sensevoice" else MODEL)
        models.append({"name": name, "directory": str(directory),
                       "source": SENSE_ARCHIVE["url"] if name == "sensevoice" else f"https://huggingface.co/{WHISPER_REPO}/tree/{WHISPER_REVISION}",
                       "files": SENSE_FILES if name == "sensevoice" else WHISPER_FILES,
                       "license_and_model_cards": LICENSE_LINKS[name]})
    return {"plan_only": True, "asr_home": str(home), "python": str(expected_python),
            "requirements": str(Path(requirements).resolve()) if requirements else "package:unified_mcp/requirements-voice.txt",
            "models": models, "audio_uploads": False,
            "note": "Weights have their own model terms. This source project's MIT license does not relicense model weights. Installation downloads packages and selected models; later ASR runs offline."}


def matches(path: Path, spec: dict) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != spec["bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == spec["sha256"]


def verified_files(directory: Path, specs: dict) -> bool:
    return all(matches(directory / name, spec) for name, spec in specs.items())


def checked_https(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in GITHUB_DOWNLOAD_HOSTS
            or parts.username is not None or parts.password is not None or parts.port not in (None, 443)):
        raise ValueError("Model archive redirect is outside official GitHub HTTPS download hosts")


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_archive(destination: Path, spec=SENSE_ARCHIVE) -> None:
    checked_https(spec["url"])
    opener = urllib.request.build_opener(OfficialRedirects())
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "wx-qq-mcp-voice-setup/0.2.1"})
    total = 0
    with opener.open(request, timeout=60) as response, destination.open("xb") as output:
        checked_https(response.geturl())
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            total += len(chunk)
            if total > spec["bytes"]:
                raise ValueError("Model archive exceeds pinned size")
            output.write(chunk)
    if not matches(destination, spec):
        raise ValueError("Model archive failed pinned size/SHA256 verification")


def extract_sensevoice(archive_path: Path, output: Path, specs=None, archive_spec=None) -> None:
    specs = SENSE_FILES if specs is None else specs
    archive_spec = SENSE_ARCHIVE if archive_spec is None else archive_spec
    if not matches(archive_path, archive_spec):
        raise ValueError("Model archive failed pinned size/SHA256 verification")
    with tarfile.open(archive_path, mode="r:bz2") as archive:
        chosen = {}
        for member in archive:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or any(":" in part for part in path.parts) or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Model archive contains an unsafe path or non-regular entry")
            if path.name in specs and member.isfile():
                if path.name in chosen or member.size != specs[path.name]["bytes"]:
                    raise ValueError("Model archive has duplicate or incorrectly sized selected files")
                chosen[path.name] = member
        if set(chosen) != set(specs):
            raise ValueError("Model archive is missing required files")
        output.mkdir(parents=True, exist_ok=True)
        for name, member in chosen.items():
            # Never extract paths from the archive. Copy only the two fixed names.
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Model entry cannot be read")
            with source, (output / name).open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        if not verified_files(output, specs):
            raise ValueError("Extracted model files failed pinned SHA256 verification")


def run_hidden(command, *, env=None):
    return subprocess.run([str(arg) for arg in command], check=True, env=env,
                          stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@contextmanager
def requirements_path(explicit=None):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError("Voice requirements file is missing")
        yield path
    else:
        with resources.as_file(resources.files("unified_mcp").joinpath("requirements-voice.txt")) as path:
            yield path


def download_whisper(python: Path, output: Path, cache: Path) -> None:
    code = """import json, sys
from huggingface_hub import snapshot_download
snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2], local_dir=sys.argv[3],
                  cache_dir=sys.argv[4], allow_patterns=json.loads(sys.argv[5]),
                  endpoint='https://huggingface.co', token=False, max_workers=1)
"""
    env = {**os.environ, "HF_ENDPOINT": "https://huggingface.co", "HF_HUB_DISABLE_TELEMETRY": "1",
           "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_OFFLINE": "0", "HF_HUB_DISABLE_XET": "1"}
    run_hidden([python, "-c", code, WHISPER_REPO, WHISPER_REVISION, output, cache,
                json.dumps(list(WHISPER_FILES))], env=env)
    if not verified_files(output, WHISPER_FILES):
        raise ValueError("Whisper files failed pinned size/SHA256 verification")


def install(plan: dict, requirements=None) -> dict:
    if os.name != "nt":
        raise ValueError("This helper provisions the Windows ASR environment. --plan is available on all platforms.")
    home, python = Path(plan["asr_home"]), Path(plan["python"])
    # Validate destinations before creating a venv or downloading anything.
    pending = []
    for item in plan["models"]:
        target = Path(item["directory"])
        if verified_files(target, item["files"]):
            item["status"] = "already_verified"
        elif target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("Selected model directory contains different/incomplete files; choose an empty external directory using the model path setting. Existing files were preserved.")
        else:
            pending.append(item)
    venv = home / "venv"
    if venv.exists() and any(venv.iterdir()) and not (venv / "pyvenv.cfg").is_file():
        raise ValueError("ASR venv directory is not a recognized Python environment; existing files were preserved.")
    with requirements_path(requirements) as req:
        if not python.is_file():
            home.mkdir(parents=True, exist_ok=True)
            run_hidden([sys.executable, "-m", "venv", venv])
        run_hidden([python, "-m", "pip", "--isolated", "install", "--index-url", "https://pypi.org/simple", "--requirement", req])
    for item in pending:
        target = Path(item["directory"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".wxqq-model-", dir=target.parent) as temporary:
            staging = Path(temporary)
            ready = staging / "ready"
            if item["name"] == "sensevoice":
                archive = staging / "sensevoice.tar.bz2"
                download_archive(archive)
                extract_sensevoice(archive, ready)
            else:
                downloaded = staging / "downloaded"
                download_whisper(python, downloaded, home / "downloads/huggingface")
                ready.mkdir()
                for name in WHISPER_FILES:
                    shutil.copyfile(downloaded / name, ready / name)
            if not verified_files(ready, item["files"]):
                raise ValueError("Prepared model files failed verification")
            if target.exists():
                if any(target.iterdir()):
                    raise ValueError("Model destination changed during setup; existing files were preserved")
                target.rmdir()  # Only the already checked empty target directory.
            os.replace(ready, target)
            item["status"] = "installed_and_verified"
    plan["plan_only"] = False
    return plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=("sensevoice", "whisper", "both"))
    parser.add_argument("--plan", action="store_true", help="Show paths, hashes and model terms without creating files, installing or downloading")
    parser.add_argument("--requirements", type=Path, help="Optional requirements file; defaults to the packaged requirements-voice.txt")
    args = parser.parse_args(argv)
    try:
        plan = make_plan(args.model, args.requirements)
        if not args.plan:
            print("Model terms: " + json.dumps({item["name"]: item["license_and_model_cards"] for item in plan["models"]}), flush=True)
            plan = install(plan, args.requirements)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"Voice setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
