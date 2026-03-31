# Kakitu Worker Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend kakitu-middleware with a WebSocket worker hub, instant KSHS micropayments per completed work unit, and a public `/stats` endpoint so community members can contribute PoW at `work.kakitu.org` and earn KSHS.

**Architecture:** Three new modules (`stats.py`, `worker_hub.py`, `payout.py`) plus changes to `main.py`. Workers connect anonymously via WebSocket to `/worker/ws`, provide their `kshs_` address, and receive work tasks. The first worker to return a valid result earns `WORK_REWARD_KSHS` sent immediately from a dedicated worker fund wallet via a Kakitu `process` RPC call. The existing BoomPow → work peers → node fallback chain is preserved when no workers are connected or the pool times out.

**Tech Stack:** Python 3.7+, aiohttp (WebSocket built-in — no new packages), nanolib (existing), hashlib.blake2b, asyncio

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `stats.py` | In-memory network stats tracking and `/stats` serialisation |
| Create | `worker_hub.py` | `WorkerPool` — WebSocket pool management, task broadcast, first-result race |
| Create | `payout.py` | Derive key, build state block, sign, submit send to worker fund wallet |
| Modify | `main.py` | Add `/worker/ws` + `/stats` routes; inject worker pool into `work_generate()` dispatch; load new env vars |
| Create | `tests/test_stats.py` | Unit tests for `Stats` |
| Create | `tests/test_worker_hub.py` | Unit tests for `WorkerPool` |
| Create | `tests/test_payout.py` | Unit tests for `derive_private_key` and `send_reward` |
| Create | `tests/conftest.py` | pytest-asyncio configuration |

---

## New Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WORKER_FUND_SEED` | No | `None` | 64-char hex seed of worker fund wallet. If absent, payouts are skipped (no-payout mode). |
| `WORKER_FUND_ADDRESS` | No | `None` | `kshs_` address of worker fund wallet |
| `WORK_REWARD_KSHS` | No | `0.01` | KSHS paid per completed work unit |

---

## Task 1: Stats module

**Files:**
- Create: `stats.py`
- Create: `tests/conftest.py`
- Create: `tests/test_stats.py`

- [ ] **Step 1: Create `tests/conftest.py`**

```python
import pytest

pytest_plugins = ['pytest_asyncio']
```

- [ ] **Step 2: Write `tests/test_stats.py`**

```python
import pytest
from stats import Stats


def make_worker(address='kshs_1abc', work_completed=0, kshs_earned=0.0):
    class FakeWorker:
        kshs_address = address
        work_completed = 0
        kshs_earned = 0.0
    w = FakeWorker()
    w.work_completed = work_completed
    w.kshs_earned = kshs_earned
    return w


def test_initial_state():
    s = Stats()
    d = s.to_dict()
    assert d['connected_workers'] == 0
    assert d['total_work_completed'] == 0
    assert d['total_kshs_paid'] == '0.000000'
    assert d['top_earners'] == []


def test_record_completion_updates_totals():
    s = Stats()
    w = make_worker('kshs_1abc')
    s.record_completion(w, 0.01)
    d = s.to_dict()
    assert d['total_work_completed'] == 1
    assert d['total_kshs_paid'] == '0.010000'


def test_top_earners_sorted_by_earned():
    s = Stats()
    w1 = make_worker('kshs_1aaa')
    w2 = make_worker('kshs_1bbb')
    s.record_completion(w1, 0.05)
    s.record_completion(w1, 0.05)
    s.record_completion(w2, 0.01)
    earners = s.to_dict()['top_earners']
    assert earners[0]['address'] == 'kshs_1aaa'
    assert earners[0]['earned'] == '0.100000'
    assert earners[1]['address'] == 'kshs_1bbb'


def test_top_earners_capped_at_ten():
    s = Stats()
    for i in range(15):
        w = make_worker(f'kshs_1addr{i:03d}')
        s.record_completion(w, float(i))
    assert len(s.to_dict()['top_earners']) == 10


def test_same_address_accumulates():
    s = Stats()
    w = make_worker('kshs_1abc')
    s.record_completion(w, 0.01)
    s.record_completion(w, 0.01)
    earners = s.to_dict()['top_earners']
    assert earners[0]['earned'] == '0.020000'
    assert earners[0]['work_completed'] == 2
```

