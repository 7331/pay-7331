"""Invoice settings (env ``PAY_DEFAULT_DESTINATION_ADDRESS``, ``PAY_OPERATOR_ACCOUNT_IDS``, ``PAY_MINIMUM_REFUND_USD``,
``PAY_MAXIMUM_OPEN_INVOICES_PER_IP``, ``PAY_MAXIMUM_OPEN_INVOICES_PER_IPV6_48``,
``PAY_MAXIMUM_OPEN_PUBLIC_INVOICES_PER_CHAIN``, ``PAY_MAXIMUM_OPEN_INVOICES_PER_ACCOUNT``,
``PAY_PUBLIC_WRITES_PER_MINUTE_PER_IPV6_48``, ``PAY_API_TOKEN_INVOICE_CREATES_PER_MINUTE``,
``PAY_INVOICE_SALT_SEED``)."""

from decimal import Decimal
from functools import lru_cache

from eth_utils import is_address, to_checksum_address
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INVOICE_SALT_SEED_BYTES = 32
# accounts.id is a bigint identity, so an id outside 1 .. 2**63 - 1 names no account.
MAXIMUM_ACCOUNT_ID = 2**63 - 1


class InvoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAY_", extra="ignore", hide_input_in_errors=True)

    # The operator's own payout address. Only invoices paying it get refunds from the operator float; every other
    # receiver keeps an excess or an underpaid amount and settles with its payer (2026-10-09). Empty: no refunds.
    default_destination_address: str = ""
    # The operator's own accounts, by id, comma-separated; order matters. Operator alerts go to the first, and only
    # these accounts' own invoices paying ``default_destination_address`` get float refunds: payout addresses are not
    # verified, so anyone can sign up with the operator's address or make a signed-out invoice paying it. Empty: no
    # account gets operator alerts and no invoice gets float refunds. Read as ``operator_account_ids``.
    operator_account_ids_text: str = Field(default="", validation_alias="PAY_OPERATOR_ACCOUNT_IDS")
    # An excess below this is kept, and a refund below it is not offered (PLAN.md section 4).
    minimum_refund_usd: Decimal = Decimal("1.00")
    # Unclosed invoices one client network may have made at once, signed in or not (PLAN.md section 9).
    maximum_open_invoices_per_ip: int = 5
    # The same count over a whole IPv6 /48 (app/shared/client_network.py), on top of the /64's: one holder of a /48
    # has 65,536 /64s. IPv4 has no such bucket.
    maximum_open_invoices_per_ipv6_48: int = 50
    # Unclosed public invoices (made signed out, or by a quantity link's payer) one chain may have at once, whatever
    # networks made them: every open invoice is read on every watcher tick, so this bounds what anonymous makers add
    # to it. Creation past it answers 429 ``public_invoice_capacity_reached``.
    maximum_open_public_invoices_per_chain: int = 2000
    # Invoices made with an API token are capped per account instead (an account's server sits on one data-centre
    # IP): unclosed invoices one account may have when it makes another with a token (2026-10-12).
    maximum_open_invoices_per_account: int = 500
    # `POST /invoices` with an API token, per account per minute (app/rate_limit_middleware.py; 2026-10-12).
    api_token_invoice_creates_per_minute: int = 60
    # Public writes per minute from one IPv6 /48 together, on top of each /64's own budget
    # (app/rate_limit_middleware.py).
    public_writes_per_minute_per_ipv6_48: int = 60
    # 32 bytes as 64 hex digits (`openssl rand -hex 32`). Each new invoice's CREATE2 salt is HMAC-SHA256 of its id
    # under this key (deposit_salts.py), so a deposit can be swept again from the id, its destination and this seed
    # if Postgres is lost. Required, with no default and no random fallback: the API, watcher and CLI refuse to start
    # without it (2026-10-12, PLAN.md section 3). Never logged.
    invoice_salt_seed: SecretStr

    @field_validator("default_destination_address")
    @classmethod
    def checksum_default_destination_address(cls, value: str) -> str:
        if not value:
            return ""
        if not is_address(value):
            raise ValueError("PAY_DEFAULT_DESTINATION_ADDRESS must be a 0x address")
        return to_checksum_address(value)

    @field_validator("operator_account_ids_text")
    @classmethod
    def check_operator_account_ids_text(cls, value: str) -> str:
        account_id_texts = split_operator_account_id_texts(value)
        if not all(
            account_id_text.isascii()
            and account_id_text.isdecimal()
            and 1 <= int(account_id_text) <= MAXIMUM_ACCOUNT_ID
            for account_id_text in account_id_texts
        ):
            raise ValueError("PAY_OPERATOR_ACCOUNT_IDS must be comma-separated account ids from 1 to 2**63 - 1")
        if len({int(account_id_text) for account_id_text in account_id_texts}) != len(account_id_texts):
            raise ValueError("PAY_OPERATOR_ACCOUNT_IDS lists an account id twice")
        return value

    @property
    def operator_account_ids(self) -> tuple[int, ...]:
        """``PAY_OPERATOR_ACCOUNT_IDS`` in its order."""
        return tuple(
            int(account_id_text) for account_id_text in split_operator_account_id_texts(self.operator_account_ids_text)
        )

    @property
    def operator_alert_account_id(self) -> int | None:
        """The account operator alerts go to: the first of ``PAY_OPERATOR_ACCOUNT_IDS``."""
        return self.operator_account_ids[0] if self.operator_account_ids else None

    @field_validator("invoice_salt_seed")
    @classmethod
    def check_invoice_salt_seed(cls, value: SecretStr) -> SecretStr:
        # RFC 2104 section 3: a key shorter than the hash output (32 bytes for SHA-256) is "strongly discouraged".
        try:
            seed_bytes = bytes.fromhex(value.get_secret_value())
        except ValueError:
            seed_bytes = b""
        if len(seed_bytes) != INVOICE_SALT_SEED_BYTES:
            raise ValueError("PAY_INVOICE_SALT_SEED must be 64 hex digits (openssl rand -hex 32)")
        return value


def split_operator_account_id_texts(operator_account_ids_text: str) -> list[str]:
    return [
        account_id_text.strip() for account_id_text in operator_account_ids_text.split(",") if account_id_text.strip()
    ]


@lru_cache
def read_invoice_settings() -> InvoiceSettings:
    return InvoiceSettings()
