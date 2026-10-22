"""The unit per-IP controls count and store: the rate limits, the open-invoice cap and the sign-up screening
cache, plus the coarser IPv6 /48 an address sits in, which the public-write budget and the open-invoice cap also
count.

No FastAPI here: the watcher's import chain reaches it through the invoice creation limits.
"""

import hashlib
import ipaddress

# One subscriber usually holds a whole IPv6 /64, so per-IP limits count the /64 (2026-10-09).
IPV6_SUBSCRIBER_PREFIX_LENGTH = 64
# A /48 holds 65,536 /64s, so the /64 limits alone would let one holder of a /48 multiply them; a coarser /48 bucket
# sits on top of them (IPv4 has none). A /48 is often not one subscriber, though: RFC 6177 section 2 drops the
# /48-per-site default, and a provider that gives each household a /56 (RIPE-690 section 4.2.2) or each phone a /64
# (section 4.2.4) puts up to 256 unrelated households or 65,536 phones in one /48. So the /48 limits sit well above
# the /64's and are env-tunable (PAY_MAXIMUM_OPEN_INVOICES_PER_IPV6_48, PAY_PUBLIC_WRITES_PER_MINUTE_PER_IPV6_48).
IPV6_SITE_PREFIX_LENGTH = 48


def client_network_of(client_ip_address: str) -> str:
    """What per-IP limits count: an IPv4 address itself, an IPv6 address's /64."""
    try:
        address = ipaddress.ip_address(client_ip_address)
    except ValueError:
        return client_ip_address
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/{IPV6_SUBSCRIBER_PREFIX_LENGTH}", strict=False))
    return str(address)


def client_ipv6_48_network_of(client_ip_address: str) -> str | None:
    """The IPv6 /48 an IPv6 address sits in; None for IPv4 (an IPv4-mapped IPv6 address included) and anything that
    does not parse, which the /48 limits leave alone."""
    try:
        address = ipaddress.ip_address(client_ip_address)
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv6Address) or address.ipv4_mapped is not None:
        return None
    return str(ipaddress.ip_network(f"{address}/{IPV6_SITE_PREFIX_LENGTH}", strict=False))


def hash_client_network(client_ip_address: str, *, salt: str) -> str:
    """The salted hash a client is stored and counted under; the IP itself is never stored."""
    return hashlib.sha256(f"{salt}:{client_network_of(client_ip_address)}".encode()).hexdigest()


def hash_client_ipv6_48_network(client_ip_address: str, *, salt: str) -> str | None:
    """The salted hash of an IPv6 address's /48, counted on top of its /64; None for IPv4."""
    network = client_ipv6_48_network_of(client_ip_address)
    if network is None:
        return None
    return hashlib.sha256(f"{salt}:{network}".encode()).hexdigest()