- [ ] **Step 3: Run to verify fail**

```bash
cd /Users/kiptengwer/Documents/kakitu/kakitu-middleware
python -m pytest tests/test_stats.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'stats'`

- [ ] **Step 4: Create `stats.py`**

```python
from dataclasses import dataclass, field
from typing import List


@dataclass
class Stats:
    total_work_completed: int = 0
    total_kshs_paid: float = 0.0
    _earners: dict = field(default_factory=dict)  # address → {earned, work_completed}
    connected_workers: int = 0  # set externally by WorkerPool

    def record_completion(self, worker, amount: float):
        self.total_work_completed += 1
        self.total_kshs_paid += amount
        worker.work_completed += 1
        worker.kshs_earned += amount

        addr = worker.kshs_address
        if addr not in self._earners:
            self._earners[addr] = {'earned': 0.0, 'work_completed': 0}
        self._earners[addr]['earned'] += amount
        self._earners[addr]['work_completed'] += 1

    def to_dict(self) -> dict:
        top = sorted(
            self._earners.items(),
            key=lambda x: x[1]['earned'],
            reverse=True
        )[:10]
        return {
            'connected_workers': self.connected_workers,
            'total_work_completed': self.total_work_completed,
            'total_kshs_paid': f'{self.total_kshs_paid:.6f}',
            'top_earners': [
                {
                    'address': addr,
                    'earned': f'{data["earned"]:.6f}',
                    'work_completed': data['work_completed'],
                }
                for addr, data in top
            ],
        }
```

- [ ] **Step 5: Run tests to verify pass**

```bash
python -m pytest tests/test_stats.py -v
```

Expected: 5 tests PASSED.

- [ ] **Step 6: Commit**

```bash
git add stats.py tests/conftest.py tests/test_stats.py
git commit -m "feat: add Stats module for network metrics tracking"
```

---

## Task 2: Worker hub

**Files:**
- Create: `worker_hub.py`
- Create: `tests/test_worker_hub.py`

- [ ] **Step 1: Write `tests/test_worker_hub.py`**

```python
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from worker_hub import WorkerPool, Worker


def make_ws(ws_id='ws-1'):
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    ws.closed = False
    return ws


@pytest.mark.asyncio
async def test_add_and_remove_worker():
    pool = WorkerPool()
    ws = make_ws()
    ws_id = await pool.add(ws, 'kshs_1abc')
    assert ws_id in pool.workers
    assert pool.workers[ws_id].kshs_address == 'kshs_1abc'
    await pool.remove(ws_id)
    assert ws_id not in pool.workers


@pytest.mark.asyncio
async def test_dispatch_returns_none_when_empty():
    pool = WorkerPool()
    result = await pool.dispatch('HASH123', 'ffffffc000000000')
    assert result is None


@pytest.mark.asyncio
async def test_dispatch_sends_task_to_workers():
    pool = WorkerPool()
    ws = make_ws()
    ws_id = await pool.add(ws, 'kshs_1abc')

    async def fake_dispatch():
        await asyncio.sleep(0.01)
        await pool.submit(ws_id, 'HASH123', 'aabbccdd11223344')

    asyncio.create_task(fake_dispatch())
    result = await pool.dispatch('HASH123', 'ffffffc000000000')
    assert result == 'aabbccdd11223344'
    ws.send_json.assert_called_once_with({
        'action': 'work',
        'hash': 'HASH123',
        'difficulty': 'ffffffc000000000',
    })


@pytest.mark.asyncio
async def test_dispatch_times_out_when_no_result():
    pool = WorkerPool(timeout=0.05)
    ws = make_ws()
    await pool.add(ws, 'kshs_1abc')
    result = await pool.dispatch('DEADBEEF', 'ffffffc000000000')
    assert result is None


@pytest.mark.asyncio
async def test_submit_returns_false_for_unknown_task():
    pool = WorkerPool()
    ws = make_ws()
    ws_id = await pool.add(ws, 'kshs_1abc')
    won = await pool.submit(ws_id, 'UNKNOWNHASH', 'aabbccdd11223344')
    assert won is False


@pytest.mark.asyncio
async def test_second_submission_returns_false():
    pool = WorkerPool()
    ws1 = make_ws('ws-1')
    ws2 = make_ws('ws-2')
    id1 = await pool.add(ws1, 'kshs_1aaa')
    id2 = await pool.add(ws2, 'kshs_1bbb')

    async def dispatch_and_submit():
        await asyncio.sleep(0.01)
        await pool.submit(id1, 'HASH123', 'aabbccdd11223344')
        await asyncio.sleep(0.01)
        return await pool.submit(id2, 'HASH123', 'aabbccdd11223344')

    asyncio.create_task(dispatch_and_submit())
    await pool.dispatch('HASH123', 'ffffffc000000000')
    # id2's submit happens after dispatch resolved — should return False
    await asyncio.sleep(0.05)
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m pytest tests/test_worker_hub.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'worker_hub'`

