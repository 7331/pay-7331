"""Calls to Safe v1.4.1 and Safe's Allowance Module v0.1.1, encoded here as raw calldata (no web3 contract objects),
knowing nothing about operator keys or invoices (PLAN.md section 5, treasury).

Every signature below is copied from the source it cites, read 2026-10-13:

- Allowance Module: https://github.com/safe-global/safe-modules/blob/allowance/v0.1.1/modules/allowances/contracts/AllowanceModule.sol
  (``setAllowance``, ``executeAllowanceTransfer``, ``getTokenAllowance``, ``addDelegate``, ``removeDelegate``,
  ``delegates``). Its ``ISafe safe`` parameter is an ``address`` in the ABI. An empty ``signature`` makes
  ``recoverSignature`` return ``msg.sender``, so a delegate that sends the transaction itself needs no signature.
  Anyone may send it with the delegate's signature over ``generateTransferHash`` instead (an operator key that has
  gas, for one that has none, 2026-10-13); the transfer still pays only ``to``, and ``payment`` stays 0.
- Safe: https://github.com/safe-global/safe-smart-account/blob/v1.4.1/contracts/Safe.sol (``setup``,
  ``execTransaction``, ``nonce``; ``Enum.Operation`` is a ``uint8``), base/ModuleManager.sol (``enableModule``,
  ``isModuleEnabled``), base/OwnerManager.sol (``getOwners``, ``getThreshold``).
- proxies/SafeProxyFactory.sol (``createProxyWithNonce``, ``proxyCreationCode``): the proxy's CREATE2 salt is
  ``keccak256(abi.encodePacked(keccak256(initializer), saltNonce))`` and its init code
  ``abi.encodePacked(proxyCreationCode, uint256(uint160(singleton)))``.
- libraries/MultiSendCallOnly.sol (``multiSend(bytes)``): each call packed as ``uint8 operation`` (0, a call),
  ``address to``, ``uint256 value``, ``uint256 dataLength``, ``bytes data``.
- An owner's pre-validated signature (``checkNSignatures``, ``v == 1``): ``r`` is the owner's address, ``s`` zero,
  accepted when ``msg.sender`` is that owner (https://docs.safe.global/advanced/smart-account-signatures).
"""

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import function_signature_to_4byte_selector, keccak, to_checksum_address

from app.infrastructure.evm_rpc_api.client import EvmRpcEndpoint
from app.infrastructure.evm_rpc_api.errors import EvmRpcUnavailableError
from app.infrastructure.evm_rpc_api.schemas import Multicall3Call, Multicall3Result
from app.infrastructure.safe_api.schemas import SafeCall, TokenAllowance

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
# The Allowance Module's resetTimeMin is a uint16 of minutes; one day.
DAILY_RESET_MINUTES = 1440
UINT96_MAXIMUM = 2**96 - 1

