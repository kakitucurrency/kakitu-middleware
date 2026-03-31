# Kakitu Worker Hub — Design Spec

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend kakitu-middleware with a WebSocket worker hub so that community members can connect their machines to `work.kakitu.org`, contribute proof-of-work computation, and earn KSHS micropayments automatically per completed work unit.

**Architecture:** Three new modules (`worker_hub.py`, `payout.py`, `stats.py`) added to the existing aiohttp server. A new `/worker/ws` WebSocket route accepts anonymous worker connections. Incoming `work_generate` requests are dispatched to the connected worker pool first; the first valid result wins a KSHS payout from a dedicated worker fund wallet. Existing fallback chain (BoomPow → work peers → local node) is preserved when no workers are connected.

**Tech Stack:** Python 3.6+, aiohttp, aiohttp WebSocket, nanolib (existing), Kakitu RPC (`work_generate`, `send` block construction)

---

## 1. New Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WORKER_FUND_SEED` | Yes (for payouts) | — | 64-char hex seed of the worker fund wallet |
| `WORKER_FUND_ADDRESS` | Yes (for payouts) | — | `kshs_` address of the worker fund wallet |
| `WORK_REWARD_KSHS` | No | `0.01` | KSHS paid per completed work unit |
| `NODE_URL` | Existing | — | Kakitu node RPC URL (used for payout sends) |

If `WORKER_FUND_SEED` is not set, the worker hub runs in **no-payout mode** — workers can still connect and contribute, but no KSHS is sent.

---

## 2. `worker_hub.py`

Manages the pool of connected workers and coordinates task dispatch.

### WorkerPool

```python
class WorkerPool:
    def __init__(self):
        self.workers: dict[str, Worker] = {}  # ws_id → Worker
        self.pending_tasks: dict[str, asyncio.Future] = {}  # hash → Future

    async def add(self, ws, kshs_address: str) -> str:
        """Register a new worker. Returns ws_id."""

    async def remove(self, ws_id: str):
        """Remove worker on disconnect."""

    async def dispatch(self, hash: str, difficulty: str) -> str | None:
        """
        Push work task to all connected workers.
        Returns first valid work result, or None if pool is empty.
        Cancels remaining workers once result received.
        Times out after 30 seconds (falls through to fallback chain).
        """

    async def submit(self, ws_id: str, hash: str, work: str) -> bool:
        """
        Called when a worker submits a result.
        Validates work via nanolib. If valid, resolves the pending Future.
        Returns True if this worker won (first valid result).
        """
```

### Worker

```python
@dataclass
class Worker:
    ws_id: str
    ws: aiohttp.web.WebSocketResponse
    kshs_address: str
    connected_at: float
    work_completed: int = 0
    kshs_earned: float = 0.0
```

### WebSocket message protocol

**Worker → Server (on connect):**
```json
{"kshs_address": "kshs_1xxx..."}
```

**Server → Worker (work task):**
```json
{"action": "work", "hash": "ECCB8CB...", "difficulty": "ffffffc000000000"}
```

**Worker → Server (result):**
```json
{"hash": "ECCB8CB...", "work": "a3d4b5c6d7e8f901"}
```

**Server → Worker (task cancelled — another worker won):**
```json
{"action": "cancel", "hash": "ECCB8CB..."}
```

**Server → Worker (payout confirmation):**
```json
{"action": "paid", "hash": "ECCB8CB...", "amount": "0.01", "tx_hash": "DEF456..."}
```

---

## 3. `payout.py`

Handles sending KSHS from the worker fund wallet to the winning worker.

```python
async def send_reward(
    node_url: str,
    fund_seed: str,
    fund_address: str,
    destination: str,
    amount_kshs: float,
) -> str:
    """
    Constructs and processes a Kakitu send block from the worker fund wallet.
    Returns the block hash on success.
    Raises PayoutError on failure (logged, worker still credited in stats).
    """
```

