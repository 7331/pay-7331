"""Queries for accounts, account_passkeys, account_sessions, passkey_challenges, account_api_tokens and
account_passkey_enrollment_links; no decisions."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ARRAY, Text, delete, func, or_, select, type_coerce, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.postgres.client import SIDE_TRANSACTION_ENGINE_SESSION_INFO_KEY
from app.payments.accounts.models import (
    Account,
    AccountApiToken,
    AccountPasskey,
    AccountPasskeyEnrollmentLink,
    AccountSession,
    PasskeyChallenge,
)


class AccountRepository:
    def __init__(self, *, session: AsyncSession) -> None:
        self.session = session

    # ---- Challenges ----

    async def insert_challenge(self, challenge: PasskeyChallenge) -> None:
        self.session.add(challenge)
        await self.session.flush()

    async def consume_unexpired_challenge(
        self, challenge_hash: str, *, purpose: str, now: datetime
    ) -> PasskeyChallenge | None:
        """Deletes the matching unexpired challenge and returns it. The delete makes it single use, also under two
        concurrent verifies (the second finds no row)."""
        result = await self.session.execute(
            delete(PasskeyChallenge)
            .where(
                PasskeyChallenge.challenge_hash == challenge_hash,
                PasskeyChallenge.purpose == purpose,
                PasskeyChallenge.expires_at > now,
            )
            .returning(PasskeyChallenge)
        )
        return result.scalar_one_or_none()

    async def find_unexpired_challenge(
        self, challenge_hash: str, *, purpose: str, now: datetime
    ) -> PasskeyChallenge | None:
        """The matching unexpired challenge, left in place: a fresh passkey check consumes it only once the assertion
        verified (``consume_challenge_and_record_passkey_use_committed``)."""
        return await self.session.scalar(
            select(PasskeyChallenge).where(
                PasskeyChallenge.challenge_hash == challenge_hash,
                PasskeyChallenge.purpose == purpose,
                PasskeyChallenge.expires_at > now,
            )
        )

    async def consume_challenge_and_record_passkey_use_committed(
        self, passkey_challenge_id: int, *, account_passkey_id: int, sign_count: int, used_at: datetime
    ) -> bool:
        """Deletes the challenge and records the passkey's use in a transaction of their own, on a connection of the
        side transaction engine (``Database``), committed before this returns, so a refusal later in the request
        (which rolls the request's transaction back) cannot bring the challenge back for a second use. False when the
        row was already gone (a concurrent use). Not the request pool: a request holding one of its connections and
        an account row lock while it waits for a second would exhaust it under concurrent checks.

        Neither statement waits on the request's own locks: deleting a challenge row and updating a passkey's
        non-key columns take no lock on the ``accounts`` row the request holds ``FOR NO KEY UPDATE``. The sign count
        only rises (``greatest``), so two concurrent uses of one passkey never write it back down."""
        side_transaction_engine = self.session.info.get(SIDE_TRANSACTION_ENGINE_SESSION_INFO_KEY)
        if side_transaction_engine is None:
            raise RuntimeError("this process opened no side transaction engine (Database side_transaction_pool_size)")
        async with (
            AsyncSession(bind=side_transaction_engine, expire_on_commit=False) as own_session,
            own_session.begin(),
        ):
            consumed_challenge_id = await own_session.scalar(
                delete(PasskeyChallenge)
                .where(PasskeyChallenge.id == passkey_challenge_id)
                .returning(PasskeyChallenge.id)
            )
            if consumed_challenge_id is None:
                return False
            await own_session.execute(
                update(AccountPasskey)
                .where(AccountPasskey.id == account_passkey_id)
                .values(sign_count=func.greatest(AccountPasskey.sign_count, sign_count), last_used_at=used_at)
            )
        return True

    async def delete_expired_challenges(self, *, now: datetime) -> None:
        await self.session.execute(delete(PasskeyChallenge).where(PasskeyChallenge.expires_at <= now))

    # ---- Accounts ----

    async def insert_account(self, account: Account) -> Account:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account

    async def find_account(self, account_id: int) -> Account | None:
        return await self.session.get(Account, account_id)

    async def list_existing_account_ids(self, account_ids: Sequence[int]) -> set[int]:
        return set(await self.session.scalars(select(Account.id).where(Account.id.in_(account_ids))))

    async def merge_accepted_asset_symbols(
        self, account_id: int, *, accepted_asset_symbols_by_chain: dict[str, list[str]]
    ) -> None:
        """Replaces the named chains' lists in one statement (jsonb ``||``); other chains' lists stay."""
        await self.session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                accepted_asset_symbols_by_chain=Account.accepted_asset_symbols_by_chain.op("||")(
                    type_coerce(accepted_asset_symbols_by_chain, JSONB)
                )
            )
        )

    async def merge_confirmations_final_by_chain(
        self, account_id: int, *, confirmations_final_by_chain: dict[str, int | None]
    ) -> None:
        """Replaces the named chains' counts in one statement (jsonb ``||``, then ``-`` for the chains whose saved
        count a `null` clears); other chains' counts stay."""
        counts_by_chain = {key: count for key, count in confirmations_final_by_chain.items() if count is not None}
        cleared_chain_keys = [key for key, count in confirmations_final_by_chain.items() if count is None]
        await self.session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                confirmations_final_by_chain=Account.confirmations_final_by_chain.op("||")(
                    type_coerce(counts_by_chain, JSONB)
                ).op("-")(type_coerce(cleared_chain_keys, ARRAY(Text)))
            )
        )

    async def merge_invoice_payment_terms_by_chain(
        self,
        account_id: int,
        *,
        invoice_lifetime_seconds_by_chain: dict[str, int | None],
        accuracy_percent_by_chain: dict[str, str | None],
    ) -> None:
        """Replaces the named chains' payment windows and shortfalls in one statement (jsonb ``||``, then ``-`` for the
        chains a `null` clears); other chains' stay."""
        await self.session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                invoice_lifetime_seconds_by_chain=Account.invoice_lifetime_seconds_by_chain.op("||")(
                    type_coerce(
                        {key: value for key, value in invoice_lifetime_seconds_by_chain.items() if value is not None},
                        JSONB,
                    )
                ).op("-")(
                    type_coerce(
                        [key for key, value in invoice_lifetime_seconds_by_chain.items() if value is None], ARRAY(Text)
                    )
                ),
                accuracy_percent_by_chain=Account.accuracy_percent_by_chain.op("||")(
                    type_coerce(
                        {key: value for key, value in accuracy_percent_by_chain.items() if value is not None}, JSONB
                    )
                ).op("-")(
                    type_coerce([key for key, value in accuracy_percent_by_chain.items() if value is None], ARRAY(Text))
                ),
            )
        )

    async def find_invoice_payment_terms_by_chain(self, account_id: int) -> tuple[dict[str, int], dict[str, str]]:
        """The account's saved payment windows and shortfalls per chain key, as saved."""
        saved_row = (
            await self.session.execute(
                select(Account.invoice_lifetime_seconds_by_chain, Account.accuracy_percent_by_chain).where(
                    Account.id == account_id
                )
            )
        ).one_or_none()
        if saved_row is None:
            return {}, {}
        return dict(saved_row[0] or {}), dict(saved_row[1] or {})

    async def find_confirmations_final_by_chain(self, account_id: int) -> dict[str, int]:
        """The account's saved confirmations per Monero chain key, as saved (a chain of another kind's key cannot be
        in there: the save validates)."""
        saved_by_chain = await self.session.scalar(
            select(Account.confirmations_final_by_chain).where(Account.id == account_id)
        )
        return dict(saved_by_chain or {})

    async def find_account_for_update(self, account_id: int) -> Account | None:
        """The account row locked (``FOR NO KEY UPDATE``) until the transaction ends, so two payout address changes
        queue. Not ``FOR UPDATE``: that also blocks the ``FOR KEY SHARE`` a foreign-key insert takes on the row (a
        message queued for the account), which deadlocked a Discord webhook change with queuing
        (https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS)."""
        return await self.session.scalar(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update(key_share=True)
            # The session may already hold this account (the session check loaded it before the lock): refresh it
            # from the locked row, not from the identity map.
            .execution_options(populate_existing=True)
        )

    async def update_payout_address(self, account_id: int, *, payout_address: str) -> None:
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(payout_address=payout_address)
        )

    async def update_support_contact(self, account_id: int, *, support_contact: str | None) -> None:
        await self.session.execute(
            update(Account).where(Account.id == account_id).values(support_contact=support_contact)
        )

    async def find_support_contact(self, account_id: int) -> str | None:
        return await self.session.scalar(select(Account.support_contact).where(Account.id == account_id))

    async def username_exists(self, username: str) -> bool:
        return await self.session.scalar(select(Account.id).where(Account.username == username)) is not None

    # ---- Passkeys ----

    async def insert_passkey(self, account_passkey: AccountPasskey) -> AccountPasskey:
        self.session.add(account_passkey)
        await self.session.flush()
        await self.session.refresh(account_passkey)
        return account_passkey

    async def find_passkey_by_credential_id(self, credential_id: bytes) -> AccountPasskey | None:
        """Read afresh even when the session already holds the passkey (the session check loads it), so its sign
        count is the stored one, not the one read before another request used it."""
        return await self.session.scalar(
            select(AccountPasskey)
            .where(AccountPasskey.credential_id == credential_id)
            .execution_options(populate_existing=True)
        )

    async def list_account_passkeys(self, account_id: int) -> list[AccountPasskey]:
        return list(
            await self.session.scalars(
                select(AccountPasskey).where(AccountPasskey.account_id == account_id).order_by(AccountPasskey.id)
            )
        )

    async def record_passkey_use(self, account_passkey_id: int, *, sign_count: int, used_at: datetime) -> None:
        """The sign count only rises (``greatest``): a concurrent use that stored a higher one keeps it."""
        await self.session.execute(
            update(AccountPasskey)
            .where(AccountPasskey.id == account_passkey_id)
            .values(sign_count=func.greatest(AccountPasskey.sign_count, sign_count), last_used_at=used_at)
        )

    async def update_account_passkey_label(
        self, account_passkey_public_id: str, *, account_id: int, label: str
    ) -> AccountPasskey | None:
        return await self.session.scalar(
            update(AccountPasskey)
            .where(AccountPasskey.public_id == account_passkey_public_id, AccountPasskey.account_id == account_id)
            .values(label=label)
            .returning(AccountPasskey)
        )

    async def delete_account_passkey(self, account_passkey_id: int, *, account_id: int) -> bool:
        """Its sessions go with it (ON DELETE CASCADE)."""
        result = await self.session.execute(
            delete(AccountPasskey)
            .where(AccountPasskey.id == account_passkey_id, AccountPasskey.account_id == account_id)
            .returning(AccountPasskey.id)
        )
        return result.scalar_one_or_none() is not None

    # ---- Sessions ----

    async def insert_session(self, token_hash: str, *, account_passkey_id: int, expires_at: datetime) -> AccountSession:
        account_session = AccountSession(
            token_hash=token_hash, account_passkey_id=account_passkey_id, expires_at=expires_at
        )
        self.session.add(account_session)
        await self.session.flush()
        return account_session

    async def find_unexpired_session(
        self, token_hash: str, *, now: datetime
    ) -> tuple[AccountSession, AccountPasskey, Account] | None:
        row = (
            await self.session.execute(
                select(AccountSession, AccountPasskey, Account)
                .join(AccountPasskey, AccountPasskey.id == AccountSession.account_passkey_id)
                .join(Account, Account.id == AccountPasskey.account_id)
                .where(AccountSession.token_hash == token_hash, AccountSession.expires_at > now)
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1], row[2])

    async def list_unexpired_session_account_ids(
        self, account_session_ids: list[int], *, now: datetime
    ) -> dict[int, int]:
        """Account id by session id, for those of the sessions that still exist and have not expired."""
        rows = await self.session.execute(
            select(AccountSession.id, AccountPasskey.account_id)
            .join(AccountPasskey, AccountPasskey.id == AccountSession.account_passkey_id)
            .where(AccountSession.id.in_(account_session_ids), AccountSession.expires_at > now)
        )
        return dict(rows.tuples().all())

    async def delete_session_by_token_hash(self, token_hash: str) -> int | None:
        result = await self.session.execute(
            delete(AccountSession).where(AccountSession.token_hash == token_hash).returning(AccountSession.id)
        )
        return result.scalar_one_or_none()

    async def delete_expired_sessions(self, *, now: datetime) -> None:
        await self.session.execute(delete(AccountSession).where(AccountSession.expires_at <= now))

    # ---- API tokens ----

    async def insert_api_token(self, account_api_token: AccountApiToken) -> AccountApiToken:
        self.session.add(account_api_token)
        await self.session.flush()
        await self.session.refresh(account_api_token)
        return account_api_token

    async def count_account_api_tokens(self, account_id: int) -> int:
        return (await self.session.scalar(select(func.count()).where(AccountApiToken.account_id == account_id))) or 0

    async def list_account_api_tokens(self, account_id: int) -> list[AccountApiToken]:
        return list(
            await self.session.scalars(
                select(AccountApiToken).where(AccountApiToken.account_id == account_id).order_by(AccountApiToken.id)
            )
        )

    async def delete_account_api_token(self, account_api_token_public_id: str, *, account_id: int) -> int | None:
        """The deleted token's internal id; None for an unknown public id or another account's."""
        result = await self.session.execute(
            delete(AccountApiToken)
            .where(AccountApiToken.public_id == account_api_token_public_id, AccountApiToken.account_id == account_id)
            .returning(AccountApiToken.id)
        )
        return result.scalar_one_or_none()

    async def find_api_token_with_account(self, token_hash: str) -> tuple[AccountApiToken, Account] | None:
        row = (
            await self.session.execute(
                select(AccountApiToken, Account)
                .join(Account, Account.id == AccountApiToken.account_id)
                .where(AccountApiToken.token_hash == token_hash)
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1])

    async def find_api_token_account_id(self, token_hash: str) -> int | None:
        return await self.session.scalar(
            select(AccountApiToken.account_id).where(AccountApiToken.token_hash == token_hash)
        )

    async def record_api_token_use(
        self, account_api_token_id: int, *, used_at: datetime, used_before: datetime
    ) -> None:
        """Sets ``last_used_at`` unless it is already at or after ``used_before`` (one write a minute at most)."""
        await self.session.execute(
            update(AccountApiToken)
            .where(
                AccountApiToken.id == account_api_token_id,
                or_(AccountApiToken.last_used_at.is_(None), AccountApiToken.last_used_at < used_before),
            )
            .values(last_used_at=used_at)
        )

    # ---- Passkey enrollment links (2026-10-15) ----

    async def find_account_by_username(self, username: str) -> Account | None:
        return await self.session.scalar(select(Account).where(Account.username == username))

    async def replace_unused_passkey_enrollment_links(
        self, enrollment_link: AccountPasskeyEnrollmentLink
    ) -> AccountPasskeyEnrollmentLink:
        """Deletes the account's unused links, so only the newest one works, and stores this one."""
        await self.session.execute(
            delete(AccountPasskeyEnrollmentLink).where(
                AccountPasskeyEnrollmentLink.account_id == enrollment_link.account_id,
                AccountPasskeyEnrollmentLink.used_at.is_(None),
            )
        )
        self.session.add(enrollment_link)
        await self.session.flush()
        return enrollment_link

    async def find_passkey_enrollment_link(
        self, token_hash: str, *, lock: bool = False
    ) -> AccountPasskeyEnrollmentLink | None:
        """With ``lock``, the row is held (``FOR UPDATE``) until the transaction ends, so two enrollments queue."""
        statement = select(AccountPasskeyEnrollmentLink).where(AccountPasskeyEnrollmentLink.token_hash == token_hash)
        return await self.session.scalar(statement.with_for_update() if lock else statement)

    async def mark_passkey_enrollment_link_used(self, enrollment_link_id: int, *, used_at: datetime) -> None:
        await self.session.execute(
            update(AccountPasskeyEnrollmentLink)
            .where(AccountPasskeyEnrollmentLink.id == enrollment_link_id)
            .values(used_at=used_at)
        )
