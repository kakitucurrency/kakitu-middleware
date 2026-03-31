import asyncio
import uuid
import logging
import time
from dataclasses import dataclass, field
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
    kshs_earned: float = 0.0


class WorkerPool:
    def __init__(self, timeout: float = 30.0):
        self.workers: Dict[str, Worker] = {}
        self._pending: Dict[str, tuple] = {}  # hash → (future, difficulty)
        self._winners: Dict[str, str] = {}    # hash → winning ws_id
        self._timeout = timeout

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
        Push work task to all connected workers.
        Returns first valid work string, or None if pool empty or timeout.
        """
        if not self.workers:
            return None

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[hash] = (future, difficulty)

        task_msg = {'action': 'work', 'hash': hash, 'difficulty': difficulty}
        for worker in list(self.workers.values()):
            try:
                await worker.ws.send_json(task_msg)
            except Exception as e:
                logger.warning(f"Failed to send task to {worker.ws_id}: {e}")

        try:
            result = await asyncio.wait_for(future, timeout=self._timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Worker pool timed out for hash {hash}")
            return None
        finally:
            self._pending.pop(hash, None)
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
        entry = self._pending.get(hash)
        if entry is None:
            return False
        future, req_difficulty = entry
        if future.done():
            return False

        try:
            if nanolib is not None:
                nanolib.validate_work(hash, work, difficulty=req_difficulty)
        except Exception as e:
            logger.warning(f"Invalid work from {ws_id}: {e}")
            return False

        try:
            future.set_result(work)
            self._winners[hash] = ws_id
            return True
        except asyncio.InvalidStateError:
            logger.debug(f"Future already resolved for hash {hash}")
            return False

    @property
    def count(self) -> int:
        return len(self.workers)