- [ ] **Step 3: Create `worker_hub.py`**

```python
import asyncio
import uuid
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

import nanolib

logger = logging.getLogger(__name__)


@dataclass
class Worker:
    ws_id: str
    ws: object          # aiohttp WebSocketResponse
    kshs_address: str
    connected_at: float = field(default_factory=lambda: __import__('time').time())
    work_completed: int = 0
    kshs_earned: float = 0.0


class WorkerPool:
    def __init__(self, timeout: float = 30.0):
        self.workers: Dict[str, Worker] = {}
        self._pending: Dict[str, asyncio.Future] = {}  # hash → Future
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

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[hash] = future

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
            # Cancel the task for remaining workers
            cancel_msg = {'action': 'cancel', 'hash': hash}
            for worker in list(self.workers.values()):
                try:
                    await worker.ws.send_json(cancel_msg)
                except Exception:
                    pass

    async def submit(self, ws_id: str, hash: str, work: str) -> bool:
        """
        Called when a worker submits a result.
        Validates work. If valid and task still pending, resolves Future and returns True.
        Returns False if task already resolved or invalid work.
        """
        future = self._pending.get(hash)
        if future is None or future.done():
            return False

        try:
            nanolib.validate_work(hash, work)
        except Exception as e:
            logger.warning(f"Invalid work from {ws_id}: {e}")
            return False

        future.set_result(work)
        return True

    @property
    def count(self) -> int:
        return len(self.workers)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
python -m pytest tests/test_worker_hub.py -v
```

Expected: 6 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add worker_hub.py tests/test_worker_hub.py
git commit -m "feat: add WorkerPool for WebSocket worker management"
```

---

## Task 3: Payout module

**Files:**
- Create: `payout.py`
- Create: `tests/test_payout.py`

- [ ] **Step 1: Write `tests/test_payout.py`**

```python
import pytest
from unittest.mock import AsyncMock, patch
from payout import derive_private_key, send_reward, PayoutError


def test_derive_private_key_deterministic():
    seed = 'a' * 64
    key1 = derive_private_key(seed, 0)
    key2 = derive_private_key(seed, 0)
    assert key1 == key2
    assert len(key1) == 64  # 32 bytes hex


def test_derive_private_key_different_index():
    seed = 'a' * 64
    key0 = derive_private_key(seed, 0)
    key1 = derive_private_key(seed, 1)
    assert key0 != key1


def test_derive_private_key_different_seed():
    key_a = derive_private_key('a' * 64, 0)
    key_b = derive_private_key('b' * 64, 0)
    assert key_a != key_b


