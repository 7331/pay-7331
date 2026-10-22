"""One feed's latest answer."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class FeedReading(BaseModel):
    model_config = ConfigDict(frozen=True)

    feed_address: str
    # answer / 10**decimals, exact.
    price: Decimal
    updated_at_timestamp: int
