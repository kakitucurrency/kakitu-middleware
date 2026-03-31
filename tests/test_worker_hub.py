import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from worker_hub import WorkerPool, Worker


def make_ws():
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

    async def fake_submit():
        await asyncio.sleep(0.01)
        await pool.submit(ws_id, 'HASH123', 'aabbccdd11223344')

    with patch('worker_hub.nanolib.validate_work', return_value=None):
        asyncio.create_task(fake_submit())
        result = await pool.dispatch('HASH123', 'ffffffc000000000')

    assert result == 'aabbccdd11223344'
    ws.send_json.assert_any_call({
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
async def test_count_property():
    pool = WorkerPool()
    assert pool.count == 0
    ws1 = make_ws()
    ws2 = make_ws()
    id1 = await pool.add(ws1, 'kshs_1aaa')
    id2 = await pool.add(ws2, 'kshs_1bbb')
    assert pool.count == 2
    await pool.remove(id1)
    assert pool.count == 1
