"""ip-api.com's record, only the fields sign-up screening asks for."""

from pydantic import BaseModel, ConfigDict, Field


class IpAddressReputation(BaseModel):
    """``proxy`` covers proxies, VPNs and Tor exits; ``hosting`` covers data centres (https://members.ip-api.com/docs/json)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    proxy: bool
    hosting: bool
    # ISO 3166-1 alpha-2; empty when ip-api has none.
    country_code: str = Field(default="", alias="countryCode")