**Block construction steps:**
1. Fetch current fund account info (frontier, balance) via `account_info` RPC
2. Compute raw amount: `int(amount_kshs * 10**30)`
3. Derive private key from `fund_seed` at index 0 using Blake2b (same as WalletGen)
4. Construct state block: type=state, subtype=send, account=fund_address, previous=frontier, representative=existing_rep, balance=current_balance-raw_amount, link=destination
5. Sign with Ed25519
6. Generate work for the frontier hash
7. Submit via `process` RPC

---

## 4. `stats.py`

Tracks and exposes network statistics.

```python
class Stats:
    total_work_completed: int = 0
    total_kshs_paid: float = 0.0
    workers_all_time: int = 0
    current_workers: int = 0  # live count from WorkerPool
    top_earners: list[dict]  # [{"address": "kshs_1...", "earned": 0.5}, ...]

    def record_completion(self, worker: Worker, amount: float):
        """Update totals and top_earners list (keep top 10 by earned)."""

    def to_dict(self) -> dict:
        """Serialise for GET /stats response."""
```

Stats are in-memory only — reset on restart. No database required.

---

## 5. Changes to `main.py`

### New routes

```python
app.router.add_get('/worker/ws', handle_worker_ws)
app.router.add_get('/stats', handle_stats)
```

### `handle_worker_ws`

```python
async def handle_worker_ws(request):
    ws = aiohttp.web.WebSocketResponse()
    await ws.prepare(request)

    # Wait for registration message
    msg = await ws.receive_json(timeout=10)
    address = msg.get('kshs_address', '')
    if not address.startswith('kshs_'):
        await ws.close(code=4000, message=b'invalid address')
        return ws

    ws_id = await worker_pool.add(ws, address)
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                won = await worker_pool.submit(ws_id, data['hash'], data['work'])
                if won:
                    asyncio.create_task(pay_and_notify(ws_id, data['hash']))
    finally:
        await worker_pool.remove(ws_id)
    return ws
```

### Updated dispatch chain in `rpc` function

```python
# 1. Try worker pool first
work = await worker_pool.dispatch(hash, difficulty)

# 2. Fallback to existing chain if pool empty or timeout
if work is None:
    work = await existing_dispatch(hash, difficulty)  # BoomPow → peers → node
```

### `handle_stats`

```python
async def handle_stats(request):
    return aiohttp.web.json_response(stats.to_dict())
```

---

## 6. Updated `requirements.txt`

Add:
```
aiohttp[speedups]>=3.8.0
```

aiohttp has native WebSocket support — no additional `websockets` package needed.

---

## 7. `/stats` Response Format

```json
{
  "connected_workers": 12,
  "total_work_completed": 4821,
  "total_kshs_paid": "48.21",
  "work_reward_kshs": "0.01",
  "top_earners": [
    {"address": "kshs_1abc...", "earned": "2.50", "work_completed": 250},
    {"address": "kshs_1def...", "earned": "1.80", "work_completed": 180}
  ]
}
```

---

## 8. Deployment

Add to `kakitu-middleware` systemd service or Railway environment:

```
WORKER_FUND_SEED=<64-char hex seed>
WORKER_FUND_ADDRESS=kshs_1workerfund...
WORK_REWARD_KSHS=0.01
```

Run with `--host 0.0.0.0` to accept public connections. Point `work.kakitu.org` at the server.

Worker fund wallet should be topped up manually from the treasury as needed. Monitor `GET /stats` to track fund burn rate.

---

## 9. Sub-project 2: kakitu-miner (Tauri Desktop App)

A separate repo `kakitu-miner` — Tauri + React + TypeScript desktop application.

**UI:**
- Dark theme, amber `#f0b429` accents matching Kakitu explorer
- Address input field + Connect/Disconnect toggle
- Live mining animation when computing work
- Session stats: work completed, KSHS earned, connection status
- Network stats pulled from `GET /stats`: connected workers, total paid

**Worker logic (Rust/Tauri backend):**
- WebSocket connection to `wss://work.kakitu.org/worker/ws`
- Receives work tasks, runs `work_generate` using local CPU (same algorithm as Nano)
- Submits results, receives payout confirmations
- Emits Tauri events to React frontend for live UI updates

**Repo:** `kakitucurrency/kakitu-miner`

This sub-project is designed after the middleware is complete and deployed.
