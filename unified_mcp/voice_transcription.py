"""Local-only voice ASR with content-addressed success caches and one worker.

Audio never leaves this machine. The explicitly provisioned multilingual model
must already exist on disk. Go's sidecar transcript caches are never changed.
Failed attempts are retryable and are never stored as successful cache entries.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import wave

try:
    from unified_mcp.runtime_paths import RUNTIME
except ModuleNotFoundError:  # Dedicated ASR venv executes this worker as a file.
    from runtime_paths import RUNTIME

ROOT = Path(__file__).resolve().parent
STATE_HOME = RUNTIME
ASR_HOME = Path(os.environ.get("WX_UNIFIED_ASR_HOME") or str(STATE_HOME / "voice-asr"))
MODEL = Path(os.environ.get("WX_UNIFIED_ASR_MODEL", str(ASR_HOME / "models/faster-whisper-small")))
CACHE = Path(os.environ.get("WX_UNIFIED_VOICE_CACHE") or str(STATE_HOME / "voice-asr/cache"))
MEDIA_CACHE = Path(os.environ.get("WX_UNIFIED_VOICE_MEDIA_CACHE") or str(Path.home() / ".wx-mcp/media-cache"))
PYTHON = Path(os.environ.get("WX_UNIFIED_ASR_PYTHON") or str(ASR_HOME / "venv/Scripts/python.exe"))
CACHE_VERSION = 1
MODEL_NAME = "Systran/faster-whisper-small"
SENSE_MODEL = Path(os.environ.get("WX_UNIFIED_SENSE_MODEL") or str(ASR_HOME / "models/sensevoice-small-int8"))
SENSE_MODEL_NAME = "k2-fsa/sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
MAX_AUDIO_BYTES = 32 * 1024 * 1024
MAX_DURATION_SECONDS = 600
SILK_NAME = re.compile(r"^voice-(?P<talker>[0-9a-f]{32})-(?P<local>\d+)-(?P<time>\d+)-(?P<server>-?\d+)-")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_audio(path: str | Path) -> bytes:
    raw = os.fspath(path)
    if "://" in raw or raw.startswith(("\\\\", "//")):
        raise ValueError("Only local audio paths are supported")
    audio = Path(raw)
    if not audio.is_file() or audio.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("Audio is missing or exceeds the size limit")
    data = audio.read_bytes()
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise ValueError("Audio is empty or exceeds the size limit")
    return data


def cache_identity(data: bytes, model: Path = MODEL, language: str = "zh", beam_size: int = 5) -> tuple[str, str]:
    audio_hash = hashlib.sha256(data).hexdigest()
    # A replacement model at the same path invalidates previous recognition.
    model_file = model / "model.bin"
    try:
        stat = model_file.stat()
        model_revision = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        model_revision = "unavailable"
    identity = f"{CACHE_VERSION}|{audio_hash}|{model.resolve()}|{model_revision}|{language}|cpu-int8-beam{beam_size}-vad"
    return hashlib.sha256(identity.encode()).hexdigest(), audio_hash


def cached_transcript(path: str | Path, cache: Path = CACHE, model: Path = MODEL,
                      language: str = "zh", include_sensevoice: bool = True, sense_model: Path = SENSE_MODEL) -> dict | None:
    try:
        data = read_audio(path)
        # Retain and prefer existing beam-5 results after choosing greedy decoding.
        for beam_size in (5, 1):
            key, audio_hash = cache_identity(data, model, language, beam_size)
            file = cache / "transcripts" / (key + ".json")
            if not file.exists():
                continue
            value = json.loads(file.read_text(encoding="utf-8"))
            if (value.get("cache_version") == CACHE_VERSION and value.get("audio_sha256") == audio_hash
                    and value.get("status") in ("ok", "no_speech")):
                return {**value, "cache_hit": True, "beam_size": value.get("beam_size", beam_size)}
        if include_sensevoice:
            key, audio_hash = sense_cache_identity(data, sense_model)
            file = cache / "transcripts" / (key + ".json")
            if file.is_file():
                value = json.loads(file.read_text(encoding="utf-8"))
                if value.get("audio_sha256") == audio_hash and value.get("status") in {"ok", "no_speech"}:
                    return {**value, "cache_hit": True}
    except (OSError, ValueError, TypeError):
        pass
    return None


class LocalVoiceASR:
    """Use only from one worker thread/process; model loads lazily once."""

    def __init__(self, model: Path = MODEL, cache: Path = CACHE, language: str = "zh", model_factory=None, beam_size: int = 1):
        self.model_path = Path(model)
        self.cache = Path(cache)
        self.language = language
        self.model_factory = model_factory
        self.model = None
        self.beam_size = beam_size

    def _model(self):
        if self.model is None:
            if self.model_factory is not None:
                self.model = self.model_factory()
            else:
                if not (self.model_path / "model.bin").is_file():
                    raise FileNotFoundError("Provisioned ASR model is missing")
                from faster_whisper import WhisperModel
                self.model = WhisperModel(str(self.model_path), device="cpu", compute_type="int8",
                                          cpu_threads=4, num_workers=1, local_files_only=True)
        return self.model

    def _decode(self, data: bytes, path: Path, audio_hash: str) -> tuple[Path, float | None]:
        if data[:10].lstrip(b"\x02").startswith(b"#!SILK_V3"):
            import pysilk
            output = io.BytesIO()
            pysilk.decode(io.BytesIO(data), output, 24000)
            pcm = output.getvalue()
            duration = len(pcm) / (24000 * 2)
            if not pcm or len(pcm) % 2 or duration > MAX_DURATION_SECONDS:
                raise ValueError("Decoded audio is empty or exceeds the duration limit")
            target = self.cache / "decoded" / (audio_hash + ".wav")
            target.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(target), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(pcm)
            return target, duration
        if path.suffix.lower() not in {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".amr", ".opus", ".aac"}:
            raise ValueError("Unsupported local voice audio format")
        return path, None

    def transcribe(self, path: str | Path, *, force: bool = False, beam_size: int | None = None, persist: bool = True) -> dict:
        started = time.monotonic()
        actual_beam = self.beam_size if beam_size is None else beam_size
        try:
            path = Path(path)
            data = read_audio(path)
            cached = cached_transcript(path, self.cache, self.model_path, self.language, include_sensevoice=False)
            if cached is not None and not force:
                return cached
            key, audio_hash = cache_identity(data, self.model_path, self.language, actual_beam)
            audio, decoded_duration = self._decode(data, path, audio_hash)
            segments, info = self._model().transcribe(
                str(audio), language=self.language, beam_size=actual_beam, best_of=actual_beam, vad_filter=True,
                condition_on_previous_text=False, hallucination_silence_threshold=2.0,
                vad_parameters={"min_silence_duration_ms": 300})
            duration = float(info.duration)
            if duration > MAX_DURATION_SECONDS:
                raise ValueError("Audio exceeds the duration limit")
            parts = [{"start": round(float(s.start), 3), "end": round(float(s.end), 3),
                      "text": s.text.strip(), "avg_logprob": round(float(s.avg_logprob), 4),
                      "no_speech_prob": round(float(s.no_speech_prob), 4)} for s in segments]
            transcript = "".join(s["text"] for s in parts).strip()
            result = {"cache_version": CACHE_VERSION, "status": "ok" if transcript else "no_speech",
                      "text": transcript, "engine": "faster-whisper-local", "model": MODEL_NAME,
                      "language": info.language, "language_probability": float(info.language_probability),
                      "language_mode": "specified_zh", "compute_type": "int8", "beam_size": actual_beam,
                      "automatic": True, "human_verified": False, "audio_sha256": audio_hash,
                      "audio_path": str(path.resolve()), "decoded_audio_path": str(audio.resolve()),
                      "duration_seconds": decoded_duration if decoded_duration is not None else duration,
                      "segments": parts, "cache_hit": False, "elapsed_seconds": round(time.monotonic()-started, 3),
                      "warning": "Local automatic speech recognition; transcription may contain errors."}
            reasons = []
            if parts and sum(s["avg_logprob"] for s in parts)/len(parts) < -0.8:
                reasons.append("low_log_probability")
            if parts and max(s["no_speech_prob"] for s in parts) > 0.5:
                reasons.append("high_no_speech_probability")
            if re.search(r"(.{2,12})\1{3,}", transcript):
                reasons.append("repetitive_text")
            if len(transcript) > max(20, duration * 10):
                reasons.append("unusual_text_rate")
            result["review_reasons"] = reasons
            result["review_required"] = bool(reasons)
            if actual_beam == 1 and reasons and persist:
                reviewed = self.transcribe(path, force=True, beam_size=5, persist=True)
                if reviewed.get("status") in {"ok", "no_speech"}:
                    reviewed["reviewed_with_beam5"] = True
                    reviewed["initial_review_reasons"] = reasons
                    reviewed["initial_beam1_elapsed_seconds"] = result["elapsed_seconds"]
                    reviewed["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    reviewed_key, _ = cache_identity(data, self.model_path, self.language, 5)
                    atomic_json(self.cache / "transcripts" / (reviewed_key + ".json"), reviewed)
                    return reviewed
            if persist:
                atomic_json(self.cache / "transcripts" / (key + ".json"), result)
            return result
        except Exception as exc:
            # Do not leak audio contents or permanently remember transient errors.
            return {"status": "unavailable", "engine": "faster-whisper-local", "automatic": True,
                    "retryable": True, "error_type": type(exc).__name__, "cache_hit": False}


def sense_cache_identity(data: bytes, model: Path = SENSE_MODEL) -> tuple[str, str]:
    audio_hash = hashlib.sha256(data).hexdigest()
    revisions = []
    for name in ("model.int8.onnx", "tokens.txt"):
        try:
            stat = (model / name).stat()
            revisions.append(f"{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            revisions.append("unavailable")
    identity = f"{CACHE_VERSION}|{audio_hash}|{model.resolve()}|{'|'.join(revisions)}|sensevoice-int8-zh-itn"
    return "sv-" + hashlib.sha256(identity.encode()).hexdigest(), audio_hash


class SenseVoiceASR:
    """Short Chinese voice messages use CTC; suspicious output gets Whisper review.

    Only one recognizer is held at a time. Existing Whisper cache entries remain
    preferred. Confidence is absent when the installed CTC API does not supply it.
    """

    def __init__(self, model: Path = SENSE_MODEL, cache: Path = CACHE, model_factory=None):
        self.model_path = Path(model)
        self.cache = Path(cache)
        self.model_factory = model_factory
        self.model = None

    def _model(self):
        if self.model is None:
            if self.model_factory:
                self.model = self.model_factory()
            else:
                import sherpa_onnx
                self.model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(self.model_path / "model.int8.onnx"), tokens=str(self.model_path / "tokens.txt"),
                    num_threads=2, provider="cpu", language="zh", use_itn=True, debug=False)
        return self.model

    def _audio(self, data: bytes, path: Path, audio_hash: str):
        import numpy as np
        if data[:10].lstrip(b"\x02").startswith(b"#!SILK_V3"):
            import pysilk
            pcm = io.BytesIO()
            pysilk.decode(io.BytesIO(data), pcm, 16000)
            pcm = pcm.getvalue()
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
            rate = 16000
            decoded = self.cache / "decoded" / (audio_hash + ".sv16.wav")
            decoded.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(decoded), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(pcm)
        else:
            from faster_whisper.audio import decode_audio
            samples, rate, decoded = decode_audio(str(path), sampling_rate=16000), 16000, path
        if not len(samples) or len(samples)/rate > MAX_DURATION_SECONDS:
            raise ValueError("Decoded audio is empty or exceeds the duration limit")
        return samples, rate, decoded

    def transcribe(self, path: str | Path, *, force: bool = False, persist: bool = True) -> dict:
        started = time.monotonic()
        try:
            path = Path(path)
            data = read_audio(path)
            cached = cached_transcript(path, self.cache, sense_model=self.model_path)
            if cached and not force:
                return cached
            key, audio_hash = sense_cache_identity(data, self.model_path)
            samples, rate, decoded = self._audio(data, path, audio_hash)
            duration = len(samples)/rate
            model = self._model()
            stream = model.create_stream()
            stream.accept_waveform(rate, samples)
            model.decode_stream(stream)
            result = stream.result
            transcript = result.text.strip()
            scores = [float(p) for p in getattr(result, "ys_log_probs", [])]
            reasons = []
            if scores and sum(scores)/len(scores) < -0.8:
                reasons.append("low_token_log_probability")
            if re.search(r"(.{2,12})\1{3,}", transcript):
                reasons.append("repetitive_text")
            if len(transcript) > max(20, duration * 10):
                reasons.append("unusual_text_rate")
            import numpy as np
            rms = float(np.sqrt(np.mean(samples*samples)))
            if (not transcript and rms > 0.008) or (transcript and rms < 0.001):
                reasons.append("audio_text_mismatch")
            value = {"cache_version": CACHE_VERSION, "status": "ok" if transcript else "no_speech",
                     "text": transcript, "engine": "sherpa-onnx-sensevoice-local", "model": SENSE_MODEL_NAME,
                     "language": "zh", "language_mode": "specified_zh", "compute_type": "int8",
                     "decoding_method": "greedy_search", "automatic": True, "human_verified": False,
                     "audio_sha256": audio_hash, "audio_path": str(path.resolve()),
                     "decoded_audio_path": str(decoded.resolve()), "duration_seconds": duration,
                     "segments": [], "cache_hit": False, "confidence_available": bool(scores),
                     "mean_token_logprob": sum(scores)/len(scores) if scores else None,
                     "review_required": bool(reasons), "review_reasons": reasons,
                     "elapsed_seconds": round(time.monotonic()-started, 3),
                     "warning": "Local automatic speech recognition; transcription may contain errors."}
            if persist and reasons:
                # Drop all references to the CTC model before loading Whisper.
                del stream, result, model
                self.model = None
                import gc
                gc.collect()
                reviewer = LocalVoiceASR(cache=self.cache, beam_size=5)
                reviewed = reviewer.transcribe(path, force=True, beam_size=5)
                reviewer.model = None
                del reviewer
                gc.collect()
                if reviewed.get("status") in {"ok", "no_speech"}:
                    reviewed["reviewed_from_engine"] = value["engine"]
                    reviewed["initial_review_reasons"] = reasons
                    reviewed["alternative_transcript"] = {"text": transcript, "engine": value["engine"],
                                                           "automatic": True, "human_verified": False}
                    reviewed["elapsed_seconds"] = round(time.monotonic()-started, 3)
                    reviewed_key, _ = cache_identity(data, beam_size=5)
                    atomic_json(self.cache / "transcripts" / (reviewed_key + ".json"), reviewed)
                    return reviewed
                value["review_retryable"] = True
                return value  # A failed review is not permanently cached.
            if persist:
                atomic_json(self.cache / "transcripts" / (key + ".json"), value)
            return value
        except Exception as exc:
            return {"status": "unavailable", "engine": "sherpa-onnx-sensevoice-local", "automatic": True,
                    "retryable": True, "error_type": type(exc).__name__, "cache_hit": False}


def create_local_engine():
    if (SENSE_MODEL / "model.int8.onnx").is_file() and os.environ.get("WX_UNIFIED_ASR_ENGINE") != "whisper":
        return SenseVoiceASR()
    return LocalVoiceASR()


def is_voice(node: dict) -> bool:
    return (node.get("kind") == "voice" or node.get("type") == "voice"
            or node.get("kind_name") == "voice" or node.get("base_kind") == 34
            or node.get("resource_family") == "voice" or "voice_server_id_str" in node
            or node.get("message_kind") == "voice" or node.get("message_kind_name") == "voice"
            or node.get("local_type") == 34 or node.get("msg_type") == 34
            or isinstance(node.get("voice"), dict))


def audio_path_from_message(node: dict, media_cache: Path = MEDIA_CACHE) -> Path | None:
    """Resolve audio solely from explicit paths or exact server-id cache names."""
    candidates = []
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"path", "local_path", "audio_path", "decoded_path"} and isinstance(child, str):
                    candidates.append(child)
                elif key in {"local_paths", "direct_readable_local_paths", "decoded_local_paths"} and isinstance(child, list):
                    candidates.extend(c for c in child if isinstance(c, str))
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(node)
    for candidate in candidates:
        if candidate.startswith(("\\\\", "//")) or "://" in candidate:
            continue
        path = Path(candidate)
        if path.suffix.lower() in {".silk", ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".amr", ".opus", ".aac"} and path.is_file():
            return path
    ids = [node.get("server_id_str"), node.get("voice_server_id_str"), node.get("server_id"), node.get("message_id"), node.get("id", {}).get("server_id_str") if isinstance(node.get("id"), dict) else None]
    original = node.get("original")
    if isinstance(original, dict):
        identity = original.get("id", {})
        if isinstance(identity, dict):
            ids.append(identity.get("server_id_str"))
    row = original if isinstance(original, dict) else node
    identity = row.get("id") if isinstance(row.get("id"), dict) else row
    local_id = identity.get("local_id", row.get("voice_local_id"))
    talker = identity.get("talker", row.get("talker"))
    created = row.get("create_time", row.get("voice_create_time", node.get("timestamp")))
    for identifier in ids:
        if identifier and str(identifier).lstrip("-").isdigit() and int(identifier) != 0:
            matches = [p for p in media_cache.glob(f"*/voice-*-{identifier}-*.silk")
                       if (match := SILK_NAME.match(p.name)) and match.group("server") == str(identifier)
                       and (local_id is None or match.group("local") == str(local_id))
                       and (created is None or match.group("time") == str(created))
                       and (not talker or match.group("talker") == hashlib.md5(str(talker).encode()).hexdigest())]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                # Go versions include or omit duration in otherwise identical
                # names. Identical bytes are the same audio, not an ambiguity.
                try:
                    hashes = {hashlib.sha256(read_audio(p)).digest() for p in matches}
                except (OSError, ValueError):
                    continue
                if len(hashes) == 1:
                    return min(matches, key=lambda p: len(str(p)))
    return None


def attach_transcript(node: dict, transcript: dict) -> dict:
    result = copy.deepcopy(node)
    result["voice_transcript"] = transcript
    voice = result.setdefault("voice", {})
    if isinstance(voice, dict):
        voice["transcript"] = transcript
    if transcript.get("status") in {"ok", "no_speech"}:
        def clear_stale(value):
            if isinstance(value, dict):
                if isinstance(value.get("warnings"), list):
                    value["warnings"] = [warning for warning in value["warnings"]
                                         if warning != "voice_transcription_unavailable"]
                if isinstance(value.get("transcript"), dict) and value["transcript"].get("status") == "unavailable":
                    value["transcript"] = transcript
                for key, child in value.items():
                    if key not in {"voice_transcript", "transcript"}:
                        clear_stale(child)
            elif isinstance(value, list):
                for child in value:
                    clear_stale(child)
        clear_stale(result)
    if transcript.get("status") == "ok" and transcript.get("text"):
        if "text" in result:
            result.setdefault("original_voice_text", result["text"])
            result["text"] = "[语音·本地自动识别] " + transcript["text"]
    return result


class VoiceService:
    """Bounded serialized subprocess; ASR dependencies stay outside gateway env."""

    def __init__(self, python: Path = PYTHON, timeout_seconds: float = 180, idle_seconds: float = 60):
        self.python = Path(python)
        self.timeout_seconds = timeout_seconds
        self.idle_seconds = idle_seconds
        if min(timeout_seconds, idle_seconds) <= 0:
            raise ValueError("Voice service timeouts must be positive")
        self.process = None
        self.lock = asyncio.Lock()
        self.closed = False
        self.idle_task = None
        self.last_used = time.monotonic()

    def _start_idle_watch(self):
        if self.idle_task is None or self.idle_task.done():
            self.idle_task = asyncio.create_task(self._watch_idle())

    async def _watch_idle(self):
        try:
            while self.process is not None and not self.closed:
                delay = max(0.001, self.idle_seconds - (time.monotonic() - self.last_used))
                await asyncio.sleep(delay)
                async with self.lock:
                    if self.process is not None and time.monotonic() - self.last_used >= self.idle_seconds:
                        await self._stop()
                        return
        except asyncio.CancelledError:
            return

    async def _stop(self):
        idle, self.idle_task = self.idle_task, None
        if idle is not None and idle is not asyncio.current_task():
            idle.cancel()
        process, self.process = self.process, None
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), timeout=5)

    async def transcribe(self, path: str | Path) -> dict:
        if self.closed:
            return {"status": "unavailable", "retryable": True, "error_type": "ServiceClosed"}
        cached = await asyncio.to_thread(cached_transcript, path)
        if cached:
            return cached
        async with self.lock:
            try:
                if self.closed:
                    raise RuntimeError("Local voice service is closed")
                self.last_used = time.monotonic()
                if self.process is None or self.process.returncode is not None:
                    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    self.process = await asyncio.create_subprocess_exec(
                        str(self.python), str(Path(__file__).resolve()), "--worker",
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, creationflags=flags, limit=1024*1024,
                        env={**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
                    if self.closed:
                        await self._stop()
                        raise RuntimeError("Local voice service is closed")
                    self._start_idle_watch()
                self.process.stdin.write((json.dumps({"audio_path": str(path)}, ensure_ascii=True) + "\n").encode())
                await self.process.stdin.drain()
                response = await asyncio.wait_for(self.process.stdout.readline(), timeout=self.timeout_seconds)
                if not response:
                    raise RuntimeError("Local voice worker exited")
                return json.loads(response)
            except asyncio.CancelledError:
                await self._stop()
                raise
            except Exception as exc:
                await self._stop()
                return {"status": "unavailable", "engine": "faster-whisper-local", "automatic": True,
                        "retryable": True, "error_type": type(exc).__name__}
            finally:
                self.last_used = time.monotonic()

    async def enrich(self, payload, transcribe: bool = True):
        if isinstance(payload, list):
            return [await self.enrich(value, transcribe) for value in payload]
        if not isinstance(payload, dict):
            return payload
        if is_voice(payload):
            path = await asyncio.to_thread(audio_path_from_message, payload)
            if path is None:
                return attach_transcript(payload, {"status": "audio_missing", "retryable": True,
                                                   "automatic": True, "engine": "faster-whisper-local"})
            transcript = await self.transcribe(path) if transcribe else await asyncio.to_thread(cached_transcript, path)
            return attach_transcript(payload, transcript or {"status": "not_transcribed", "retryable": True})
        return {key: await self.enrich(value, transcribe) for key, value in payload.items()}

    async def close(self):
        self.closed = True
        # Closing must not queue behind a slow ASR inference holding the lock.
        await self._stop()


def run_batch(input_path: Path, output_path: Path, summary_path: Path, limit: int | None = None):
    engine = create_local_engine()
    rows = [json.loads(line) for line in input_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    rows = rows[:limit] if limit is not None else rows
    summary = {"total": len(rows), "completed": 0, "counts": {}, "pid": os.getpid(),
               "state": "running", "input": str(input_path), "output": str(output_path),
               "default_model": SENSE_MODEL_NAME if isinstance(engine, SenseVoiceASR) else MODEL_NAME,
               "audio_uploads": False}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(summary_path, summary)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            path = audio_path_from_message(row)
            result = engine.transcribe(path) if path else {"status": "audio_missing", "retryable": True}
            handle.write(json.dumps(attach_transcript(row, result), ensure_ascii=False) + "\n")
            handle.flush()
            summary["completed"] += 1
            status = result["status"]
            summary["counts"][status] = summary["counts"].get(status, 0) + 1
            atomic_json(summary_path, summary)
    summary["state"] = "complete"
    atomic_json(summary_path, summary)
    print(json.dumps({"state": summary["state"], "total": summary["total"], "counts": summary["counts"]}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.worker:
        engine = create_local_engine()
        for line in sys.stdin:
            try:
                value = json.loads(line)
                result = engine.transcribe(value["audio_path"])
            except Exception as exc:
                result = {"status": "unavailable", "retryable": True, "error_type": type(exc).__name__}
            print(json.dumps(result, ensure_ascii=True), flush=True)
    elif args.input and args.output and args.summary:
        run_batch(args.input, args.output, args.summary, args.limit)
    else:
        parser.error("Provide --worker or --input, --output and --summary")


if __name__ == "__main__":
    main()
