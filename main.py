#!/usr/bin/env python
from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import ipaddress
import logging
import json
import signal
import uuid
import aioredis
import datetime
from aiohttp import ClientSession, ClientTimeout, WSMsgType, log, web
from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler
import uvloop
import sys
import os
import nanolib
from decimal import Decimal

import re

from bpow_client import BPOWClient, ConnectionClosed

# Valid base32 alphabet for Kakitu/Nano addresses (no 0, 2, l, v)
_KSHS_ADDR_RE = re.compile(r'^kshs_[13][13456789abcdefghijkmnopqrstuwxyz]{59}$')


def validate_kshs_address(address: str) -> bool:
    """Validate a kshs_ address: correct prefix, length (65), and base32 chars."""
    if not isinstance(address, str):
        return False
    if len(address) != 65:
        return False
    return _KSHS_ADDR_RE.match(address) is not None

from aiographql.client import GraphQLResponse
from stats import Stats
from worker_hub import WorkerPool
from payout import send_reward, PayoutError, SEND_DIFFICULTY

uvloop.install()

current_loop = asyncio.new_event_loop()
asyncio.set_event_loop(current_loop)

# Configuration arguments

parser = argparse.ArgumentParser(description="Kakitu Middleware Work Distributer, Callback Forwarder, Work Precacher (KSHS)")
parser.add_argument('--host', type=str, help='Host for kakitu-middleware to listen on', default='127.0.0.1')
parser.add_argument('--port', type=int, help='Port for kakitu-middleware to listen on', default=int(os.environ.get('PORT', '5555')))
parser.add_argument('--node-url', type=str, help='Node RPC Connection String')
parser.add_argument('--log-file', type=str, help='Log file location')
parser.add_argument('--work-urls', nargs='*', help='Work servers to send work too (NOT for dPOW')
parser.add_argument('--callbacks',nargs='*', help='Endpoints to forward node callbacks to')
parser.add_argument('--bpow-url', type=str, help='BoomPow (bPow) HTTP URL', default='https://boompow.kakitu.org/graphql')
parser.add_argument('--bpow-kakitu-difficulty', action='store_true', help='Use KSHS difficulty with BoomPow (If using for Kakitu instead of legacy networks)', default=False)
parser.add_argument('--precache', action='store_true', help='Enables work precaching if specified (does not apply to dPOW or bPow)', default=False)
parser.add_argument('--debug', action='store_true', help='Runs in debug mode if specified', default=False)
options = parser.parse_args()

# Callback forwarding
CALLBACK_FORWARDS = options.callbacks if options.callbacks is not None else []

# Work URLs
WORK_URLS = options.work_urls if options.work_urls is not None else []

# Precache
PRECACHE = options.precache

# Log
LOG_FILE = options.log_file

LISTEN_HOST = str(ipaddress.ip_address(options.host))
LISTEN_PORT = int(options.port)

# Node URL — CLI arg takes precedence, fall back to NODE_URL env var
NODE_CONNSTR = options.node_url or os.getenv('NODE_URL', None)
NODE_FALLBACK = False
if NODE_CONNSTR is not None:
    try:
        NODE_URL = NODE_CONNSTR.split(':')[0]
        NODE_PORT = NODE_CONNSTR.split(':')[1]
        NODE_FALLBACK = True
    except Exception:
        logging.error(f"Invalid node connection string, should be url:port, not {NODE_CONNSTR}")
        parser.print_help()
        sys.exit(1)

# For Kakitu's BoomPow
BPOW_URL = options.bpow_url
BPOW_KEY = os.getenv('BPOW_KEY', None)
BPOW_ENABLED = BPOW_KEY is not None
BPOW_FOR_KAKITU = options.bpow_kakitu_difficulty

# Worker hub configuration
WORKER_FUND_SEED = os.getenv('WORKER_FUND_SEED', None)
WORKER_FUND_ADDRESS = os.getenv('WORKER_FUND_ADDRESS', None)
WORK_REWARD_KSHS = Decimal(os.getenv('WORK_REWARD_KSHS', '0.01'))
WORKER_PAYOUTS_ENABLED = (
    WORKER_FUND_SEED is not None
    and WORKER_FUND_ADDRESS is not None
    and NODE_CONNSTR is not None
)

stats = Stats()
worker_pool = WorkerPool(timeout=30.0)
_payout_lock = asyncio.Lock()
_idempotency_cache: dict = {}  # idempotency_key → response dict

work_futures = dict()

