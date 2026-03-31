import hashlib
import json
import logging
from decimal import Decimal
from typing import Optional

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

RAW_PER_KSHS = 10 ** 30


class PayoutError(Exception):
    pass


def derive_private_key(seed_hex: str, index: int = 0) -> str:
    """
    Derive Ed25519 private key from a 64-char hex seed using Blake2b.
    Matches the Kakitu/Nano key derivation standard.
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
    Converts kshs_ addresses to nano_ for nanolib.
    """
    try:
        import nanolib
        # nanolib.Block.link expects a 64-char hex public key, not an account address
        dest_pubkey_hex = nanolib.get_account_public_key(account_id=_kshs_to_nano(destination))
        block = nanolib.Block(
            block_type='state',
            account=_kshs_to_nano(fund_address),
            previous=frontier,
            representative=_kshs_to_nano(representative),
            balance=new_balance,
            link=dest_pubkey_hex,
        )
        block.sign(private_key_hex)
        block.work = work
        return json.loads(block.json())
    except ImportError:
        raise PayoutError("nanolib not available — cannot build block on this platform")


async def _rpc(node_url: str, payload: dict) -> dict:
    """Single RPC call to the Kakitu node."""
    url = node_url if node_url.startswith('http') else f'http://{node_url}'
    timeout = ClientTimeout(total=10)
    async with ClientSession(timeout=timeout) as session:
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
    raw_amount = int(Decimal(str(amount_kshs)) * Decimal(RAW_PER_KSHS))

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