EXECUTE_ALLOWANCE_TRANSFER_SELECTOR = function_signature_to_4byte_selector(
    "executeAllowanceTransfer(address,address,address,uint96,address,uint96,address,bytes)"
)
# The module's constants, each the keccak of the type string it documents (checked against the source 2026-10-13).
DOMAIN_SEPARATOR_TYPEHASH = keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)")
ALLOWANCE_TRANSFER_TYPEHASH = keccak(
    text="AllowanceTransfer(address safe,address token,address to,uint96 amount,address paymentToken,uint96 payment,"
    "uint16 nonce)"
)
GET_TOKEN_ALLOWANCE_SELECTOR = function_signature_to_4byte_selector("getTokenAllowance(address,address,address)")
SET_ALLOWANCE_SELECTOR = function_signature_to_4byte_selector("setAllowance(address,address,uint96,uint16,uint32)")
ADD_DELEGATE_SELECTOR = function_signature_to_4byte_selector("addDelegate(address)")
REMOVE_DELEGATE_SELECTOR = function_signature_to_4byte_selector("removeDelegate(address,bool)")
DELEGATES_SELECTOR = function_signature_to_4byte_selector("delegates(address,uint48)")
ENABLE_MODULE_SELECTOR = function_signature_to_4byte_selector("enableModule(address)")
IS_MODULE_ENABLED_SELECTOR = function_signature_to_4byte_selector("isModuleEnabled(address)")
SETUP_SELECTOR = function_signature_to_4byte_selector(
    "setup(address[],uint256,address,bytes,address,address,uint256,address)"
)
EXEC_TRANSACTION_SELECTOR = function_signature_to_4byte_selector(
    "execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
)
SAFE_NONCE_SELECTOR = function_signature_to_4byte_selector("nonce()")
GET_OWNERS_SELECTOR = function_signature_to_4byte_selector("getOwners()")
GET_THRESHOLD_SELECTOR = function_signature_to_4byte_selector("getThreshold()")
CREATE_PROXY_WITH_NONCE_SELECTOR = function_signature_to_4byte_selector("createProxyWithNonce(address,bytes,uint256)")
PROXY_CREATION_CODE_SELECTOR = function_signature_to_4byte_selector("proxyCreationCode()")
MULTI_SEND_SELECTOR = function_signature_to_4byte_selector("multiSend(bytes)")
# Enum.Operation (safe-smart-account v1.4.1, libraries/Enum.sol): Call 0, DelegateCall 1.
OPERATION_CALL = 0
OPERATION_DELEGATE_CALL = 1


def token_or_zero(token_address: str | None) -> str:
    """The Allowance Module names the native coin ``address(0)``."""
    return token_address or ZERO_ADDRESS


def check_uint96(amount: int) -> int:
    if not 0 <= amount <= UINT96_MAXIMUM:
        raise ValueError(f"{amount} does not fit the Allowance Module's uint96")
    return amount


# Allowance Module


def encode_execute_allowance_transfer_to_delegate(
    *,
    safe_address: str,
    token_address: str | None,
    delegate_address: str,
    amount_base_units: int,
    delegate_signature: bytes,
) -> bytes:
    """``amount`` from the Safe to the delegate itself, with no relayer payment (``paymentToken`` zero, ``payment``
    0, so the sender is never paid from the allowance). An empty signature: the sender is the delegate. Else the
    delegate's 65-byte ``r || s || v`` signature over ``allowance_transfer_hash_of``, and anyone may send it."""
    return EXECUTE_ALLOWANCE_TRANSFER_SELECTOR + encode(
        ["address", "address", "address", "uint96", "address", "uint96", "address", "bytes"],
        [
            safe_address,
            token_or_zero(token_address),
            delegate_address,
            check_uint96(amount_base_units),
            ZERO_ADDRESS,
            0,
            delegate_address,
            delegate_signature,
        ],
    )


def allowance_transfer_hash_of(
    *,
    chain_id: int,
    allowance_module_address: str,
    safe_address: str,
    token_address: str | None,
    to_address: str,
    amount_base_units: int,
    allowance_nonce: int,
) -> bytes:
    """The module's ``generateTransferHash`` with no relayer payment: EIP-712 ``0x19 0x01 || domainSeparator ||
    keccak(abi.encode(ALLOWANCE_TRANSFER_TYPEHASH, safe, token, to, amount, paymentToken, payment, nonce))``, the
    domain ``EIP712Domain(uint256 chainId,address verifyingContract)``. ``allowance_nonce`` is the delegate's
    allowance nonce for that token (``getTokenAllowance``'s fifth value), so a signature fits one transfer only. A
    plain EOA signature of this hash (``v`` 27 or 28) passes ``recoverSignature``'s ``ecrecover`` branch."""
    domain_separator = keccak(
        encode(["bytes32", "uint256", "address"], [DOMAIN_SEPARATOR_TYPEHASH, chain_id, allowance_module_address])
    )
    transfer_hash = keccak(
        encode(
            ["bytes32", "address", "address", "address", "uint96", "address", "uint96", "uint16"],
            [
                ALLOWANCE_TRANSFER_TYPEHASH,
                safe_address,
                token_or_zero(token_address),
                to_address,
                check_uint96(amount_base_units),
                ZERO_ADDRESS,
                0,
                allowance_nonce,
            ],
        )
    )
    return keccak(b"\x19\x01" + domain_separator + transfer_hash)


