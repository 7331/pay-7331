"""Vulture allow-list: names the dead-code scan reports that are not dead (https://github.com/jendrikseipp/vulture#whitelists).

Vulture parses this file as ordinary code, so a name referenced here counts as used. A new entry means a genuine false
positive; a finding that is really dead code is deleted instead.
"""

# app/infrastructure/postgres/base.py: SQLAlchemy calls TypeDecorator.process_bind_param and process_result_value with
# the dialect; the override keeps the signature it is called with.
dialect

# app/payments/pages/router.py: the path parameters name what the route matches; the single-page app reads the path
# itself, so the handlers take them and leave them unused.
invoice_reference
payment_link_reference
unknown_page_path
