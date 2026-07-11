import asyncio

import pytest

from app.config import Settings
from app.speech import SpeechScheduler, SpeechRequest, QueueFullError


def test_initial_queue_policy_is_bounded_single_worker_friendly():
    settings = Settings()
    assert settings.max_queue_size == 3
    assert settings.cancel_on_new_request is False
    assert settings.cancel_on_client_disconnect is True


def test_single_worker_queue_rejects_when_capacity_is_full():
    queue = SpeechScheduler(max_size=1)

    async def scenario():
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker():
            blocker_started.set()
            await release_blocker.wait()

        first = asyncio.create_task(
            queue.run(SpeechRequest(synthesis_id="s1", connection_id="c1", text="test"), blocker))
        await blocker_started.wait()

        with pytest.raises(QueueFullError, match="Queue full"):
            await queue.run(SpeechRequest(synthesis_id="s2", connection_id="c2", text="test"),
                           lambda: asyncio.sleep(0))

        release_blocker.set()
        await first
        assert queue.snapshot()["pending"] == 0

    asyncio.run(scenario())
