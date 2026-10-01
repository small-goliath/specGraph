import asyncio
import logging

import pytest

from specgraph.indexer.__main__ import parse_args, run_loop
from specgraph.indexer.service import PollResult


class FakeService:
    def __init__(self, fail_times: int = 0):
        self.polls = 0
        self.fail_times = fail_times

    async def poll_once(self) -> PollResult:
        self.polls += 1
        if self.polls <= self.fail_times:
            raise RuntimeError("boom")
        return PollResult()


async def test_once_runs_single_poll():
    service = FakeService()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    code = await run_loop(service, interval=30, once=True, sleep=sleep)

    assert code == 0
    assert service.polls == 1
    assert sleeps == []


async def test_once_returns_nonzero_when_branch_failed():
    class Failing(FakeService):
        async def poll_once(self):
            self.polls += 1
            return PollResult(failed={"draft/a": "x"})

    assert await run_loop(Failing(), interval=1, once=True) == 1


async def test_loop_sleeps_poll_interval():
    service = FakeService()
    stop = asyncio.Event()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            stop.set()

    code = await run_loop(service, interval=42, once=False, sleep=sleep, stop=stop)

    assert code == 0
    assert service.polls == 3
    assert sleeps == [42, 42, 42]


async def test_loop_survives_unexpected_poll_exception():
    service = FakeService(fail_times=1)
    stop = asyncio.Event()

    async def sleep(seconds):
        if service.polls >= 2:
            stop.set()

    await run_loop(service, interval=1, once=False, sleep=sleep, stop=stop)

    assert service.polls == 2


async def test_unexpected_poll_exception_logged_with_traceback_and_type(caplog):
    caplog.set_level(logging.ERROR)

    code = await run_loop(FakeService(fail_times=1), interval=1, once=True)

    assert code == 1
    record = next(r for r in caplog.records if "event=poll_crashed" in r.getMessage())
    assert "error_type=RuntimeError" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError


@pytest.mark.parametrize(("argv", "once"), [([], False), (["--once"], True)])
def test_parse_args(argv, once):
    assert parse_args(argv).once is once
