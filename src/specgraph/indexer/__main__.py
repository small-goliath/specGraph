"""indexer 데몬 진입점 — ``python -m specgraph.indexer [--once]`` / ``specgraph-indexer``.

폴링 주기는 ``SPECGRAPH_POLL_INTERVAL_SECONDS`` (AC7). ``--once`` 는 1회 동기화 후 종료한다
(AC12 측정용). PostgreSQL advisory lock 으로 인덱서 중복 실행을 막는다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from specgraph.errors import SpecGraphError
from specgraph.indexer.service import PollResult
from specgraph.log import configure_logging, log_event

logger = logging.getLogger("specgraph.indexer")


class _Pollable(Protocol):
    async def poll_once(self) -> PollResult: ...


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="specgraph-indexer", description=__doc__)
    parser.add_argument("--once", action="store_true", help="한 번 동기화하고 종료한다")
    return parser.parse_args(argv)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_loop(
    service: _Pollable,
    interval: float,
    *,
    once: bool,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    stop: asyncio.Event | None = None,
) -> int:
    stop = stop or asyncio.Event()
    sleeper = sleep or (lambda seconds: _wait(stop, seconds))
    while True:
        try:
            result = await service.poll_once()
        except Exception as exc:  # noqa: BLE001 — 데몬은 다음 주기에 재시도한다
            log_event(
                logger,
                "poll_crashed",
                logging.ERROR,
                exc_info=exc,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            result = PollResult(error=str(exc))
        if once:
            return 1 if (result.failed or result.error) else 0
        await sleeper(interval)
        if stop.is_set():
            return 0


async def amain(argv: Sequence[str] | None = None) -> int:
    from specgraph.indexer.git_source import GitSource
    from specgraph.indexer.service import IndexerService
    from specgraph.lightrag_store import LightRagStore
    from specgraph.llm import LlmCallCounter
    from specgraph.manifest import PostgresManifest
    from specgraph.settings import load_settings

    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_format, settings.log_level)

    manifest = await PostgresManifest.create(settings.postgres_dsn())
    try:
        if not await manifest.try_acquire_indexer_lock():
            log_event(logger, "lock_busy", logging.ERROR, detail="다른 인덱서가 실행 중")
            return 2
        counter = LlmCallCounter()
        store = await LightRagStore.create(settings, counter)
        try:
            service = IndexerService(
                git=GitSource(
                    repo_url=settings.repo_url,
                    cache_dir=settings.cache_dir,
                    token=settings.git_token,
                    branch_glob=settings.branch_glob,
                    timeout_seconds=settings.git_timeout_seconds,
                ),
                rag=store,
                manifest=manifest,
                counter=counter,
                include_dirs=settings.include_dirs,
                max_block_chars=settings.max_block_chars,
            )
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            log_event(
                logger,
                "indexer_start",
                once=args.once,
                poll_interval_seconds=settings.poll_interval_seconds,
                branch_glob=settings.branch_glob,
            )
            return await run_loop(
                service, settings.poll_interval_seconds, once=args.once, stop=stop
            )
        finally:
            await store.close()
    finally:
        await manifest.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except SpecGraphError as exc:
        print(f"specgraph-indexer: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
