"""
Minimal nanolib stub for environments where the C extension cannot be built.
Only the functions used by worker_hub are stubbed here.
"""


def validate_work(block_hash: str, work: str):
    """
    Validate proof-of-work for a Nano/Kakitu block hash.
    Raises ValueError if the work is invalid.
    This stub always raises NotImplementedError; real validation
    requires the compiled nanolib C extension.
    """
    raise NotImplementedError(
        "nanolib C extension not available on this platform. "
        "Patch worker_hub.nanolib.validate_work in tests."
    )
