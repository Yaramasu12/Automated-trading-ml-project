from __future__ import annotations

import asyncio


class InstrumentLockManager:
    """One asyncio.Lock per instrument symbol.

    Prevents concurrent broker submissions for the same instrument, eliminating
    duplicate-fill races without any external coordination.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def acquire(self, symbol: str) -> asyncio.Lock:
        async with self._meta_lock:
            if symbol not in self._locks:
                self._locks[symbol] = asyncio.Lock()
        lock = self._locks[symbol]
        await lock.acquire()
        return lock

    def release(self, symbol: str) -> None:
        lock = self._locks.get(symbol)
        if lock and lock.locked():
            lock.release()

    def is_locked(self, symbol: str) -> bool:
        lock = self._locks.get(symbol)
        return lock is not None and lock.locked()

    @property
    def locked_symbols(self) -> list[str]:
        return [sym for sym, lock in self._locks.items() if lock.locked()]
