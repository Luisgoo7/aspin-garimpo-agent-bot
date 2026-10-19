"""Robô sempre ligado: cliques em tempo real, coleta quando devida e passagem a outros jobs."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import worker
from app.config import settings


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t


@pytest.fixture
def daemon_env(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(settings, "cycle_timeout_seconds", 600)
    monkeypatch.setattr(settings, "daemon_queue_check_seconds", 120)
    polls = []

    async def poll(poll_seconds=0):
        polls.append(poll_seconds)
        clock.t += 30  # cada long polling dura ~30 s
        await asyncio.sleep(0)
        return {"clicks": 1, "approved": 1, "discarded": 0, "sheet_ok": 1}
    monkeypatch.setattr(worker, "process_buttons", poll)
    monkeypatch.setattr(worker, "redispatch_daemon", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "queued_runs", AsyncMock(return_value=[]))
    return clock, polls


@pytest.mark.asyncio
async def test_daemon_listens_runs_due_cycle_and_restarts_itself(monkeypatch, daemon_env):
    clock, polls = daemon_env
    reasons = iter(["coleta atrasada"] + [None] * 1000)
    monkeypatch.setattr(worker, "collector_dispatch_reason", lambda: next(reasons))
    cycles = []

    async def cycle(reason):
        cycles.append(reason)
        return {"outcome": "success"}
    monkeypatch.setattr(worker, "_daemon_cycle", cycle)
    totals = await worker.run_daemon(max_minutes=20)
    assert cycles == ["coleta atrasada"] and totals["cycles"] == 1
    assert polls and all(p == settings.daemon_poll_seconds for p in polls)
    assert totals["approved"] == len(polls) and totals["redispatched"] is True
    worker.redispatch_daemon.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_yields_to_manual_job_after_finishing_cycle(monkeypatch, daemon_env):
    clock, polls = daemon_env
    monkeypatch.setattr(worker, "collector_dispatch_reason", lambda: None)
    monkeypatch.setattr(worker, "queued_runs", AsyncMock(return_value=[{"id": 9, "path": ".github/workflows/garimpo.yml"}]))
    totals = await worker.run_daemon(max_minutes=300)
    assert totals["minutes"] < 10  # deu passagem logo, sem esperar as 5 horas


@pytest.mark.asyncio
async def test_daemon_does_not_yield_to_its_own_next_run(monkeypatch, daemon_env):
    clock, polls = daemon_env
    monkeypatch.setattr(worker, "collector_dispatch_reason", lambda: None)
    monkeypatch.setattr(worker, "queued_runs", AsyncMock(return_value=[{"id": 9, "path": ".github/workflows/robo.yml"}]))
    totals = await worker.run_daemon(max_minutes=15)
    assert totals["minutes"] >= 15


@pytest.mark.asyncio
async def test_daemon_survives_telegram_errors(monkeypatch, daemon_env):
    clock, polls = daemon_env
    monkeypatch.setattr(worker, "collector_dispatch_reason", lambda: None)
    monkeypatch.setattr(worker.asyncio, "sleep", AsyncMock())

    async def broken(poll_seconds=0):
        clock.t += 30
        raise RuntimeError("Telegram: falha de conexão")
    monkeypatch.setattr(worker, "process_buttons", broken)
    totals = await worker.run_daemon(max_minutes=5)
    assert totals["errors"] > 0 and totals["redispatched"] is True


@pytest.mark.asyncio
async def test_redispatch_skips_when_next_run_already_queued(monkeypatch):
    monkeypatch.setattr(worker, "queued_runs", AsyncMock(return_value=[{"id": 1, "path": ".github/workflows/robo.yml"}]))
    github = AsyncMock()
    monkeypatch.setattr(worker, "_github", github)
    assert await worker.redispatch_daemon() is False
    github.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_polling_passes_timeout_to_telegram(monkeypatch):
    from app import telegram_bot
    seen = {}

    async def call(method, payload, timeout=20, require_result=True):
        seen.update(method=method, payload=payload, timeout=timeout)
        return []
    monkeypatch.setattr(telegram_bot, "_call", call)
    await telegram_bot.get_updates(5, poll_seconds=25)
    assert seen["payload"]["timeout"] == 25 and seen["timeout"] > 25 and seen["payload"]["offset"] == 5
