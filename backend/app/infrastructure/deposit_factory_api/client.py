"""DepositForwarderFactory (contracts/src/DepositForwarderFactory.sol, PLAN.md section 3).

The ABI is the contracts build's own ``contracts/abi/DepositForwarderFactory.json`` (regenerated with ``forge
inspect``), read by path relative to the repository; the image keeps the same layout. Selectors
and the event topic are derived from that ABI at import, so a changed signature fails the start, not a sweep.
"""

from collections.abc import Sequence
import json
from pathlib import Path

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address
from pydantic import BaseModel, ConfigDict, JsonValue

from app.infrastructure.deposit_factory_api.schemas import DepositSweptEvent
from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.schemas import LogEntry

FACTORY_ABI_PATH = Path(__file__).resolve().parents[4] / "contracts" / "abi" / "DepositForwarderFactory.json"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class AbiEntry(BaseModel):
    """The parts of a forge ABI entry the signatures are built from."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: str
    name: str = ""
    inputs: list[dict[str, JsonValue]] = []


def read_factory_abi() -> list[AbiEntry]:
    loaded = json.loads(FACTORY_ABI_PATH.read_text())
    # `forge inspect ... abi --json` writes the array; a full artifact nests it under "abi".
    entries = loaded["abi"] if isinstance(loaded, dict) else loaded
    return [AbiEntry.model_validate(entry) for entry in entries]


def signature_of(abi: list[AbiEntry], *, entry_type: str, name: str) -> tuple[str, list[str]]:
    for entry in abi:
        if entry.type == entry_type and entry.name == name:
            input_types = [str(argument["type"]) for argument in entry.inputs]
            return f"{name}({','.join(input_types)})", input_types
    raise RuntimeError(f"{FACTORY_ABI_PATH} has no {entry_type} {name}")


FACTORY_ABI = read_factory_abi()
PREDICT_SIGNATURE, PREDICT_INPUT_TYPES = signature_of(FACTORY_ABI, entry_type="function", name="predictDepositAddress")
SWEEP_SIGNATURE, SWEEP_INPUT_TYPES = signature_of(FACTORY_ABI, entry_type="function", name="sweepToDestination")
DEPOSIT_SWEPT_SIGNATURE, _ = signature_of(FACTORY_ABI, entry_type="event", name="DepositSwept")
PREDICT_SELECTOR = keccak(text=PREDICT_SIGNATURE)[:4]
SWEEP_SELECTOR = keccak(text=SWEEP_SIGNATURE)[:4]
DEPOSIT_SWEPT_TOPIC = "0x" + keccak(text=DEPOSIT_SWEPT_SIGNATURE).hex()


def address_from_topic(topic: str) -> str:
    return to_checksum_address("0x" + topic.removeprefix("0x")[-40:])


class DepositFactoryApiClient:
    """One chain's factory. Calls go through the endpoint the caller's tick (or request) uses."""

    def __init__(self, factory_address: str) -> None:
        self.factory_address = to_checksum_address(factory_address)

    async def predict_deposit_address(
        self, endpoint: EvmRpcEndpoint, *, invoice_salt: bytes, destination_address: str
    ) -> str:
        """The factory's own ``predictDepositAddress`` (one eth_call), never a local copy of the init code."""
        call_data = PREDICT_SELECTOR + encode(PREDICT_INPUT_TYPES, [invoice_salt, destination_address])
        return_data = await endpoint.call_contract(self.factory_address, call_data=call_data)
        (predicted,) = decode(["address"], return_data)
        return to_checksum_address(predicted)

    def encode_sweep_call(
        self, *, invoice_salt: bytes, destination_address: str, token_addresses: Sequence[str]
    ) -> bytes:
        """Calldata of ``sweepToDestination``. Token contracts only, never address zero."""
        return SWEEP_SELECTOR + encode(SWEEP_INPUT_TYPES, [invoice_salt, destination_address, list(token_addresses)])

    def decode_deposit_swept(self, log_entry: LogEntry) -> DepositSweptEvent | None:
        """The event when ``log_entry`` is this factory's DepositSwept, else None."""
        if log_entry.address != self.factory_address or not log_entry.topics:
            return None
        if log_entry.topics[0] != DEPOSIT_SWEPT_TOPIC or len(log_entry.topics) != 4:
            return None
        token_address = address_from_topic(log_entry.topics[3])
        (amount,) = decode(["uint256"], bytes.fromhex(log_entry.data.removeprefix("0x")))
        return DepositSweptEvent(
            deposit_address=address_from_topic(log_entry.topics[1]),
            destination_address=address_from_topic(log_entry.topics[2]),
            token_address=None if token_address == ZERO_ADDRESS else token_address,
            amount=amount,
            block_number=log_entry.block_number,
            block_hash=log_entry.block_hash,
            transaction_hash=log_entry.transaction_hash,
            log_index=log_entry.log_index,
        )

    async def read_deposit_swept_events(
        self, endpoint: EvmRpcEndpoint, *, from_block: int, to_block: int
    ) -> list[DepositSweptEvent]:
        log_entries = await endpoint.read_logs(
            addresses=[self.factory_address], topics=[DEPOSIT_SWEPT_TOPIC], from_block=from_block, to_block=to_block
        )
        events = [self.decode_deposit_swept(log_entry) for log_entry in log_entries]
        return [event for event in events if event is not None]
