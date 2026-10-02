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


class BlockingService:
    """poll_once 가 풀리지 않는 한 끝나지 않는다 — 취소되면 ``cancelled`` 를 기록한다."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False
        self.polls = 0

    async def poll_once(self) -> PollResult:
        self.polls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return PollResult()


async def _stop_after_poll_starts(service: BlockingService, stop: asyncio.Event) -> None:
    await service.started.wait()
    stop.set()


async def test_stop_during_poll_cancels_poll_and_returns():
    service = BlockingService()
    stop = asyncio.Event()
    signaller = asyncio.create_task(_stop_after_poll_starts(service, stop))

    code = await asyncio.wait_for(run_loop(service, interval=1, once=False, stop=stop), timeout=2)
    await signaller

    assert code == 0
    assert service.cancelled is True
    assert service.polls == 1


async def test_stop_during_poll_in_once_mode_returns_1():
    service = BlockingService()
    stop = asyncio.Event()
    signaller = asyncio.create_task(_stop_after_poll_starts(service, stop))

    code = await asyncio.wait_for(run_loop(service, interval=1, once=True, stop=stop), timeout=2)
    await signaller

    assert code == 1
    assert service.cancelled is True


async def test_stop_during_poll_logs_poll_cancelled(caplog):
    caplog.set_level(logging.INFO)
    service = BlockingService()
    stop = asyncio.Event()
    signaller = asyncio.create_task(_stop_after_poll_starts(service, stop))

    await asyncio.wait_for(run_loop(service, interval=1, once=False, stop=stop), timeout=2)
    await signaller

    assert any("event=poll_cancelled" in r.getMessage() for r in caplog.records)


async def test_stop_before_next_poll_still_returns_0():
    service = FakeService()
    stop = asyncio.Event()

    async def sleep(seconds):
        stop.set()

    code = await asyncio.wait_for(
        run_loop(service, interval=1, once=False, sleep=sleep, stop=stop), timeout=2
    )

    assert code == 0
    assert service.polls == 1


async def test_stop_already_set_does_not_start_poll():
    service = BlockingService()
    stop = asyncio.Event()
    stop.set()

    code = await asyncio.wait_for(run_loop(service, interval=1, once=False, stop=stop), timeout=2)

    assert code == 0
    assert service.polls == 0
    assert service.cancelled is False


class StopThenFinishService:
    """poll 이 끝나는 순간 종료 신호도 이미 set 이다 — 결과가 보존돼야 한다."""

    def __init__(self, stop: asyncio.Event, *, crash: bool = False, failed: bool = False):
        self.stop = stop
        self.crash = crash
        self.failed = failed

    async def poll_once(self) -> PollResult:
        self.stop.set()
        if self.crash:
            raise RuntimeError("boom-concurrent")
        return PollResult(failed={"draft/a": "x"}) if self.failed else PollResult()


async def test_poll_finishing_with_stop_set_keeps_result_in_once_mode():
    stop = asyncio.Event()

    code = await asyncio.wait_for(
        run_loop(StopThenFinishService(stop), interval=1, once=True, stop=stop), timeout=2
    )

    assert code == 0  # 결과 기반(성공). 취소 경로였다면 1


async def test_poll_finishing_with_stop_set_keeps_failed_result_in_once_mode(caplog):
    caplog.set_level(logging.INFO)
    stop = asyncio.Event()

    code = await asyncio.wait_for(
        run_loop(StopThenFinishService(stop, failed=True), interval=1, once=True, stop=stop),
        timeout=2,
    )

    assert code == 1
    assert not any("event=poll_cancelled" in r.getMessage() for r in caplog.records)


async def test_poll_crashing_with_stop_set_logs_poll_crashed(caplog):
    caplog.set_level(logging.INFO)
    stop = asyncio.Event()

    code = await asyncio.wait_for(
        run_loop(StopThenFinishService(stop, crash=True), interval=1, once=True, stop=stop),
        timeout=2,
    )

    messages = [r.getMessage() for r in caplog.records]
    assert code == 1
    assert any("event=poll_crashed" in m and "error_type=RuntimeError" in m for m in messages)
    assert not any("event=poll_cancelled" in m for m in messages)


class FailingCleanupService(BlockingService):
    """취소를 받으면 정리 중 CancelledError 가 아닌 다른 예외를 던진다."""

    async def poll_once(self) -> PollResult:
        self.polls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            self.cancelled = True
            raise ValueError("cleanup-boom") from exc
        return PollResult()


async def test_cancel_cleanup_failure_is_logged_and_shutdown_proceeds(caplog):
    caplog.set_level(logging.INFO)
    service = FailingCleanupService()
    stop = asyncio.Event()
    signaller = asyncio.create_task(_stop_after_poll_starts(service, stop))

    code = await asyncio.wait_for(run_loop(service, interval=1, once=False, stop=stop), timeout=2)
    await signaller

    messages = [r.getMessage() for r in caplog.records]
    assert code == 0
    assert any(
        "event=poll_cancel_cleanup_failed" in m and "error_type=ValueError" in m for m in messages
    )
    assert any("event=poll_cancelled" in m for m in messages)


async def test_cancelling_run_loop_itself_propagates_to_poll():
    service = BlockingService()
    task = asyncio.create_task(run_loop(service, interval=1, once=False))
    await asyncio.wait_for(service.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert service.cancelled is True


@pytest.mark.parametrize(("argv", "once"), [([], False), (["--once"], True)])
def test_parse_args(argv, once):
    assert parse_args(argv).once is once