async def init_bpow(app):
    if BPOW_ENABLED:
        app['bpow'] = BPOWClient(BPOW_URL,BPOW_KEY, app,force_kakitu_difficulty=BPOW_FOR_KAKITU)
    else:
        app['bpow'] = None

DEBUG = options.debug

# Constants
PRECACHE_Q_KEY = 'kakitu_middleware_pcache_q'

### PEER-related functions

async def json_post(url, request, timeout=30, app=None, dontcare=False):
    try:
        ct = ClientTimeout(total=timeout)
        async with ClientSession(timeout=ct) as session:
            async with session.post(url, json=request) as resp:
                if dontcare:
                    return resp.status
                else:
                    return await resp.json(content_type=None)
    except Exception as e:
        log.server_logger.exception(e)
        if app is not None:
            app['failover'] = True
            if app['failover_dt'] is None:
                app['failover_dt'] = datetime.datetime.utcnow()
        return None

async def work_cancel(hash):
    """RPC work_cancel"""
    request = {"action":"work_cancel", "hash":hash}
    tasks = []
    for p in WORK_URLS:
        tasks.append(json_post(str(p), request))
    # Don't care about waiting for any responses on work_cancel
    for t in tasks:
        asyncio.ensure_future(t)

async def work_generate(hash, app, precache=False, difficulty=None, reward=True):
    """RPC work_generate"""
    redis = app['redis']
    # Always include difficulty; fall back to SEND_DIFFICULTY when not specified
    if difficulty is None:
        difficulty = SEND_DIFFICULTY
    request = {"action":"work_generate", "hash":hash, "difficulty": difficulty}
    request['reward'] = reward
    # Try connected workers first
    if not precache and worker_pool.count > 0:
        worker_result = await worker_pool.dispatch(hash, difficulty)
        if worker_result is not None:
            try:
                await redis.set(
                    f"{hash}:{difficulty}",
                    worker_result,
                    expire=600000
                )
            except Exception:
                pass
            return {'work': worker_result}

    # Fallback: work peers + BoomPow
    tasks = []
    for p in WORK_URLS:
        tasks.append(asyncio.create_task(json_post(p, request, app=app)))
    if BPOW_ENABLED and not precache:
        tasks.append(asyncio.create_task(app['bpow'].request_work(hash, difficulty)))

    if NODE_FALLBACK and app['failover']:
        # Failover to the node since we have some requests that are failing
        # If its been an hour since a work request failed, disable failover mode
        if app['failover_dt'] is not None and (datetime.datetime.utcnow() - app['failover_dt']).total_seconds() > 3600:
            app['failover'] = False
            app['failover_dt'] = None
        else:
            tasks.append(json_post(f"http://{NODE_CONNSTR}", request, timeout=30))

    while len(tasks):
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=30)
        for task in done:
            try:
                result = task.result()
                if isinstance(result, GraphQLResponse):
                    if len(result.errors) > 0:
                        result = None
                    else:
                        result = {"work": result.data["workGenerate"]}
                if isinstance(result, list):
                    result = json.loads(result[1])
                elif result is None:
                    log.server_logger.info("task returned None")
                    continue
                if 'work' in result:
                    asyncio.ensure_future(work_cancel(hash))
                    try:
                        await redis.set(f"{hash}:{difficulty}", result['work'], expire=600000) # Cache work
                    except Exception:
                        pass
                    return result
                elif 'error' in result:
                    log.server_logger.info(f'task returned error {result["error"]}')
            except Exception as exc:
                log.server_logger.exception("Task raised an exception")
                task.cancel()
    # Fallback method — work_generate can be slow, allow 120s
    if NODE_FALLBACK:
        return await json_post(f"http://{NODE_CONNSTR}", request, timeout=120)

    return None

async def precache_queue_process(app):
    while True and PRECACHE:
        if app['busy']:
            # Wait and try precache again later
            await asyncio.sleep(5)
            continue

        # Pop item off the queue
        hash = await app['redis'].lpop(PRECACHE_Q_KEY)
        if hash is None:
            await asyncio.sleep(3) # Delay before checking again
            continue
        # See if already have this hash cached
        have_pow = await app['redis'].get(hash)
        if have_pow is not None:
            continue # Already cached
        log.server_logger.info(f"precaching {hash}")
        await work_generate(hash, app, precache=True)

### END PEER-related functions

### API

_HEX64_RE = re.compile(r'^[0-9a-fA-F]{64}$')