def encode_get_token_allowance_call(
    *, allowance_module_address: str, safe_address: str, delegate_address: str, token_address: str | None
) -> Multicall3Call:
    return Multicall3Call(
        target=allowance_module_address,
        call_data=GET_TOKEN_ALLOWANCE_SELECTOR
        + encode(["address", "address", "address"], [safe_address, delegate_address, token_or_zero(token_address)]),
    )


def decode_token_allowance(result: Multicall3Result) -> TokenAllowance | None:
    if not result.succeeded:
        return None
    try:
        (values,) = decode(["uint256[5]"], result.return_data)
    except DecodingError:
        return None
    amount, spent, reset_time_minutes, last_reset_minute, nonce = values
    return TokenAllowance(
        amount=amount,
        spent=spent,
        reset_time_minutes=reset_time_minutes,
        last_reset_minute=last_reset_minute,
        nonce=nonce,
    )


def encode_delegate_slot_call(
    *, allowance_module_address: str, safe_address: str, delegate_address: str
) -> Multicall3Call:
    """``delegates(safe, uint48(delegate))``: the delegate list node, keyed by the address's low 48 bits."""
    delegate_index = int(delegate_address, 16) & (2**48 - 1)
    return Multicall3Call(
        target=allowance_module_address,
        call_data=DELEGATES_SELECTOR + encode(["address", "uint48"], [safe_address, delegate_index]),
    )


def decode_delegate_slot(result: Multicall3Result) -> str | None:
    """The delegate address stored at the slot (zero address: none), or None when unreadable."""
    if not result.succeeded:
        return None
    try:
        delegate_address, _, _ = decode(["address", "uint48", "uint48"], result.return_data)
    except DecodingError:
        return None
    return to_checksum_address(delegate_address)


def build_add_delegate_call(*, allowance_module_address: str, delegate_address: str) -> SafeCall:
    return SafeCall(
        to=allowance_module_address,
        value=0,
        data=ADD_DELEGATE_SELECTOR + encode(["address"], [delegate_address]),
        description=f"addDelegate({delegate_address})",
    )


def build_remove_delegate_call(*, allowance_module_address: str, delegate_address: str) -> SafeCall:
    return SafeCall(
        to=allowance_module_address,
        value=0,
        data=REMOVE_DELEGATE_SELECTOR + encode(["address", "bool"], [delegate_address, True]),
        description=f"removeDelegate({delegate_address}, removeAllowances=true)",
    )


def build_set_allowance_call(
    *, allowance_module_address: str, delegate_address: str, token_address: str | None, allowance_base_units: int
) -> SafeCall:
    """A daily allowance (``resetTimeMin`` 1440) from the first set's minute on (``resetBaseMin`` 0)."""
    return SafeCall(
        to=allowance_module_address,
        value=0,
        data=SET_ALLOWANCE_SELECTOR
        + encode(
            ["address", "address", "uint96", "uint16", "uint32"],
            [
                delegate_address,
                token_or_zero(token_address),
                check_uint96(allowance_base_units),
                DAILY_RESET_MINUTES,
                0,
            ],
        ),
        description=f"setAllowance({delegate_address}, {token_or_zero(token_address)}, {allowance_base_units}, "
        f"{DAILY_RESET_MINUTES}, 0)",
    )


# Safe


def build_enable_module_call(*, safe_address: str, module_address: str) -> SafeCall:
    """``enableModule`` is ``authorized``: the Safe calls itself."""
    return SafeCall(
        to=safe_address,
        value=0,
        data=ENABLE_MODULE_SELECTOR + encode(["address"], [module_address]),
        description=f"enableModule({module_address})",
    )


