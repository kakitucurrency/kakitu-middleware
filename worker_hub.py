import asyncio
import uuid
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional

try:
    import nanolib
except ImportError:
    nanolib = None  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class Worker:
    ws_id: str
    ws: object
    kshs_address: str
    connected_at: float = field(default_factory=time.time)
    work_completed: int = 0
    kshs_earned: Decimal = field(default_factory=lambda: Decimal('0'))


class WorkerPool:
    def __init__(self, timeout: float = 30.0):
        self.workers: Dict[str, Worker] = {}
        self._pending: Dict[str, asyncio.Future] = {}  # hash → future (in-flight dispatches)
        self._pending_difficulty: Dict[str, str] = {}   # hash → difficulty
        self._winners: Dict[str, str] = {}    # hash → winning ws_id
        self._timeout = timeout
        self._pending_lock = asyncio.Lock()  # short lock around _pending dict mutations only

    async def add(self, ws, kshs_address: str) -> str:
        ws_id = str(uuid.uuid4())
        self.workers[ws_id] = Worker(ws_id=ws_id, ws=ws, kshs_address=kshs_address)
        logger.info(f"Worker connected: {ws_id} ({kshs_address})")
        return ws_id

    async def remove(self, ws_id: str):
        self.workers.pop(ws_id, None)
        logger.info(f"Worker disconnected: {ws_id}")

    async def dispatch(self, hash: str, difficulty: str) -> Optional[str]:
        """
        Push work task to connected workers.  Multiple different hashes
        can be computed concurrently.  If the same hash is already being
        dispatched, callers coalesce onto the existing future.

        A short lock (_pending_lock) is held only while mutating the
        _pending dict -- never during the full wait_for duration.
        """
        if not self.workers:
            return None

        # Fast path: if this hash is already in flight, just await it
        async with self._pending_lock:
            existing = self._pending.get(hash)
            if existing is not None:
                future = existing
            else:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                self._pending[hash] = future
                self._pending_difficulty[hash] = difficulty

        # Only broadcast to workers if we created a new future
        if existing is None:
            task_msg = {'action': 'work', 'hash': hash, 'difficulty': difficulty}
            for worker in list(self.workers.values()):
                try:
                    await worker.ws.send_json(task_msg)
                except Exception as e:
                    logger.warning(f"Failed to send task to {worker.ws_id}: {e}")
                    # Fix 4: remove dead worker on send failure
                    self.workers.pop(worker.ws_id, None)
                    logger.info(f"Removed dead worker {worker.ws_id} from pool after send failure")

        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=self._timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Worker pool timed out for hash {hash}")
            return None
        finally:
            # Clean up only if we are the originator of this future
            async with self._pending_lock:
                if self._pending.get(hash) is future:
                    self._pending.pop(hash, None)
                    self._pending_difficulty.pop(hash, None)
            winner_id = self._winners.pop(hash, None)
            cancel_msg = {'action': 'cancel', 'hash': hash}
            for worker in list(self.workers.values()):
                if worker.ws_id == winner_id:
                    continue
                try:
                    await worker.ws.send_json(cancel_msg)
                except Exception:
                    pass

    async def submit(self, ws_id: str, hash: str, work: str) -> bool:
        """
        Called when a worker submits a result.
        Validates work via nanolib. Resolves the pending Future if first valid result.
        Returns True if this worker won, False otherwise.
        """
        future = self._pending.get(hash)
        if future is None:
            return False
        if future.done():
            return False

        req_difficulty = self._pending_difficulty.get(hash)
        try:
            if nanolib is not None and req_difficulty is not None:
                nanolib.validate_work(hash, work, difficulty=req_difficulty)
        except Exception as e:
            logger.warning(f"Invalid work from {ws_id}: {e}")
            return False

        try:
            self._winners[hash] = ws_id  # set before resolving to avoid race in dispatch's finally
            future.set_result(work)
            return True
        except asyncio.InvalidStateError:
            logger.debug(f"Future already resolved for hash {hash}")
            return False

    @property
    def count(self) -> int:
        return len(self.workers)
