"""Crash-safe Bluetooth Mesh sequence number reservations.

Mesh nodes replay-protect every network PDU. Reusing a sequence number under
the same IV Index makes a perfectly valid command look like a replay, so the
next block is persisted *before* any value from it is handed to the proxy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

MAX_SEQUENCE: Final = 0xFFFFFF
SEQUENCE_SPACE: Final = MAX_SEQUENCE + 1


class SequenceExhaustedError(RuntimeError):
    """The 24-bit sequence space is exhausted for the current IV Index."""


class SequenceReservation:
    """Reserve durable blocks while exposing the next safe in-memory value."""

    def __init__(
        self,
        next_sequence: int,
        reserved_until: int,
        block_size: int,
        save: Callable[[dict[str, int]], Awaitable[None]],
    ) -> None:
        self.next_sequence = next_sequence
        self._reserved_until = reserved_until
        self._block_size = block_size
        self._save = save

    @classmethod
    def create(
        cls,
        stored: Mapping[str, Any],
        save: Callable[[dict[str, int]], Awaitable[None]],
        *,
        block_size: int,
        minimum_sequence: int = 0,
    ) -> SequenceReservation:
        """Load a reservation; the first allocation reserves its block.

        Version 1 of the integration stored ``sequence`` as its last observed
        in-memory value and skipped one block at startup. Keep that extra skip
        for a safe, one-way migration to the new high-water representation.
        """
        if block_size < 1:
            raise ValueError("block_size must be positive")

        if "reserved_until" in stored:
            next_sequence = int(stored["reserved_until"])
        elif "sequence" in stored:
            next_sequence = int(stored["sequence"]) + block_size
        else:
            # Configuration immediately after provisioning uses the beginning
            # of the sequence space before a config entry (and its Store key)
            # exists. Start runtime traffic in the next block so those setup
            # messages can never be replayed after the first reconnect.
            minimum_sequence = max(minimum_sequence, block_size)
            next_sequence = (
                (minimum_sequence + block_size - 1) // block_size * block_size
            )

        reservation = cls(next_sequence, next_sequence, block_size, save)
        return reservation

    @property
    def reserved_until(self) -> int:
        """Exclusive upper bound already written to durable storage."""
        return self._reserved_until

    async def ensure_reserved(self, sequence: int) -> None:
        """Persist a block containing ``sequence`` before it can be used."""
        if sequence < 0 or sequence > MAX_SEQUENCE:
            raise SequenceExhaustedError(
                "Bluetooth Mesh sequence numbers are exhausted; re-provision "
                "the fixture to create a fresh mesh"
            )
        if sequence < self._reserved_until:
            return

        reserved_until = min(sequence + self._block_size, SEQUENCE_SPACE)
        # Only update memory after the write succeeds. If storage fails, the
        # caller must not send with an unreserved number.
        # Keep the legacy ``sequence`` field at the same conservative
        # high-water mark. Version 0.1 only understands that key and advances
        # it again on startup, so a component rollback remains replay-safe
        # after version 0.2 has transmitted messages.
        await self._save({"reserved_until": reserved_until, "sequence": reserved_until})
        self._reserved_until = reserved_until

    def mark_next(self, sequence: int) -> None:
        """Remember the proxy's next value for an in-process reconnect."""
        self.next_sequence = sequence
