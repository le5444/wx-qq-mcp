import asyncio
import io
import json
from pathlib import Path
import tempfile
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from unified_mcp.voice_transcription import (
    LocalVoiceASR, SenseVoiceASR, VoiceService, attach_transcript, audio_path_from_message,
    cached_transcript, is_voice, read_audio, MODEL,
)


class FakeModel:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.speech = True

    def transcribe(self, path, **options):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Temporary inference failure")
        segment = SimpleNamespace(start=0, end=0.5, text=" test speech ", avg_logprob=-0.3, no_speech_prob=0.01)
        info = SimpleNamespace(duration=0.5, language="zh", language_probability=0.99)
        return iter([segment] if self.speech else []), info


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "voice.wav"
        with wave.open(str(self.audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 8000)
        self.model = FakeModel()
        self.engine = LocalVoiceASR(self.root / "model", self.root / "cache", model_factory=lambda: self.model)

    def test_success_cached_and_source_unchanged(self):
        source = self.audio.read_bytes()
        first = self.engine.transcribe(self.audio)
        second = self.engine.transcribe(self.audio)
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["human_verified"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(source, self.audio.read_bytes())
        self.assertFalse(self.audio.with_suffix(".transcript.json").exists())

    def test_failure_retries_immediately_and_never_caches(self):
        self.model.fail = True
        failed = self.engine.transcribe(self.audio)
        self.assertEqual(failed["status"], "unavailable")
        self.assertTrue(failed["retryable"])
        self.assertIsNone(cached_transcript(self.audio, self.engine.cache, self.engine.model_path))
        self.model.fail = False
        self.assertEqual(self.engine.transcribe(self.audio)["status"], "ok")
        self.assertEqual(self.model.calls, 2)

    def test_changed_source_invalidates_cache(self):
        self.engine.transcribe(self.audio)
        with self.audio.open("ab") as handle:
            handle.write(b"\0\0")
        self.assertFalse(self.engine.transcribe(self.audio)["cache_hit"])
        self.assertEqual(self.model.calls, 2)

    def test_changed_model_invalidates_cache(self):
        self.engine.transcribe(self.audio)
        self.engine.model_path.mkdir()
        (self.engine.model_path / "model.bin").write_bytes(b"replacement")
        self.assertFalse(self.engine.transcribe(self.audio)["cache_hit"])

    def test_no_speech_distinct_from_missing_transcription(self):
        self.model.speech = False
        first = self.engine.transcribe(self.audio)
        second = self.engine.transcribe(self.audio)
        self.assertEqual(first["status"], "no_speech")
        self.assertEqual(first["text"], "")
        self.assertTrue(second["cache_hit"])

    def test_remote_audio_is_refused(self):
        for path in ("https://example.test/voice.wav", "\\\\server\\voice.wav", "//server/voice.wav"):
            with self.assertRaises(ValueError):
                read_audio(path)

    def test_exact_server_id_and_ambiguity(self):
        directory = self.root / "media/account"
        directory.mkdir(parents=True)
        correct = directory / ("voice-" + "a"*32 + "-12-12345-678-3000-abcd.silk")
        wrong = directory / ("voice-" + "a"*32 + "-678-12345-567-3000-abcd.silk")
        correct.write_bytes(b"audio")
        wrong.write_bytes(b"different")
        self.assertEqual(audio_path_from_message({"server_id_str": "678"}, self.root/"media"), correct)
        duplicate = directory / ("voice-" + "a"*32 + "-13-12346-678-3000-abcd.silk")
        duplicate.write_bytes(b"different")
        self.assertIsNone(audio_path_from_message({"server_id_str": "678"}, self.root/"media"))

    def test_same_audio_in_old_and_new_cache_filenames_resolves(self):
        directory = self.root / "media/account"
        directory.mkdir(parents=True)
        paths = [directory / ("voice-" + "a"*32 + "-12-12345-678-" + suffix + ".silk")
                 for suffix in ("3000-abcd", "abcd")]
        for path in paths:
            path.write_bytes(b"same audio")
        self.assertIn(audio_path_from_message({"server_id_str": "678", "local_id": 12, "create_time": 12345}, self.root/"media"), paths)
        self.assertIsNone(audio_path_from_message({"server_id_str": "678", "local_id": 13}, self.root/"media"))

    def test_existing_beam5_success_reused_by_greedy(self):
        result = self.engine.transcribe(self.audio, beam_size=5)
        self.assertEqual(result["beam_size"], 5)
        reused = self.engine.transcribe(self.audio, beam_size=1)
        self.assertTrue(reused["cache_hit"])
        self.assertEqual(reused["beam_size"], 5)
        self.assertEqual(self.model.calls, 1)

    def test_low_confidence_greedy_rechecked_using_beam5(self):
        original = self.model.transcribe
        beams = []
        def confidence(path, **options):
            beams.append(options["beam_size"])
            segments, info = original(path, **options)
            segment = next(segments)
            segment.avg_logprob = -1.2 if options["beam_size"] == 1 else -0.3
            return iter([segment]), info
        self.model.transcribe = confidence
        result = self.engine.transcribe(self.audio)
        self.assertEqual(beams, [1, 5])
        self.assertTrue(result["reviewed_with_beam5"])
        self.assertEqual(result["beam_size"], 5)
        cached = self.engine.transcribe(self.audio)
        self.assertTrue(cached["reviewed_with_beam5"])
        self.assertEqual(beams, [1, 5])

    def test_full_and_display_rows_identified(self):
        self.assertTrue(is_voice({"kind_name": "voice", "base_kind": 34}))
        self.assertTrue(is_voice({"kind": "voice", "voice": {"duration_ms": 500}}))
        self.assertTrue(is_voice({"voice_server_id_str": "123", "resource_family": "audio"}))
        self.assertFalse(is_voice({"kind": "quote", "text": "[语音]"}))

    def test_enrichment_preserves_original_and_marks_automatic(self):
        original = {"kind": "voice", "text": "[语音] 0.5s", "voice": {"duration_ms": 500}}
        attached = attach_transcript(original, self.engine.transcribe(self.audio))
        self.assertEqual(original["text"], "[语音] 0.5s")
        self.assertEqual(attached["original_voice_text"], original["text"])
        self.assertIn("本地自动识别", attached["text"])
        self.assertTrue(attached["voice"]["transcript"]["automatic"])

    def test_success_removes_only_stale_asr_warning(self):
        original = {"kind": "voice", "voice": {"duration_ms": 500,
                    "warnings": ["voice_transcription_unavailable", "other_warning"]},
                    "media_read_hints": [{"transcript": {"status": "unavailable"}}]}
        for status in ("ok", "no_speech"):
            attached = attach_transcript(original, {"status": status, "text": "test" if status=="ok" else ""})
            self.assertEqual(attached["voice"]["warnings"], ["other_warning"])
            self.assertEqual(attached["voice"]["duration_ms"], 500)
            self.assertEqual(attached["media_read_hints"][0]["transcript"]["status"], status)
        self.assertEqual(len(original["voice"]["warnings"]), 2)

    def test_failed_asr_preserves_warning_and_missing_voice_created(self):
        original = {"kind_name": "voice", "warnings": ["voice_transcription_unavailable"]}
        attached = attach_transcript(original, {"status": "unavailable"})
        self.assertEqual(attached["warnings"], original["warnings"])
        self.assertEqual(attached["voice"]["transcript"]["status"], "unavailable")


class SenseVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / "audio.wav"
        self.audio.write_bytes(b"test audio")
        self.calls = 0
        self.text = "test speech"
        self.scores = []
        owner = self
        class Samples:
            def __len__(self): return 16000
            def __mul__(self, other): return [0.01]
        class Stream:
            result = None
            def accept_waveform(self, rate, samples): pass
        class Recognizer:
            def create_stream(self): return Stream()
            def decode_stream(self, stream):
                owner.calls += 1
                stream.result = SimpleNamespace(text=owner.text, ys_log_probs=owner.scores)
        self.engine = SenseVoiceASR(self.root / "model", self.root / "cache", model_factory=Recognizer)
        self.engine._audio = lambda *args: (Samples(), 16000, self.audio)
        numpy = SimpleNamespace(sqrt=math.sqrt, mean=lambda values: sum(values)/len(values))
        mock = patch.dict("sys.modules", {"numpy": numpy})
        mock.start()
        self.addCleanup(mock.stop)

    def test_absent_ctc_confidence_is_not_fabricated_and_result_reused(self):
        first = self.engine.transcribe(self.audio)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["engine"], "sherpa-onnx-sensevoice-local")
        self.assertFalse(first["confidence_available"])
        self.assertIsNone(first["mean_token_logprob"])
        self.assertFalse(first["human_verified"])
        second = self.engine.transcribe(self.audio)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(self.calls, 1)

    def test_existing_whisper_results_remain_preferred(self):
        whisper = LocalVoiceASR(MODEL, self.engine.cache, model_factory=FakeModel, beam_size=5)
        self.assertEqual(whisper.transcribe(self.audio)["status"], "ok")
        second = self.engine.transcribe(self.audio)
        self.assertEqual(second["engine"], "faster-whisper-local")
        self.assertEqual(second["beam_size"], 5)
        self.assertEqual(self.calls, 0)

    def test_reviewer_failure_is_not_cached_as_final(self):
        self.scores = [-2]
        with patch("unified_mcp.voice_transcription.LocalVoiceASR") as reviewer:
            reviewer.return_value.transcribe.return_value = {"status": "unavailable", "retryable": True}
            first = self.engine.transcribe(self.audio)
            self.assertTrue(first["review_retryable"])
            self.assertIsNone(cached_transcript(self.audio, self.engine.cache, sense_model=self.engine.model_path))
        self.scores = []
        self.assertEqual(self.engine.transcribe(self.audio)["status"], "ok")
        self.assertEqual(self.calls, 2)


class VoiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_worker_released_without_closing_service(self):
        class Process:
            returncode = None
            killed = False
            def kill(self):
                self.killed = True
                self.returncode = -1
            async def wait(self):
                return self.returncode
        service = VoiceService(idle_seconds=0.02)
        process = service.process = Process()
        service._start_idle_watch()
        await asyncio.sleep(0.06)
        self.assertTrue(process.killed)
        self.assertIsNone(service.process)
        self.assertFalse(service.closed)
        await service.close()

    async def test_idle_watch_does_not_kill_active_inference(self):
        class Process:
            returncode = None
            killed = False
            def kill(self):
                self.killed = True
                self.returncode = -1
            async def wait(self):
                return self.returncode
        service = VoiceService(idle_seconds=0.02)
        process = service.process = Process()
        await service.lock.acquire()
        service._start_idle_watch()
        await asyncio.sleep(0.06)
        self.assertFalse(process.killed)
        service.last_used = __import__("time").monotonic()
        service.lock.release()
        await asyncio.sleep(0.005)
        self.assertFalse(process.killed)
        await service.close()

    async def test_close_does_not_wait_for_inference_lock(self):
        service = VoiceService()
        await service.lock.acquire()
        try:
            await asyncio.wait_for(service.close(), timeout=0.1)
        finally:
            service.lock.release()
        self.assertTrue(service.closed)
        result = await service.transcribe("unused.wav")
        self.assertEqual(result["error_type"], "ServiceClosed")

    async def test_missing_audio_and_unrelated_text(self):
        service = VoiceService()
        payload = {"messages": [{"kind": "voice", "id": {"server_id_str": "0"}},
                                {"kind": "text", "text": "ordinary"}]}
        enriched = await service.enrich(payload)
        self.assertEqual(enriched["messages"][0]["voice_transcript"]["status"], "audio_missing")
        self.assertEqual(enriched["messages"][1], payload["messages"][1])
        self.assertIsNone(service.process)

    async def test_worker_start_failure_is_retryable(self):
        service = VoiceService(python=Path("nonexistent-python"))
        with patch("unified_mcp.voice_transcription.cached_transcript", return_value=None):
            result = await service.transcribe("unused.wav")
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["retryable"])
        self.assertIsNone(service.process)


if __name__ == "__main__":
    unittest.main()
