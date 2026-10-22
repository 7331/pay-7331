"""The operator keys of a chain: one refund key and a pool of sweeper keys (2026-10-13; PLAN.md section 5).

``OperatorKeyAddresses`` is what every process knows (the addresses, no key); ``OperatorKeyRing`` holds the signing
accounts and is opened by the watcher, the only process that signs. A new sweep gets the
usable sweeper with the fewest active operations (ties: the lowest address); with no sweeper key set, the refund key
sweeps as before. A key that is not ``active`` (frozen or retired, ``operator_key_states``) gets no new work.

In the settlement runner (``runner_service.py``), each operation is signed by the operator key its
``operator_address`` names (2026-10-13): a sweep by a sweeper key
(below), a refund and a Safe creation by the refund key, a top-up by the key it fills or by another
that pays its gas (``operator_key_maintenance_service.py``), a treasury return by the key it is for. A frozen key never
signs: its stored attempts are still rebroadcast (top-ups excepted), never bumped. Its
unmined sweeps end ``failed`` with ``operator_key_frozen`` once the newest attempt is ``NONCE_TAKEN_ELSEWHERE_AFTER``
(``runner_service.py``)
old and every endpoint was asked for a receipt; the invoice side then asks for a new sweep in that transaction, which
another key signs (its nonce may never be used, so waiting for the nonce-taken-elsewhere path could hold the money
forever). A late mine of the old attempt is harmless: ``sweepToDestination`` pays only the destination baked into the
deposit, and the second sweep then moves whatever is left, nothing included, without reverting. Its unmined top-ups
are never rebroadcast and end the same way, so no more allowance flows into a leaked key. A top-up another key
sends for a key that froze after its first attempt goes on: its replacements resend the stored call, so the frozen key
signs nothing and the sender's nonce gets used, not left as a gap that would stall the sender (the leaked delegate
could pull that allowance itself until the owner removes it, after which the call reverts). Its other operations end
by the nonce-taken-elsewhere path; a refund is never issued again this way. A nonce an operation ended unused leaves
a gap before the key's next one, so ``operator unfreeze-key`` and ``retire-key`` refuse the key until ``operator
fill-abandoned-nonces`` has used every such nonce (``abandoned_nonce_service.py``). A pending sweep whose key is
frozen, retired, no longer configured or not yet recorded moves to another sweeper when it is signed; a refund or Safe
creation waits for a usable refund key; a top-up or treasury return whose key cannot sign fails before any nonce is
spent.
"""

from datetime import datetime

from eth_account import Account
from eth_account.signers.local import LocalAccount
from pydantic import BaseModel, ConfigDict

from app.payments.settlements.configuration import read_operator_private_key, read_sweeper_private_keys
from app.payments.settlements.enums import OperatorKeyRole, OperatorKeyStatus


class OperatorKeyAddresses(BaseModel):
    model_config = ConfigDict(frozen=True)

    refund_key_address: str
    sweeper_key_addresses: list[str]

    @property
    def all_addresses(self) -> list[str]:
        return [self.refund_key_address, *self.sweeper_key_addresses]

    @property
    def sweep_candidate_addresses(self) -> list[str]:
        """The keys a sweep may be signed by: the sweepers, or the refund key when there are none."""
        return self.sweeper_key_addresses or [self.refund_key_address]

    def role_of(self, address: str) -> OperatorKeyRole | None:
        if address == self.refund_key_address:
            return OperatorKeyRole.REFUND
        if address in self.sweeper_key_addresses:
            return OperatorKeyRole.SWEEPER
        return None


class OperatorKeyStateValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    key_id: int
    chain_key: str
    operator_address: str
    role: OperatorKeyRole
    status: OperatorKeyStatus
    reason: str
    changed_at: datetime


class OperatorKeyRing:
    """One chain's signing accounts."""

    def __init__(self, *, refund_account: LocalAccount, sweeper_accounts: list[LocalAccount]) -> None:
        self.refund_account = refund_account
        self.sweeper_accounts = sweeper_accounts
        self.accounts_by_address = {account.address: account for account in [refund_account, *sweeper_accounts]}
        if len(self.accounts_by_address) != 1 + len(sweeper_accounts):
            raise ValueError("a sweeper key repeats another operator key of the chain")
        self.addresses = OperatorKeyAddresses(
            refund_key_address=refund_account.address,
            sweeper_key_addresses=[account.address for account in sweeper_accounts],
        )

    def find_account(self, address: str) -> LocalAccount | None:
        return self.accounts_by_address.get(address)


def read_operator_key_ring(chain_key: str) -> OperatorKeyRing | None:
    """The chain's keys from its environment variables; None without a refund key."""
    refund_private_key = read_operator_private_key(chain_key).get_secret_value()
    if not refund_private_key:
        return None
    return OperatorKeyRing(
        refund_account=Account.from_key(refund_private_key),
        sweeper_accounts=[
            Account.from_key(private_key.get_secret_value()) for private_key in read_sweeper_private_keys(chain_key)
        ],
    )


def choose_sweep_key_address(
    addresses: OperatorKeyAddresses, *, unusable_addresses: set[str], active_operation_counts: dict[str, int]
) -> str | None:
    """The usable sweep candidate with the fewest active operations, ties to the lowest address; else the refund key
    when it is usable (the money waits in the deposit otherwise); None when no key may sign a sweep."""
    usable = [address for address in addresses.sweep_candidate_addresses if address not in unusable_addresses]
    if not usable and addresses.refund_key_address not in unusable_addresses:
        usable = [addresses.refund_key_address]
    if not usable:
        return None
    return min(usable, key=lambda address: (active_operation_counts.get(address, 0), address.lower()))
