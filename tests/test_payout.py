import pytest
from unittest.mock import AsyncMock, patch
from payout import derive_private_key, send_reward, PayoutError


def test_derive_private_key_deterministic():
    seed = 'a' * 64
    key1 = derive_private_key(seed, 0)
    key2 = derive_private_key(seed, 0)
    assert key1 == key2
    assert len(key1) == 64  # 32 bytes as hex


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
        {
            'frontier': 'A' * 64,
            'balance': str(10 ** 32),
            'representative': 'kshs_1rep',
        },
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
        {
            'frontier': 'A' * 64,
            'balance': str(10 ** 32),
            'representative': 'kshs_1rep',
        },
        {'work': 'aabb112233445566'},
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
