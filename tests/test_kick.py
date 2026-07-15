import asyncio
import shutil
import tempfile
from datetime import datetime

import pytest

import ride_dispatch.bot as bot


@pytest.fixture
def sock_dir(monkeypatch):
    # macOS caps AF_UNIX socket paths at 104 chars; pytest's tmp_path
    # (/private/var/folders/...) easily exceeds that, so use a short /tmp dir.
    d = tempfile.mkdtemp(prefix="rdkick-", dir="/tmp")
    monkeypatch.setattr(bot, "DB_PATH", d + "/orders.db")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_kick_server_resets_poll(sock_dir):

    scheduled = []

    class FakeJobQueue:
        def run_once(self, fn, when, job_kwargs=None):
            scheduled.append(fn)

    class FakeApp:
        job_queue = FakeJobQueue()

    async def scenario():
        bot._next_poll_at = datetime(2099, 1, 1)
        await bot._start_kick_server(FakeApp())
        try:
            _, writer = await asyncio.open_unix_connection(bot._sock_path())
            writer.write(b"kick\n")
            await writer.drain()
            writer.close()
            for _ in range(100):
                if bot._next_poll_at is None:
                    break
                await asyncio.sleep(0.01)
            assert bot._next_poll_at is None
            assert scheduled == [bot._poll_tick]
        finally:
            bot._kick_server.close()
            await bot._kick_server.wait_closed()

    asyncio.run(scenario())


def test_kick_server_ignores_garbage(sock_dir):

    class FakeApp:
        class job_queue:
            @staticmethod
            def run_once(fn, when, job_kwargs=None):
                raise AssertionError("must not schedule on garbage input")

    async def scenario():
        bot._next_poll_at = datetime(2099, 1, 1)
        await bot._start_kick_server(FakeApp())
        try:
            _, writer = await asyncio.open_unix_connection(bot._sock_path())
            writer.write(b"hello\n")
            await writer.drain()
            writer.close()
            await asyncio.sleep(0.1)
            assert bot._next_poll_at is not None
        finally:
            bot._kick_server.close()
            await bot._kick_server.wait_closed()

    asyncio.run(scenario())
