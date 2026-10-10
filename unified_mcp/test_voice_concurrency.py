"""Multi-client voice request behavior without a real model or subprocess."""

import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unified_mcp.voice_transcription import VoiceService


class Harness(VoiceService):
    def __init__(self, **args):
        super().__init__(**args)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.cancellations = 0
        self.fail = False

    async def _transcribe_locked(self, path):
        self.calls.append(Path(path).read_bytes())
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            raise
        if self.fail:
            raise RuntimeError("synthetic temporary worker failure")
        return {"status": "ok", "text": "synthetic recognized words", "automatic": True,
                "audio_sha256": hashlib.sha256(self.calls[-1]).hexdigest(),
                "segments": [{"text": "synthetic recognized words"}]}


class VoiceConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wxqq-voice-concurrency-")
        self.folder = Path(self.temp.name)
        self.a, self.alias, self.b, self.c = [self.folder / name for name in ("a.wav", "alias.wav", "b.wav", "c.wav")]
        for file, data in ((self.a, b"same audio"), (self.alias, b"same audio"), (self.b, b"second audio"), (self.c, b"third audio")):
            file.write_bytes(data)
        self.cache_patch = patch("unified_mcp.voice_transcription.cached_transcript", return_value=None)
        self.cache_patch.start()
        self.service = Harness()
        self.tasks = []

    async def asyncTearDown(self):
        await self.service.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.cache_patch.stop()
        self.temp.cleanup()

    def request(self, path):
        task = asyncio.create_task(self.service.transcribe(path))
        self.tasks.append(task)
        return task

    async def wait_until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(poll(), timeout=2)

    async def test_same_audio_in_two_paths_is_one_job_and_returns_independent_copies(self):
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        second = self.request(self.alias)
        await self.wait_until(lambda: self.service.status()["deduplicated_requests"] == 1)
        self.assertEqual(self.service.status()["inflight_jobs"], 1)
        self.service.release.set()
        left, right = await asyncio.gather(first, second)
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(left, right)
        left["segments"][0]["text"] = "client-only change"
        self.assertEqual(right["segments"][0]["text"], "synthetic recognized words")

    async def test_one_client_cancel_does_not_abort_another_waiter(self):
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        second = self.request(self.a)
        await self.wait_until(lambda: self.service.status()["deduplicated_requests"] == 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(self.service.cancellations, 0)
        self.assertEqual(self.service.status()["request_waiters"], 1)
        self.service.release.set()
        self.assertEqual((await second)["status"], "ok")
        self.assertEqual(len(self.service.calls), 1)

    async def test_final_waiter_cancel_releases_inference_and_allows_retry(self):
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await self.wait_until(lambda: self.service.cancellations == 1)
        self.service.release.set()
        self.assertEqual((await self.service.transcribe(self.a))["status"], "ok")
        self.assertEqual(len(self.service.calls), 2)

    async def test_distinct_jobs_are_bounded_but_duplicate_can_join_full_queue(self):
        await self.service.close()
        self.service = Harness(max_pending=2)
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        second = self.request(self.b)
        await self.wait_until(lambda: self.service.status()["inflight_jobs"] == 2)
        rejected = await self.service.transcribe(self.c)
        self.assertEqual(rejected["error_type"], "QueueFull")
        self.assertTrue(rejected["retryable"])
        duplicate = self.request(self.alias)
        await self.wait_until(lambda: self.service.status()["deduplicated_requests"] == 1)
        status = self.service.status()
        self.assertEqual((status["active_jobs"], status["queued_jobs"]), (1, 1))
        self.service.release.set()
        self.assertTrue(all(r["status"] == "ok" for r in await asyncio.gather(first, second, duplicate)))
        self.assertEqual(self.service.calls, [b"same audio", b"second audio"])

    async def test_waiter_limit_prevents_unbounded_same_audio_clients(self):
        await self.service.close()
        self.service = Harness(max_waiters=2)
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        second = self.request(self.alias)
        await self.wait_until(lambda: self.service.status()["deduplicated_requests"] == 1)
        self.assertEqual((await self.service.transcribe(self.a))["error_type"], "QueueFull")
        self.service.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.service.status()["request_waiters"], 0)

    async def test_cancel_a_queued_job_leaves_active_job_and_capacity_intact(self):
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        queued = self.request(self.b)
        await self.wait_until(lambda: self.service.status()["queued_jobs"] == 1)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertEqual(self.service.cancellations, 0)
        self.service.release.set()
        self.assertEqual((await first)["status"], "ok")
        self.assertEqual(self.service.calls, [b"same audio"])

    async def test_failure_is_retryable_and_not_left_as_an_inflight_cache(self):
        self.service.release.set()
        self.service.fail = True
        self.assertEqual((await self.service.transcribe(self.a))["status"], "unavailable")
        self.service.fail = False
        self.assertEqual((await self.service.transcribe(self.a))["status"], "ok")
        self.assertEqual(len(self.service.calls), 2)

    async def test_changed_audio_while_queued_is_not_recognized_for_old_identity(self):
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        queued = self.request(self.b)
        await self.wait_until(lambda: self.service.status()["queued_jobs"] == 1)
        self.b.write_bytes(b"replaced audio while waiting")
        self.service.release.set()
        self.assertEqual((await queued)["error_type"], "AudioChanged")
        await first
        self.assertEqual(self.service.calls, [b"same audio"])

    async def test_shutdown_cancels_owned_jobs_and_refuses_new_requests(self):
        first = self.request(self.a)
        await asyncio.wait_for(self.service.started.wait(), 2)
        second = self.request(self.b)
        await self.wait_until(lambda: self.service.status()["queued_jobs"] == 1)
        await asyncio.wait_for(self.service.close(), timeout=2)
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(r, asyncio.CancelledError) for r in outcomes))
        self.assertEqual((await self.service.transcribe(self.c))["error_type"], "ServiceClosed")
        self.assertTrue(self.service.status()["closed"])
        self.assertEqual(self.service.status()["active_jobs"], 0)

    async def test_status_does_not_load_worker_or_expose_audio_paths(self):
        with patch.object(self.service, "_prepare", side_effect=AssertionError("status must not read audio")):
            status = self.service.status()
        self.assertEqual(status["inflight_jobs"], 0)
        self.assertFalse(status["worker_running"])
        self.assertNotIn(str(self.folder), str(status))

    async def test_worker_result_for_changed_audio_is_not_shared_as_original(self):
        async def changed_audio(_):
            return {"status": "ok", "text": "words from replacement content", "audio_sha256": "different-content"}
        with patch.object(self.service, "_transcribe_locked", side_effect=changed_audio):
            result = await self.service.transcribe(self.a)
        self.assertEqual(result["error_type"], "AudioChanged")
        self.assertNotIn("text", result)


if __name__ == "__main__":
    unittest.main()