async def rpc(request):
    requestjson = await request.json()
    log.server_logger.info(f"Received request {str(requestjson)}")
    if 'action' not in requestjson or requestjson['action'] != 'work_generate':
        return web.HTTPBadRequest(reason='invalid action')
    elif 'hash' not in requestjson:
        return web.HTTPBadRequest(reason='Missing hash in request')

    # Sanitize hash: must be exactly 64 hex characters
    req_hash = str(requestjson['hash']).strip()
    if not _HEX64_RE.match(req_hash):
        return web.HTTPBadRequest(reason='Invalid hash format')
    requestjson['hash'] = req_hash

    difficulty = requestjson.get('difficulty', SEND_DIFFICULTY)
    reward = requestjson.get('reward', True)
    # See if work is in cache
    try:
        work = await request.app['redis'].get(f"{requestjson['hash']}:{difficulty}")
        if work is not None:
            # Validate
            try:
                nanolib.validate_work(requestjson['hash'], work, difficulty=difficulty)
                return web.json_response({"work":work})
            except nanolib.InvalidWork:
                pass
    except Exception:
        pass
    # Not in cache, request it from peers
    try:
        request.app['busy'] = True # Halts the precaching process
        respjson = await work_generate(requestjson['hash'], request.app, difficulty=difficulty, reward=reward)
        if respjson is None:
            request.app['busy'] = False
            return web.HTTPInternalServerError(reason="Couldn't generate work")
        request.app['busy'] = False
        return web.json_response(respjson)
    except Exception as e:
        request.app['busy'] = False
        log.server_logger.exception(e)
        return web.HTTPInternalServerError(reason=str(sys.exc_info()))

async def callback(request):
    requestjson = await request.json()
    hash = requestjson['hash']
    log.server_logger.debug(f"callback received {hash}")
    # Forward callback
    for c in CALLBACK_FORWARDS:
        await asyncio.ensure_future(json_post(c, requestjson, dontcare=True))
    # Precache POW if necessary
    if not PRECACHE:
        return web.Response(status=200)
    block = json.loads(requestjson['block'])
    if 'previous' not in block:
        log.server_logger.info(f"previous not in block from callback {str(block)}")
        return web.Response(status=200)
    previous_pow = await request.app['redis'].get(block['previous'])
    if previous_pow is None:
        return web.Response(status=200) # They've never requested work from us before so we don't care
    else:
        await request.app['redis'].rpush(PRECACHE_Q_KEY, hash)
    return web.Response(status=200)

### END API

### APP setup

async def handle_worker_ws(request):
    """WebSocket endpoint for worker connections at /worker/ws."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Wait for registration message
    try:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
    except Exception:
        await ws.close(code=4000, message=b'expected JSON registration message')
        return ws

    address = msg.get('kshs_address', '')
    if not validate_kshs_address(address):
        await ws.close(code=4001, message=b'invalid kshs_ address')
        return ws

    ws_id = await worker_pool.add(ws, address)
    stats.connected_workers = worker_pool.count
    stats.workers_all_time += 1

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    worker_obj = worker_pool.workers.get(ws_id)
                    won = await worker_pool.submit(ws_id, data['hash'], data['work'])
                    if won and worker_obj is not None:
                        asyncio.ensure_future(
                            pay_and_notify(worker_obj, data['hash'])
                        )
                except Exception as e:
                    log.server_logger.warning(f"Bad message from {ws_id}: {e}")
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        await worker_pool.remove(ws_id)
        stats.connected_workers = worker_pool.count

    return ws


async def _fast_work_generate(hash: str) -> str:
    """Generate payout work directly via node RPC.
    Bypasses worker pool so we don't cancel in-progress miner tasks.
    Uses send/change difficulty (64x)."""
    from payout import _rpc, SEND_DIFFICULTY
    work_resp = await _rpc(NODE_CONNSTR, {
        'action': 'work_generate',
        'hash': hash,
        'difficulty': SEND_DIFFICULTY,
    }, timeout_secs=30)
    if 'work' in work_resp:
        return work_resp['work']
    raise Exception(f"payout work_generate failed: {work_resp}")


async def pay_and_notify(worker, hash: str):
    """Issue payout and notify worker. Fire-and-forget via ensure_future.
    worker is captured at submission time so payout works even if the worker
    has since disconnected. _payout_lock serialises sends so concurrent
    completions don't race on the same frontier."""
    if not WORKER_PAYOUTS_ENABLED:
        return

    # Natural idempotency key: worker_address:block_hash
    payout_key = f"{worker.kshs_address}:{hash}"
    if payout_key in _idempotency_cache:
        return

    req_id = str(uuid.uuid4())[:8]
    async with _payout_lock:
        # Re-check after acquiring lock (another coroutine may have completed it)
        if payout_key in _idempotency_cache:
            return
        try:
            log.server_logger.info(
                f"[payout:{req_id}] sending {WORK_REWARD_KSHS} KSHS to {worker.kshs_address} for hash {hash}"
            )
            tx_hash = await send_reward(
                node_url=NODE_CONNSTR,
                fund_seed=WORKER_FUND_SEED,
                fund_address=WORKER_FUND_ADDRESS,
                destination=worker.kshs_address,
                amount_kshs=WORK_REWARD_KSHS,
                work_generate_fn=_fast_work_generate,
            )
            _idempotency_cache[payout_key] = tx_hash
            stats.record_completion(worker, WORK_REWARD_KSHS)
            log.server_logger.info(
                f"[payout:{req_id}] paid {worker.kshs_address} — block {tx_hash}"
            )
            try:
                await worker.ws.send_json({
                    'action': 'paid',
                    'hash': hash,
                    'amount': str(WORK_REWARD_KSHS),
                    'tx_hash': tx_hash,
                })
            except Exception:
                pass
        except Exception as e:
            log.server_logger.error(f"[payout:{req_id}] failed for {worker.kshs_address}: {e}")
            stats.record_completion(worker, 0)


