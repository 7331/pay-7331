"""OperatorKeyService: operator key states on one session (2026-10-13; PLAN.md section 5, operator keys).

- A configured key gets its ``operator_key_states`` row (``active``) when the watcher first sees it, and its
  ``operator_nonces`` row is then made or moved up to its ``latest`` transaction count, the highest any endpoint
  answers (never down): what the key did before this system first saw it is adopted, not taken as a leak. A row older
  than the state row counts too: main's allocator before 2026-10-13 absorbed by-hand sends at ``pending``, so the
  refund key's row can be behind.
- Leak: a key whose nonce (at ``latest``, or ``pending`` when the runner is about to sign) is past what the allocator
  handed out (``operator_nonces.next_nonce``) was used elsewhere. It is frozen: it never signs and gets no work again,
  and its in-flight operations end by the existing nonce-taken-elsewhere path. Only the owner's ``operator
  unfreeze-key`` makes it active again, moving its ``next_nonce`` up to the chain's pending count so it is not frozen
  again for the nonces it used meanwhile. That CLI (and ``retire-key``) first refuses a frozen key with a nonce it
  was handed but never used (``abandoned_nonce_service.py``).
- ``operator retire-key``: no new work; once it has no active operation the watcher sends what it holds back to the
  treasury Safe (``operator_key_maintenance_service.py``).
"""

from datetime import UTC, datetime
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession

from app.payments.settlements.enums import OperatorKeyRole, OperatorKeyStatus
from app.payments.settlements.errors import OperatorKeyNotFoundError, OperatorKeyStatusConflictError
from app.payments.settlements.log_events import OperatorKeyStatusChanged
from app.payments.settlements.models import OperatorKeyState, OperatorNonce
from app.payments.settlements.operator_keys import (
    OperatorKeyAddresses,
    OperatorKeyStateValue,
    choose_sweep_key_address,
)
from app.payments.settlements.repository import SettlementRepository
from app.shared import logging as log

logger = log.get_logger(__name__)

NEW_KEY_REASON = "configured key first seen"


def operator_key_state_value_of(key_state: OperatorKeyState) -> OperatorKeyStateValue:
    return OperatorKeyStateValue(
        key_id=key_state.id,
        chain_key=key_state.chain_key,
        operator_address=key_state.operator_address,
        role=key_state.role,
        status=key_state.status,
        reason=key_state.reason,
        changed_at=key_state.changed_at,
    )


