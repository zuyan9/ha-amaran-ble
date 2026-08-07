"""Crash-safe sequence reservation tests."""

from __future__ import annotations

import pytest
from amaranble.sequence import (
    MAX_SEQUENCE,
    SequenceExhaustedError,
    SequenceReservation,
)


@pytest.mark.asyncio
async def test_fresh_runtime_skips_configuration_block() -> None:
    saved: list[dict[str, int]] = []

    async def save(data: dict[str, int]) -> None:
        saved.append(data)

    reservation = SequenceReservation.create({}, save, block_size=512)

    assert reservation.next_sequence == 512
    assert reservation.reserved_until == 512
    assert saved == []
    await reservation.ensure_reserved(512)
    assert reservation.reserved_until == 1024
    assert saved == [{"reserved_until": 1024, "sequence": 1024}]


@pytest.mark.asyncio
async def test_restart_skips_every_previously_reserved_number() -> None:
    durable: dict[str, int] = {}

    async def save(data: dict[str, int]) -> None:
        durable.clear()
        durable.update(data)

    first = SequenceReservation.create(durable, save, block_size=4)
    assert first.next_sequence == 4
    await first.ensure_reserved(4)
    assert durable == {"reserved_until": 8, "sequence": 8}

    # Simulate a crash after using only the first value. The next process starts
    # at the old exclusive high-water mark, never at the last in-memory value.
    first.mark_next(5)
    second = SequenceReservation.create(durable, save, block_size=4)
    assert second.next_sequence == 8
    await second.ensure_reserved(8)
    assert durable == {"reserved_until": 12, "sequence": 12}


@pytest.mark.asyncio
async def test_legacy_store_migration_adds_safety_block() -> None:
    saved: list[dict[str, int]] = []

    async def save(data: dict[str, int]) -> None:
        saved.append(data)

    reservation = SequenceReservation.create({"sequence": 123}, save, block_size=512)

    assert reservation.next_sequence == 635
    await reservation.ensure_reserved(635)
    assert saved == [{"reserved_until": 1147, "sequence": 1147}]


@pytest.mark.asyncio
async def test_new_block_is_persisted_before_use() -> None:
    saved: list[dict[str, int]] = []

    async def save(data: dict[str, int]) -> None:
        saved.append(data)

    reservation = SequenceReservation.create({}, save, block_size=2)
    await reservation.ensure_reserved(2)
    await reservation.ensure_reserved(3)
    assert len(saved) == 1
    await reservation.ensure_reserved(4)
    assert saved[-1] == {"reserved_until": 6, "sequence": 6}


@pytest.mark.asyncio
async def test_failed_persistence_does_not_reserve_in_memory() -> None:
    calls = 0

    async def save(_data: dict[str, int]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")

    reservation = SequenceReservation.create({}, save, block_size=2)
    await reservation.ensure_reserved(2)
    with pytest.raises(OSError, match="disk full"):
        await reservation.ensure_reserved(4)
    assert reservation.reserved_until == 4


@pytest.mark.asyncio
async def test_sequence_exhaustion() -> None:
    async def save(_data: dict[str, int]) -> None:
        return None

    reservation = SequenceReservation.create(
        {"reserved_until": MAX_SEQUENCE}, save, block_size=2
    )
    await reservation.ensure_reserved(MAX_SEQUENCE)
    assert reservation.reserved_until == MAX_SEQUENCE + 1

    with pytest.raises(SequenceExhaustedError):
        await reservation.ensure_reserved(MAX_SEQUENCE + 1)