async def handle_stats(request):
    """GET /stats — public network statistics."""
    data = stats.to_dict()
    data['work_reward_kshs'] = str(WORK_REWARD_KSHS)
    return web.json_response(data, headers={'Access-Control-Allow-Origin': '*'})


async def handle_health(request):
    """GET /health -- liveness/readiness check."""
    node_connected = False
    if NODE_CONNSTR:
        try:
            from payout import _rpc
            resp = await _rpc(NODE_CONNSTR, {'action': 'version'}, timeout_secs=5)
            node_connected = 'node_vendor' in resp or 'rpc_version' in resp
        except Exception:
            node_connected = False
    return web.json_response({
        'status': 'ok',
        'node_connected': node_connected,
        'workers': worker_pool.count,
    })


MAX_TRANSACTION_KSHS = Decimal('1000000')  # 1M KSHS per tx safety cap


async def handle_admin_fund(request):
    """POST /admin/fund — one-time fund worker wallet from genesis. Requires ADMIN_SECRET env var."""
    ADMIN_SECRET = os.getenv('ADMIN_SECRET', '')
    if not ADMIN_SECRET:
        return web.json_response({'error': 'admin not enabled'}, status=403)
    try:
        body = await request.json()
        secret = body.get('secret', '')
        if not secret or not secret.strip():
            return web.json_response({'error': 'missing admin secret'}, status=403)
        if secret != ADMIN_SECRET:
            return web.json_response({'error': 'forbidden'}, status=403)
        priv_hex    = body['priv_hex']     # 64-char hex genesis private key
        from_addr   = str(body.get('from_addr', '')).strip()
        to_addr     = str(body.get('to_addr', WORKER_FUND_ADDRESS or '')).strip()
        if not validate_kshs_address(from_addr):
            return web.json_response({'error': 'invalid from_addr'}, status=400)
        if not validate_kshs_address(to_addr):
            return web.json_response({'error': 'invalid to_addr'}, status=400)
        amount_kshs = Decimal(str(body.get('amount_kshs', '10000')))
        if amount_kshs <= 0:
            return web.json_response({'error': 'amount must be positive'}, status=400)
        if amount_kshs > MAX_TRANSACTION_KSHS:
            return web.json_response(
                {'error': f'amount exceeds max {MAX_TRANSACTION_KSHS} KSHS per transaction'},
                status=400,
            )

        # Idempotency: if key already processed, return cached result
        idempotency_key = body.get('idempotency_key')
        if idempotency_key and idempotency_key in _idempotency_cache:
            return web.json_response(_idempotency_cache[idempotency_key])

        req_id = str(uuid.uuid4())[:8]
        log.server_logger.info(f"[req:{req_id}] admin_fund: {from_addr} -> {to_addr}, {amount_kshs} KSHS")

        # Use send_reward which handles work_generate with correct 120s timeout
        tx_hash = await send_reward(
            node_url=NODE_CONNSTR,
            fund_seed=priv_hex,
            fund_address=from_addr,
            destination=to_addr,
            amount_kshs=amount_kshs,
        )
        result = {'ok': True, 'hash': tx_hash, 'amount_kshs': str(amount_kshs)}
        if idempotency_key:
            _idempotency_cache[idempotency_key] = result
        log.server_logger.info(f"[req:{req_id}] admin_fund complete: block {tx_hash}")
        return web.json_response(result)
    except Exception as e:
        log.server_logger.error(f"admin_fund error: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def get_app():
    async def close_redis(app):
        """Close redis connection"""
        log.server_logger.info('Closing redis connection')
        try:
            app['redis'].close()
        except Exception:
            pass

    async def open_redis(app):
        """Open redis connection"""
        log.server_logger.info("Opening redis connection")
        try:
            app['redis'] = await aioredis.create_redis_pool((os.getenv('REDIS_HOST', 'localhost'), 6379),
                                                db=int(os.getenv('REDIS_DB', '1')), encoding='utf-8', minsize=2, maxsize=15)
        except Exception:
            app['redis'] = None
            log.server_logger.warning('Could not connect to Redis, work caching and some other features will not work')

    async def init_queue(app):
        """Initialize task queue"""
        app['precache_task'] = current_loop.create_task(precache_queue_process(app))

    async def clear_queue(app):
        app['precache_task'].cancel()
        await app['precache_task']

    log_formatter = logging.Formatter(
        "%(asctime)s;%(levelname)s;%(message)s", "%Y-%m-%d %H:%M:%S %z"
    )
    if DEBUG:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s;%(levelname)s;%(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S %z")
    else:
        if LOG_FILE is not None:
            root = logging.getLogger('aiohttp.server')
            logging.basicConfig(level=logging.INFO, format="%(asctime)s;%(levelname)s;%(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S %z")
            handler = WatchedFileHandler(LOG_FILE)
            handler.setFormatter(log_formatter)
            root.addHandler(handler)
            root.addHandler(TimedRotatingFileHandler(LOG_FILE, when="d", interval=1, backupCount=100))
        else:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s;%(levelname)s;%(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S %z")
    app = web.Application()
    app['busy'] = False
    app['failover'] = False
    app['failover_dt'] = None
    app.add_routes([web.post('/', rpc)])
    app.add_routes([web.post('/callback', callback)])
    app.add_routes([web.get('/worker/ws', handle_worker_ws)])
    app.add_routes([web.get('/stats', handle_stats)])
    app.add_routes([web.post('/admin/fund', handle_admin_fund)])
    app.add_routes([web.get('/health', handle_health)])
    app.on_startup.append(open_redis)
    app.on_startup.append(init_queue)
    app.on_startup.append(init_bpow)
    app.on_cleanup.append(clear_queue)
    app.on_shutdown.append(close_redis)

    return app

work_app = current_loop.run_until_complete(get_app())

def main():
    """Main application loop with graceful shutdown."""

    runner = None

    async def start():
        nonlocal runner
        runner = web.AppRunner(work_app)
        await runner.setup()
        site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
        await site.start()
        log.server_logger.info(f"Kakitu middleware listening on {LISTEN_HOST}:{LISTEN_PORT}")

    async def graceful_shutdown():
        """Stop accepting connections, wait for in-flight payouts, then cleanup."""
        log.server_logger.info("Shutdown signal received, stopping gracefully...")

        # 1. Stop accepting new connections
        if runner is not None:
            await runner.cleanup()

        # 2. Wait for in-flight payouts to complete (up to 30s)
        if _payout_lock.locked():
            log.server_logger.info("Waiting for in-flight payouts to complete (up to 30s)...")
            try:
                await asyncio.wait_for(_payout_lock.acquire(), timeout=30)
                _payout_lock.release()
            except asyncio.TimeoutError:
                log.server_logger.warning("Timed out waiting for in-flight payouts")

        # 3. Clean shutdown of the app
        await work_app.shutdown()
        await work_app.cleanup()
        log.server_logger.info("Shutdown complete.")

    def _signal_handler():
        log.server_logger.info("Received termination signal")
        asyncio.ensure_future(graceful_shutdown())
        current_loop.call_later(1, current_loop.stop)

    # Register signal handlers for graceful shutdown
    for sig in (signal.SIGTERM, signal.SIGINT):
        current_loop.add_signal_handler(sig, _signal_handler)

    current_loop.run_until_complete(start())

    # Main program
    try:
        current_loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        current_loop.run_until_complete(graceful_shutdown())

    current_loop.close()

if __name__ == "__main__":
    main()