def encode_is_module_enabled_call(*, safe_address: str, module_address: str) -> Multicall3Call:
    return Multicall3Call(
        target=safe_address, call_data=IS_MODULE_ENABLED_SELECTOR + encode(["address"], [module_address])
    )


def decode_bool(result: Multicall3Result) -> bool | None:
    if not result.succeeded or len(result.return_data) < 32:
        return None
    return int.from_bytes(result.return_data[:32], "big") != 0


def encode_get_owners_call(safe_address: str) -> Multicall3Call:
    return Multicall3Call(target=safe_address, call_data=GET_OWNERS_SELECTOR)


def decode_owners(result: Multicall3Result) -> list[str] | None:
    if not result.succeeded:
        return None
    try:
        (owners,) = decode(["address[]"], result.return_data)
    except DecodingError:
        return None
    return [to_checksum_address(owner) for owner in owners]


def encode_get_threshold_call(safe_address: str) -> Multicall3Call:
    return Multicall3Call(target=safe_address, call_data=GET_THRESHOLD_SELECTOR)


def encode_safe_setup_initializer(*, owner_address: str, fallback_handler_address: str) -> bytes:
    """``setup``: one owner, threshold 1, the fallback handler, no module setup call and no payment."""
    return SETUP_SELECTOR + encode(
        ["address[]", "uint256", "address", "bytes", "address", "address", "uint256", "address"],
        [[owner_address], 1, ZERO_ADDRESS, b"", fallback_handler_address, ZERO_ADDRESS, 0, ZERO_ADDRESS],
    )


def encode_create_proxy_with_nonce(*, singleton_address: str, initializer: bytes, salt_nonce: int) -> bytes:
    return CREATE_PROXY_WITH_NONCE_SELECTOR + encode(
        ["address", "bytes", "uint256"], [singleton_address, initializer, salt_nonce]
    )


async def predict_safe_proxy_address(
    endpoint: EvmRpcEndpoint,
    *,
    proxy_factory_address: str,
    singleton_address: str,
    initializer: bytes,
    salt_nonce: int,
) -> str:
    """The CREATE2 address ``createProxyWithNonce`` deploys to, with the factory's own ``proxyCreationCode`` (one
    eth_call) rather than a local copy of the proxy's bytecode."""
    (creation_code,) = decode(
        ["bytes"], await endpoint.call_contract(proxy_factory_address, call_data=PROXY_CREATION_CODE_SELECTOR)
    )
    if not creation_code:
        raise EvmRpcUnavailableError("the Safe proxy factory returned no proxy creation code")
    init_code = creation_code + int(singleton_address, 16).to_bytes(32, "big")
    salt = keccak(keccak(initializer) + salt_nonce.to_bytes(32, "big"))
    address_hash = keccak(b"\xff" + bytes.fromhex(proxy_factory_address.removeprefix("0x")) + salt + keccak(init_code))
    return to_checksum_address(address_hash[-20:])


def encode_multi_send(calls: list[SafeCall]) -> bytes:
    """``multiSend`` of plain calls, packed as MultiSendCallOnly reads them."""
    packed = b"".join(
        OPERATION_CALL.to_bytes(1, "big")
        + bytes.fromhex(call.to.removeprefix("0x"))
        + call.value.to_bytes(32, "big")
        + len(call.data).to_bytes(32, "big")
        + call.data
        for call in calls
    )
    return MULTI_SEND_SELECTOR + encode(["bytes"], [packed])


def encode_exec_transaction_by_sole_owner(
    *, to: str, value: int, data: bytes, operation: int, owner_address: str
) -> bytes:
    """``execTransaction`` sent by an owner of a threshold-1 Safe with its pre-validated signature (``v`` 1, ``r``
    the owner, ``s`` 0); no gas refund (safeTxGas, baseGas and gasPrice 0, so a failing inner call reverts all)."""
    signature = int(owner_address, 16).to_bytes(32, "big") + (0).to_bytes(32, "big") + b"\x01"
    return EXEC_TRANSACTION_SELECTOR + encode(
        ["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "bytes"],
        [to, value, data, operation, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, signature],
    )