class OperatorKeyService:
    def __init__(self, *, repository: SettlementRepository) -> None:
        self.repository = repository

    @classmethod
    def on_session(cls, session: AsyncSession) -> Self:
        return cls(repository=SettlementRepository(session=session))

    async def ensure_key_states(
        self, chain_key: str, *, addresses: OperatorKeyAddresses, latest_nonce_by_address: dict[str, int]
    ) -> list[OperatorKeyStateValue]:
        """A row for every configured key; a key seen for the first time is adopted at its ``latest`` count (module
        docstring). Every row of the chain, configured or not, comes back. A configured key's role follows the
        configuration."""
        now = datetime.now(UTC)
        for address in addresses.all_addresses:
            role = addresses.role_of(address) or OperatorKeyRole.SWEEPER
            state_inserted = await self.repository.insert_operator_key_state_if_missing(
                OperatorKeyState(
                    chain_key=chain_key,
                    operator_address=address,
                    role=role,
                    status=OperatorKeyStatus.ACTIVE,
                    reason=NEW_KEY_REASON,
                    changed_at=now,
                )
            )
            latest_nonce = latest_nonce_by_address.get(address)
            if latest_nonce is None:
                continue
            await self.repository.insert_operator_nonce_if_missing(
                chain_key, operator_address=address, next_nonce=latest_nonce
            )
            if state_inserted:
                await self.repository.raise_operator_next_nonce(
                    chain_key, operator_address=address, minimum_next_nonce=latest_nonce
                )
        key_states = await self.repository.list_operator_key_states(chain_key)
        for key_state in key_states:
            role = addresses.role_of(key_state.operator_address)
            if role is not None and key_state.role != role:
                key_state.role = role
        await self.repository.session.flush()
        return [operator_key_state_value_of(key_state) for key_state in key_states]

    async def list_key_states(self, chain_key: str | None = None) -> list[OperatorKeyStateValue]:
        return [
            operator_key_state_value_of(key_state)
            for key_state in await self.repository.list_operator_key_states(chain_key)
        ]

    async def find_unusable_addresses(self, chain_key: str) -> set[str]:
        """Frozen and retired keys: no new work and, frozen, no signature at all."""
        return {
            key_state.operator_address
            for key_state in await self.repository.list_operator_key_states(chain_key)
            if key_state.status != OperatorKeyStatus.ACTIVE
        }

    async def choose_sweep_key_address(self, chain_key: str, *, addresses: OperatorKeyAddresses) -> str | None:
        return choose_sweep_key_address(
            addresses,
            unusable_addresses=await self.find_unusable_addresses(chain_key),
            active_operation_counts=await self.repository.count_active_operations_by_operator(chain_key),
        )

    async def freeze_key(self, chain_key: str, *, operator_address: str, role: OperatorKeyRole, reason: str) -> bool:
        """``frozen`` (a row is made if the key has none yet); True when this call froze it."""
        key_state = await self.repository.find_operator_key_state(
            chain_key, operator_address=operator_address, lock=True
        )
        if key_state is None:
            await self.repository.insert_operator_key_state_if_missing(
                OperatorKeyState(
                    chain_key=chain_key,
                    operator_address=operator_address,
                    role=role,
                    status=OperatorKeyStatus.ACTIVE,
                    reason=NEW_KEY_REASON,
                    changed_at=datetime.now(UTC),
                )
            )
            key_state = await self.repository.find_operator_key_state(
                chain_key, operator_address=operator_address, lock=True
            )
        if key_state is None or key_state.status == OperatorKeyStatus.FROZEN:
            return False
        await self.change_status(key_state, status=OperatorKeyStatus.FROZEN, reason=reason)
        return True

    async def retire_key(self, chain_key: str, *, operator_address: str) -> OperatorKeyStateValue:
        key_state = await self.lock_key_state(chain_key, operator_address=operator_address)
        if key_state.status == OperatorKeyStatus.RETIRED:
            raise OperatorKeyStatusConflictError("that key is already retired")
        await self.change_status(key_state, status=OperatorKeyStatus.RETIRED, reason="retired by the owner")
        return operator_key_state_value_of(key_state)

    async def unfreeze_key(
        self, chain_key: str, *, operator_address: str, reason: str, pending_transaction_count: int
    ) -> OperatorKeyStateValue:
        """``active`` again, its allocator row moved up to the chain's pending count (the nonces used elsewhere)."""
        key_state = await self.lock_key_state(chain_key, operator_address=operator_address)
        if key_state.status != OperatorKeyStatus.FROZEN:
            raise OperatorKeyStatusConflictError(f"that key is {key_state.status.value}, not frozen")
        nonce_row = await self.repository.lock_operator_nonce(chain_key, operator_address=operator_address)
        if nonce_row is None:
            await self.repository.insert_operator_nonce(
                OperatorNonce(
                    chain_key=chain_key, operator_address=operator_address, next_nonce=pending_transaction_count
                )
            )
        else:
            nonce_row.next_nonce = max(nonce_row.next_nonce, pending_transaction_count)
        await self.change_status(key_state, status=OperatorKeyStatus.ACTIVE, reason=f"unfrozen by the owner: {reason}")
        return operator_key_state_value_of(key_state)

    async def lock_key_state(self, chain_key: str, *, operator_address: str) -> OperatorKeyState:
        key_state = await self.repository.find_operator_key_state(
            chain_key, operator_address=operator_address, lock=True
        )
        if key_state is None:
            raise OperatorKeyNotFoundError(f"no operator key {operator_address} on {chain_key}")
        return key_state

    async def change_status(self, key_state: OperatorKeyState, *, status: OperatorKeyStatus, reason: str) -> None:
        previous_status = key_state.status
        key_state.status = status
        key_state.reason = reason[:200]
        key_state.changed_at = datetime.now(UTC)
        await self.repository.session.flush()
        log.emit(
            logger,
            event=OperatorKeyStatusChanged(
                chain_key=key_state.chain_key,
                key_id=key_state.id,
                operator_address=key_state.operator_address,
                previous_status=previous_status.value,
                status=status.value,
                reason=key_state.reason,
            ),
        )