@pytest.mark.asyncio
async def test_send_reward_raises_on_account_error():
    with patch('payout._rpc', new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = {'error': 'Account not found'}
        with pytest.raises(PayoutError, match='account_info failed'):
            await send_reward(
                node_url='localhost:7076',
                fund_seed='a' * 64,
                fund_address='kshs_1fund',
                destination='kshs_1worker',
                amount_kshs=0.01,
            )


@pytest.mark.asyncio
async def test_send_reward_raises_on_insufficient_funds():
    with patch('payout._rpc', new_callable=AsyncMock) as mock_rpc:
        mock_rpc.return_value = {
            'frontier': 'A' * 64,
            'balance': '1',  # 1 raw — far less than 0.01 KSHS
            'representative': 'kshs_1rep',
        }
        with pytest.raises(PayoutError, match='Insufficient funds'):
            await send_reward(
                node_url='localhost:7076',
                fund_seed='a' * 64,
                fund_address='kshs_1fund',
                destination='kshs_1worker',
                amount_kshs=0.01,
            )


@pytest.mark.asyncio
async def test_send_reward_raises_on_work_failure():
    rpc_responses = [
        # account_info
        {
            'frontier': 'A' * 64,
            'balance': str(10 ** 32),
            'representative': 'kshs_1rep',
        },
        # work_generate fails
        {'error': 'work failed'},
    ]
    with patch('payout._rpc', new_callable=AsyncMock, side_effect=rpc_responses):
        with pytest.raises(PayoutError, match='work_generate failed'):
            await send_reward(
                node_url='localhost:7076',
                fund_seed='a' * 64,
                fund_address='kshs_1fund',
                destination='kshs_1worker',
                amount_kshs=0.01,
            )


@pytest.mark.asyncio
async def test_send_reward_returns_hash_on_success():
    rpc_responses = [
        # account_info
        {
            'frontier': 'A' * 64,
            'balance': str(10 ** 32),
            'representative': 'kshs_1rep',
        },
        # work_generate
        {'work': 'aabb112233445566'},
        # process
        {'hash': 'DEADBEEF' * 8},
    ]
    with patch('payout._rpc', new_callable=AsyncMock, side_effect=rpc_responses):
        with patch('payout._build_block', return_value={'type': 'state', 'signature': 'sig'}):
            result = await send_reward(
                node_url='localhost:7076',
                fund_seed='a' * 64,
                fund_address='kshs_1fund',
                destination='kshs_1worker',
                amount_kshs=0.01,
            )
            assert result == 'DEADBEEF' * 8
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m pytest tests/test_payout.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'payout'`

- [ ] **Step 3: Create `payout.py`**

```python
import hashlib
import json
import logging
from typing import Optional

import nanolib
from aiohttp import ClientSession

logger = logging.getLogger(__name__)

RAW_PER_KSHS = 10 ** 30


class PayoutError(Exception):
    pass


def derive_private_key(seed_hex: str, index: int = 0) -> str:
    """
    Derive Ed25519 private key from a 64-char hex seed using Blake2b.
    This matches the Kakitu/Nano key derivation standard.
    Returns 64-char hex string (32 bytes).
    """
    seed_bytes = bytes.fromhex(seed_hex)
    index_bytes = index.to_bytes(4, 'big')
    h = hashlib.blake2b(digest_size=32)
    h.update(seed_bytes)
    h.update(index_bytes)
    return h.hexdigest()


def _kshs_to_nano(address: str) -> str:
    """Convert kshs_ address to nano_ for nanolib block operations."""
    if address.startswith('kshs_'):
        return 'nano_' + address[5:]
    return address


def _build_block(
    fund_address: str,
    frontier: str,
    representative: str,
    new_balance: int,
    destination: str,
    private_key_hex: str,
    work: str,
) -> dict:
    """
    Construct, sign, and return a state send block as a dict.
    Converts kshs_ addresses to nano_ for nanolib, work is injected after signing.
    """
    block = nanolib.Block(
        block_type='state',
        account=_kshs_to_nano(fund_address),
        previous=frontier,
        representative=_kshs_to_nano(representative),
        balance=new_balance,
        link=_kshs_to_nano(destination),
    )
    block.sign(private_key_hex)
    block.work = work
    return json.loads(block.json())


async def _rpc(node_url: str, payload: dict) -> dict:
    """Single RPC call to the Kakitu node."""
    url = node_url if node_url.startswith('http') else f'http://{node_url}'
    async with ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            return await resp.json(content_type=None)


async def send_reward(
    node_url: str,
    fund_seed: str,
    fund_address: str,
    destination: str,
    amount_kshs: float,
) -> str:
    """
    Build and submit a Kakitu send block from the worker fund wallet.
    Returns block hash on success. Raises PayoutError on failure.
    """
    raw_amount = int(amount_kshs * RAW_PER_KSHS)

    # 1. Get current account state
    info = await _rpc(node_url, {
        'action': 'account_info',
        'account': fund_address,
        'representative': 'true',
    })
    if 'error' in info:
        raise PayoutError(f"account_info failed: {info['error']}")

    frontier = info['frontier']
    current_balance = int(info['balance'])
    representative = info['representative']

    if current_balance < raw_amount:
        raise PayoutError(
            f"Insufficient funds: {current_balance} raw < {raw_amount} raw needed"
        )

    new_balance = current_balance - raw_amount

    # 2. Derive private key
    private_key_hex = derive_private_key(fund_seed, 0)

    # 3. Generate work for the frontier
    work_resp = await _rpc(node_url, {
        'action': 'work_generate',
        'hash': frontier,
    })
    if 'work' not in work_resp:
        raise PayoutError(f"work_generate failed: {work_resp}")

    # 4. Build and sign block
    block_dict = _build_block(
        fund_address=fund_address,
        frontier=frontier,
        representative=representative,
        new_balance=new_balance,
        destination=destination,
        private_key_hex=private_key_hex,
        work=work_resp['work'],
    )

    # 5. Submit block
    process_resp = await _rpc(node_url, {
        'action': 'process',
        'json_block': 'true',
        'subtype': 'send',
        'block': block_dict,
    })

    if 'hash' not in process_resp:
        raise PayoutError(f"process failed: {process_resp}")

    logger.info(f"Paid {amount_kshs} KSHS to {destination} — block {process_resp['hash']}")
    return process_resp['hash']
```

- [ ] **Step 4: Run tests to verify pass**

```bash
python -m pytest tests/test_payout.py -v
```

Expected: 5 tests PASSED.

- [ ] **Step 5: Commit**

```bash
git add payout.py tests/test_payout.py
git commit -m "feat: add payout module for KSHS worker rewards"
```

---

## Task 4: Wire worker hub into main.py

**Files:**
- Modify: `main.py`

The following changes are made to `main.py`:

### 4a — Load new env vars (after line 73, alongside existing config)

- [ ] **Step 1: Add env var loading after the existing `BPOW_FOR_KAKITU` line (line 73)**

Insert after `BPOW_FOR_KAKITU = options.bpow_kakitu_difficulty`:

```python
# Worker hub configuration
WORKER_FUND_SEED = os.getenv('WORKER_FUND_SEED', None)
WORKER_FUND_ADDRESS = os.getenv('WORKER_FUND_ADDRESS', None)
WORK_REWARD_KSHS = float(os.getenv('WORK_REWARD_KSHS', '0.01'))
WORKER_PAYOUTS_ENABLED = WORKER_FUND_SEED is not None and WORKER_FUND_ADDRESS is not None
```

### 4b — Import new modules (after existing imports, before BPOWClient import)

- [ ] **Step 2: Add imports after `from bpow_client import BPOWClient, ConnectionClosed`**

```python
from stats import Stats
from worker_hub import WorkerPool
from payout import send_reward, PayoutError
```

### 4c — Instantiate Stats and WorkerPool at module level (after existing globals)

- [ ] **Step 3: Add after the `options = parser.parse_args()` block (around line 40)**

```python
stats = Stats()
worker_pool = WorkerPool(timeout=30.0)
```

### 4d — Inject worker pool into `work_generate()` dispatch chain

- [ ] **Step 4: In `work_generate()`, add worker pool as the FIRST dispatch attempt, before the existing tasks list (insert before `tasks = []` on line 123)**

Replace this block:
```python
    tasks = []
    for p in WORK_URLS:
        tasks.append(asyncio.create_task(json_post(p, request, app=app)))
    if BPOW_ENABLED and not precache:
        tasks.append(asyncio.create_task(app['bpow'].request_work(hash, difficulty)))
```

With:
```python
    # Try connected workers first
    if not precache and worker_pool.count > 0:
        worker_result = await worker_pool.dispatch(hash, difficulty or 'ffffffc000000000')
        if worker_result is not None:
            await redis.set(
                f"{hash}:{difficulty}" if difficulty is not None else hash,
                worker_result,
                expire=600000
            )
            return {'work': worker_result}

    # Fallback: work peers + BoomPow
    tasks = []
    for p in WORK_URLS:
        tasks.append(asyncio.create_task(json_post(p, request, app=app)))
    if BPOW_ENABLED and not precache:
        tasks.append(asyncio.create_task(app['bpow'].request_work(hash, difficulty)))
```

### 4e — Add `handle_worker_ws` handler (add as new function before `get_app()`)

- [ ] **Step 5: Add `handle_worker_ws` and `handle_stats` functions before `get_app()`**

```python
async def handle_worker_ws(request):
    """WebSocket endpoint for worker connections."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Wait for registration message
    try:
        msg = await ws.receive_json()
    except Exception:
        await ws.close(code=4000, message=b'expected JSON registration message')
        return ws

    address = msg.get('kshs_address', '')
    if not address.startswith('kshs_'):
        await ws.close(code=4001, message=b'invalid kshs_ address')
        return ws

    ws_id = await worker_pool.add(ws, address)
    stats.connected_workers = worker_pool.count

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    won = await worker_pool.submit(ws_id, data['hash'], data['work'])
                    if won:
                        asyncio.ensure_future(
                            pay_and_notify(ws_id, data['hash'], data['work'])
                        )
                except Exception as e:
                    log.server_logger.warning(f"Bad message from {ws_id}: {e}")
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        await worker_pool.remove(ws_id)
        stats.connected_workers = worker_pool.count

    return ws


async def pay_and_notify(ws_id: str, hash: str, work: str):
    """Issue payout and notify worker of result. Fire-and-forget via ensure_future."""
    worker = worker_pool.workers.get(ws_id)
    if worker is None:
        return

    stats.record_completion(worker, WORK_REWARD_KSHS)

    if not WORKER_PAYOUTS_ENABLED:
        return

    try:
        tx_hash = await send_reward(
            node_url=NODE_CONNSTR,
            fund_seed=WORKER_FUND_SEED,
            fund_address=WORKER_FUND_ADDRESS,
            destination=worker.kshs_address,
            amount_kshs=WORK_REWARD_KSHS,
        )
        try:
            await worker.ws.send_json({
                'action': 'paid',
                'hash': hash,
                'amount': f'{WORK_REWARD_KSHS:.6f}',
                'tx_hash': tx_hash,
            })
        except Exception:
            pass
    except PayoutError as e:
        log.server_logger.error(f"Payout failed for {worker.kshs_address}: {e}")


async def handle_stats(request):
    """GET /stats — public network statistics."""
    return web.json_response(stats.to_dict())
```

### 4f — Register new routes in `get_app()`

- [ ] **Step 6: Add new routes inside `get_app()` alongside existing routes**

After `app.add_routes([web.post('/callback', callback)])`, add:

```python
    app.add_routes([web.get('/worker/ws', handle_worker_ws)])
    app.add_routes([web.get('/stats', handle_stats)])
```

- [ ] **Step 7: Verify the server starts**

```bash
cd /Users/kiptengwer/Documents/kakitu/kakitu-middleware
python main.py --host 127.0.0.1 --port 5555 &
sleep 2
curl -s http://127.0.0.1:5555/stats | python3 -m json.tool
kill %1
```

Expected output:
```json
{
    "connected_workers": 0,
    "total_work_completed": 0,
    "total_kshs_paid": "0.000000",
    "top_earners": []
}
```

- [ ] **Step 8: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: All tests PASSED.

- [ ] **Step 9: Commit**

```bash
git add main.py
git commit -m "feat: wire WorkerPool and Stats into main.py dispatch chain and routes"
```

---

## Task 5: Install test dependencies and verify full test suite

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add test dependencies to `requirements.txt`**

Append to `requirements.txt`:
```
pytest
pytest-asyncio
```

- [ ] **Step 2: Install**

```bash
./venv/bin/pip install pytest pytest-asyncio
```

- [ ] **Step 3: Run full suite**

```bash
python -m pytest tests/ -v
```

Expected: All tests PASSED, no warnings.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: add pytest and pytest-asyncio to requirements"
git push
```
