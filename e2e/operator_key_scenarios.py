"""Fork e2e scenarios for the operator key pool and the treasury Safe (2026-10-13; PLAN.md sections 5 and 12), run by
``polygon_fork_flow.py`` after its own:

- Sweeps paid in one block go to different sweeper keys, each sent from its own key.
- The treasury: ``treasury create-safe`` (the watcher sends it from the refund key), ``setup-transactions`` (a
  Transaction Builder batch with its checksum), ``execute-setup`` by the owner, then nothing left to set up; funded,
  the Safe tops a drained refund key back to its target.
- A sweeper with no gas at all: another key sends its top-up with its delegate signature, filling it to its target.
- An allowance cut below what was spent: a drained key pulls nothing, one ``allowance_exhausted`` alert.
- A sweeper key used outside the gateway: frozen, alerted, later sweeps go to other keys; unfrozen by the owner, it
  stays active.
- A retired sweeper: no new sweeps, its USDC and then its POL go back to the Safe.

The treasury scenarios share one Safe (``ensure_treasury``), so each also runs alone with ``--scenario``.
"""

from collections.abc import Callable
import json
import re
import time

from eth_abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
from fork_stack import (
    DEV_ADDRESSES,
    FORK_CHAIN_KEY,
    NATIVE_SYMBOL,
    NATIVE_UNIT_WEI,
    PRIMARY_CHAIN,
    SWEEPER_ACCOUNTS,
    TREASURY_OWNER_ACCOUNT,
    TREASURY_OWNER_KEY_SETTING,
    USDC_BASE_UNITS,
    FlowContext,
    FlowFailedError,
    call_anvil,
    check,
    compose_arguments,
    compose_environment,
    create_invoice,
    create_local_account,
    fund_with_usdc,
    last_sweep_final,
    pause_mining,
    read_native_balance,
    read_usdc_balance,
    resume_mining,
    run_command,
    run_operator_cli,
    run_sql,
    send_transaction,
    wait_for_invoice,
    wait_for_receipt,
)

OPERATOR_KEY_PAYERS = [create_local_account() for _ in range(6)]
REFUND_KEY_ADDRESS = DEV_ADDRESSES[0]
TREASURY_SAFE_SETTING = f"PAY_TREASURY_SAFE_ADDRESS_{PRIMARY_CHAIN.environment_suffix}"
ALLOWANCE_MODULE_ADDRESS = "0xAA46724893dedD72658219405185Fb0Fc91e091C"
NATIVE_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"
# The maintenance step runs once a minute; a little over two of them.
MAINTENANCE_WAIT_SECONDS = 150
# chains.toml local_fork: refund POL low 5, target 15; daily 30.
REFUND_POL_TARGET_WEI = 15 * NATIVE_UNIT_WEI
# chains.toml local_fork: sweeper POL low 1, target 3.
SWEEPER_POL_TARGET_WEI = 3 * NATIVE_UNIT_WEI
DRAINED_BALANCE_WEI = NATIVE_UNIT_WEI // 2
EXEC_TRANSACTION_SELECTOR = keccak(
    text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
)[:4]
SET_ALLOWANCE_SELECTOR = keccak(text="setAllowance(address,address,uint96,uint16,uint32)")[:4]

TREASURY_STATE: dict[str, str] = {}


def wait_until[T](description: str, read: Callable[[], T | None], *, timeout_seconds: float) -> T:
    deadline = time.monotonic() + timeout_seconds
    while (value := read()) in (None, "", False):
        check(time.monotonic() < deadline, f"no {description} in {timeout_seconds} s")
        time.sleep(2)
    return value


def set_environment_setting(context: FlowContext, name: str, value: str) -> None:
    kept_lines = [line for line in context.environment_file.read_text().splitlines() if not line.startswith(f"{name}=")]
    context.environment_file.write_text("".join(f"{line}\n" for line in [*kept_lines, f"{name}={value}"]))


def recreate_watcher(context: FlowContext) -> None:
    run_command(
        compose_arguments(context.environment_file, "up", "-d", "--no-deps", "--force-recreate", "watcher"),
        environment=compose_environment(context.environment_file),
        log_path=context.scratch_folder / "commands.log",
    )


def read_key_state(context: FlowContext, address: str) -> tuple[str, str]:
    """(id, status) of the key's ``operator_key_states`` row; ("", "") without one."""
    row = run_sql(
        context,
        f"SELECT id, status FROM operator_key_states WHERE operator_address = '{address}'",  # noqa: S608
    )
    key_id, _, status = row.partition("|")
    return key_id, status


def count_operator_alerts(context: FlowContext, problem_key: str) -> int:
    statement = (
        "SELECT count(*) FROM account_events WHERE event_type = 'operator.alert' AND account_id IS NOT NULL "  # noqa: S608
        f"AND data->>'problem_key' = '{problem_key}'"
    )
    return int(run_sql(context, statement))


def read_sweep_operator_addresses(context: FlowContext, invoice_ids: list[str]) -> dict[str, str]:
    """Invoice id to the key its last sweep was signed by."""
    quoted = ", ".join(f"'{invoice_id}'" for invoice_id in invoice_ids)
    rows = run_sql(
        context,
        "SELECT DISTINCT ON (invoice_id) invoice_id, operator_address FROM outgoing_operations "  # noqa: S608
        f"WHERE kind = 'sweep' AND invoice_id IN ({quoted}) ORDER BY invoice_id, id DESC",
    )
    return dict(row.split("|") for row in rows.splitlines() if row)


def read_transaction_sender(context: FlowContext, transaction_hash: str) -> str:
    transaction = call_anvil(context.http_client, "eth_getTransactionByHash", transaction_hash)
    if not isinstance(transaction, dict):
        raise FlowFailedError(f"no transaction {transaction_hash}")
    return str(transaction["from"])


def pay_pol_invoices_in_one_block(context: FlowContext, *, count: int, note: str) -> list[str]:
    """``count`` 2 POL invoices to fresh destinations, paid in one block by different payers; their ids, swept."""
    invoices = [
        create_invoice(
            context, amount="2", currency=NATIVE_SYMBOL, assets=[NATIVE_SYMBOL], destination_address=Account.create().address,
            note=f"{note} {index}",
        )
        for index in range(count)
    ]  # fmt: skip
    pause_mining(context.http_client)
    try:
        transaction_hashes = [
            send_transaction(
                context.http_client,
                sender=payer.address,
                to=str(invoice["deposit_address"]),
                value_wei=2 * NATIVE_UNIT_WEI,
            )
            for invoice, payer in zip(invoices, OPERATOR_KEY_PAYERS, strict=False)
        ]
        call_anvil(context.http_client, "evm_mine")
    finally:
        resume_mining(context.http_client)
    for transaction_hash in transaction_hashes:
        wait_for_receipt(context.http_client, transaction_hash)
    invoice_ids = [str(invoice["id"]) for invoice in invoices]
    for invoice_id in invoice_ids:
        wait_for_invoice(context, invoice_id, description="final sweep", condition=last_sweep_final)
    return invoice_ids


def scenario_sweeps_spread_over_the_sweeper_keys(context: FlowContext) -> dict[str, object]:
    """Three sweeps recorded together go to three different sweepers, each sent from its own key."""
    invoice_ids = pay_pol_invoices_in_one_block(context, count=3, note="e2e sweeper pool")
    operator_by_invoice = read_sweep_operator_addresses(context, invoice_ids)
    sweeper_addresses = {account.address for account in SWEEPER_ACCOUNTS}
    check(
        set(operator_by_invoice.values()) <= sweeper_addresses,
        f"a sweep went to a key outside the sweeper pool: {operator_by_invoice}",
    )
    check(len(set(operator_by_invoice.values())) == 3, f"three sweeps, not three sweepers: {operator_by_invoice}")
    senders = {}
    for invoice_id in invoice_ids:
        transaction_hash = run_sql(
            context,
            "SELECT transaction_hash FROM outgoing_operations WHERE kind = 'sweep' "  # noqa: S608
            f"AND invoice_id = '{invoice_id}' ORDER BY id DESC LIMIT 1",
        )
        senders[invoice_id] = read_transaction_sender(context, transaction_hash)
        check(
            senders[invoice_id].lower() == operator_by_invoice[invoice_id].lower(),
            f"{invoice_id}: sent by {senders[invoice_id]}, recorded for {operator_by_invoice[invoice_id]}",
        )
    return {"operator_by_invoice": operator_by_invoice}


def ensure_treasury(context: FlowContext) -> str:
    """The run's treasury Safe: created through the watcher, configured, set up by its owner and funded (100 POL and
    50 USDC). Once per run; the address."""
    if "safe_address" in TREASURY_STATE:
        return TREASURY_STATE["safe_address"]
    owner_address = TREASURY_OWNER_ACCOUNT.address
    code, output = run_operator_cli(
        context, "treasury", "create-safe", "--chain", FORK_CHAIN_KEY, "--owner-address", owner_address
    )
    match = re.search(r"treasury Safe (0x[0-9a-fA-F]{40}): ", output)
    check(code == 0 and match is not None, f"create-safe: {output[-600:]}")
    safe_address = match.group(1) if match else ""
    wait_until(
        "treasury Safe code",
        lambda: call_anvil(context.http_client, "eth_getCode", safe_address, "latest") not in ("0x", "0x0"),
        timeout_seconds=120,
    )
    again_code, again_output = run_operator_cli(
        context, "treasury", "create-safe", "--chain", FORK_CHAIN_KEY, "--owner-address", owner_address
    )
    check(
        again_code == 0 and f"treasury Safe {safe_address}: already deployed" in again_output,
        f"create-safe again did not find the same Safe: {again_output[-600:]}",
    )
    set_environment_setting(context, TREASURY_SAFE_SETTING, safe_address)
    recreate_watcher(context)
    batch_code, batch_output = run_operator_cli(context, "treasury", "setup-transactions", "--chain", FORK_CHAIN_KEY)
    # The batch is the indented object starting at its "version" key; the CLI's JSON log lines surround it.
    batch_start = batch_output.find('{\n  "version"')
    batch = json.JSONDecoder().raw_decode(batch_output, batch_start)[0] if batch_start >= 0 else {}
    # enableModule, four addDelegate (the refund key, three sweepers), six setAllowance (the refund key's POL, USDC
    # and USDT; each sweeper's POL).
    check(
        batch_code == 0 and len(batch.get("transactions", [])) == 1 + 4 + 3 + 3 and batch["meta"].get("checksum"),
        f"setup-transactions: {batch_output[-800:]}",
    )
    execute_code, execute_output = run_operator_cli(
        context, "treasury", "execute-setup", "--chain", FORK_CHAIN_KEY,
        "--owner-key-environment-variable", TREASURY_OWNER_KEY_SETTING,
    )  # fmt: skip
    check(execute_code == 0 and "done" in execute_output, f"execute-setup: {execute_output[-600:]}")
    nothing_code, nothing_output = run_operator_cli(
        context, "treasury", "setup-transactions", "--chain", FORK_CHAIN_KEY
    )
    check(nothing_code == 0 and "nothing to set up" in nothing_output, f"setup again: {nothing_output[-600:]}")
    call_anvil(context.http_client, "anvil_setBalance", safe_address, hex(100 * NATIVE_UNIT_WEI))
    fund_with_usdc(context.http_client, safe_address, base_units=50 * USDC_BASE_UNITS)
    TREASURY_STATE["safe_address"] = safe_address
    context.write_log("treasury_ready", safe_address=safe_address, setup_calls=len(batch["transactions"]))
    return safe_address


def wait_for_final_top_up(context: FlowContext, *, recipient_address: str, asset_symbol: str, after_id: int) -> str:
    """The newest final top-up that filled ``recipient_address``, whichever key sent it."""
    return wait_until(
        f"final {asset_symbol} top-up",
        lambda: run_sql(
            context,
            "SELECT id FROM outgoing_operations WHERE kind = 'top_up' AND status = 'final' "  # noqa: S608
            f"AND recipient_address = '{recipient_address}' AND asset_symbol = '{asset_symbol}' AND id > {after_id} "
            "ORDER BY id DESC LIMIT 1",
        ),
        timeout_seconds=MAINTENANCE_WAIT_SECONDS,
    )


def read_last_operation_id(context: FlowContext) -> int:
    return int(run_sql(context, "SELECT coalesce(max(id), 0) FROM outgoing_operations"))


def scenario_treasury_tops_up_a_drained_refund_key(context: FlowContext) -> dict[str, object]:
    """The refund key drained below its low mark: one top-up from the treasury Safe brings it back to its target."""
    safe_address = ensure_treasury(context)
    refund_key_balance = read_native_balance(context.http_client, REFUND_KEY_ADDRESS)
    last_operation_id = read_last_operation_id(context)
    safe_balance_before = read_native_balance(context.http_client, safe_address)
    call_anvil(context.http_client, "anvil_setBalance", REFUND_KEY_ADDRESS, hex(DRAINED_BALANCE_WEI))
    try:
        top_up_id = wait_for_final_top_up(
            context, recipient_address=REFUND_KEY_ADDRESS, asset_symbol=NATIVE_SYMBOL, after_id=last_operation_id
        )
        topped_up_balance = read_native_balance(context.http_client, REFUND_KEY_ADDRESS)
        moved_wei = int(run_sql(context, f"SELECT amount_base_units FROM outgoing_operations WHERE id = {top_up_id}"))  # noqa: S608
        safe_balance_after = read_native_balance(context.http_client, safe_address)
        check(
            REFUND_POL_TARGET_WEI - NATIVE_UNIT_WEI // 10 <= topped_up_balance <= REFUND_POL_TARGET_WEI,
            f"refund key holds {topped_up_balance} wei after the top-up, target {REFUND_POL_TARGET_WEI}",
        )
        check(safe_balance_before - safe_balance_after == moved_wei, "the Safe lost other than the top-up")
        top_up_count = int(
            run_sql(
                context,
                f"SELECT count(*) FROM outgoing_operations WHERE kind = 'top_up' AND asset_symbol = '{NATIVE_SYMBOL}' "  # noqa: S608
                f"AND recipient_address = '{REFUND_KEY_ADDRESS}' AND id > {last_operation_id}",
            )
        )
        check(top_up_count == 1, f"{top_up_count} {NATIVE_SYMBOL} top-ups for one drain")
    finally:
        call_anvil(context.http_client, "anvil_setBalance", REFUND_KEY_ADDRESS, hex(refund_key_balance))
    return {"safe_address": safe_address, "top_up_operation": top_up_id, "moved_wei": moved_wei,
            "refund_key_usdc": read_usdc_balance(context.http_client, REFUND_KEY_ADDRESS)}  # fmt: skip


def scenario_treasury_tops_up_a_sweeper_with_no_gas(context: FlowContext) -> dict[str, object]:
    """A sweeper at 0 POL cannot pay its own top-up's gas (2026-10-14: every new sweeper starts so): another key sends
    it with the sweeper's delegate signature, and the Safe fills the sweeper to its target with no send by hand."""
    safe_address = ensure_treasury(context)
    sweeper = SWEEPER_ACCOUNTS[0]
    sweeper_balance = read_native_balance(context.http_client, sweeper.address)
    last_operation_id = read_last_operation_id(context)
    call_anvil(context.http_client, "anvil_setBalance", sweeper.address, "0x0")
    try:
        top_up_id = wait_for_final_top_up(
            context, recipient_address=sweeper.address, asset_symbol=NATIVE_SYMBOL, after_id=last_operation_id
        )
        sender_address, transaction_hash = run_sql(
            context,
            "SELECT o.operator_address || ' ' || a.transaction_hash FROM outgoing_operations o JOIN outgoing_attempts a "  # noqa: S608
            f"ON a.operation_id = o.id WHERE o.id = {top_up_id} AND a.receipt_succeeded ORDER BY a.id DESC LIMIT 1",
        ).split()
        check(sender_address != sweeper.address, f"the empty sweeper sent its own top-up {top_up_id}")
        check(
            read_transaction_sender(context, transaction_hash).lower() == sender_address.lower(),
            f"top-up {top_up_id} was not sent by its operation's key {sender_address}",
        )
        sweeper_balance_after = read_native_balance(context.http_client, sweeper.address)
        check(
            sweeper_balance_after == SWEEPER_POL_TARGET_WEI,
            f"sweeper holds {sweeper_balance_after} wei after the top-up, target {SWEEPER_POL_TARGET_WEI}",
        )
    finally:
        call_anvil(context.http_client, "anvil_setBalance", sweeper.address, hex(sweeper_balance))
    return {"safe_address": safe_address, "top_up_operation": top_up_id, "sent_by": sender_address}


def send_safe_call_as_owner(context: FlowContext, *, safe_address: str, to: str, data: bytes) -> None:
    signature = bytes(12) + bytes.fromhex(TREASURY_OWNER_ACCOUNT.address.removeprefix("0x")) + bytes(32) + b"\x01"
    call_data = EXEC_TRANSACTION_SELECTOR + encode(
        ["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "bytes"],
        [to, 0, data, 0, 0, 0, 0, NATIVE_TOKEN_ADDRESS, NATIVE_TOKEN_ADDRESS, signature],
    )
    wait_for_receipt(
        context.http_client,
        send_transaction(
            context.http_client, sender=TREASURY_OWNER_ACCOUNT.address, to=safe_address, data="0x" + call_data.hex()
        ),
    )


def scenario_treasury_allowance_exhausted_alerts_once(context: FlowContext) -> dict[str, object]:
    """The owner cuts the refund key's POL allowance below what it spent: drained, the key pulls nothing and one
    ``allowance_exhausted`` alert lands; ``execute-setup`` puts the allowance back."""
    safe_address = ensure_treasury(context)
    key_id, _ = read_key_state(context, REFUND_KEY_ADDRESS)
    problem_key = f"allowance_exhausted:{FORK_CHAIN_KEY}:{key_id}:{NATIVE_SYMBOL}"
    send_safe_call_as_owner(
        context,
        safe_address=safe_address,
        to=ALLOWANCE_MODULE_ADDRESS,
        data=SET_ALLOWANCE_SELECTOR
        + encode(
            ["address", "address", "uint96", "uint16", "uint32"], [REFUND_KEY_ADDRESS, NATIVE_TOKEN_ADDRESS, 1, 1440, 0]
        ),
    )
    refund_key_balance = read_native_balance(context.http_client, REFUND_KEY_ADDRESS)
    last_operation_id = read_last_operation_id(context)
    safe_balance_before = read_native_balance(context.http_client, safe_address)
    call_anvil(context.http_client, "anvil_setBalance", REFUND_KEY_ADDRESS, hex(DRAINED_BALANCE_WEI))
    try:
        wait_until("allowance_exhausted alert", lambda: count_operator_alerts(context, problem_key) or None,
                   timeout_seconds=MAINTENANCE_WAIT_SECONDS)  # fmt: skip
        time.sleep(75)  # another maintenance look: no second alert before the reminder, still no top-up
        alert_count = count_operator_alerts(context, problem_key)
        top_ups = run_sql(
            context,
            f"SELECT count(*) FROM outgoing_operations WHERE kind = 'top_up' AND asset_symbol = '{NATIVE_SYMBOL}' "  # noqa: S608
            f"AND recipient_address = '{REFUND_KEY_ADDRESS}' AND id > {last_operation_id}",
        )
        check(alert_count == 1, f"{alert_count} allowance_exhausted alerts, expected 1")
        check(top_ups == "0", f"{top_ups} {NATIVE_SYMBOL} top-ups past an exhausted allowance")
        check(
            read_native_balance(context.http_client, safe_address) == safe_balance_before,
            "the Safe paid out past the allowance",
        )
    finally:
        call_anvil(context.http_client, "anvil_setBalance", REFUND_KEY_ADDRESS, hex(refund_key_balance))
    execute_code, execute_output = run_operator_cli(
        context, "treasury", "execute-setup", "--chain", FORK_CHAIN_KEY,
        "--owner-key-environment-variable", TREASURY_OWNER_KEY_SETTING,
    )  # fmt: skip
    check(execute_code == 0 and "done" in execute_output, f"restore: {execute_output[-600:]}")
    return {"problem_key": problem_key, "alerts": alert_count}


def scenario_sweeper_used_elsewhere_is_frozen(context: FlowContext) -> dict[str, object]:
    """A sweeper key sends a transaction of its own: frozen with one alert, later sweeps go to other keys, and
    ``operator unfreeze-key`` makes it active again for good."""
    sweeper = SWEEPER_ACCOUNTS[2]
    wait_until(
        "the sweeper's nonce row",
        lambda: run_sql(
            context, f"SELECT next_nonce FROM operator_nonces WHERE operator_address = '{sweeper.address}'"
        ),  # noqa: S608
        timeout_seconds=MAINTENANCE_WAIT_SECONDS,
    )
    wait_for_receipt(
        context.http_client,
        send_transaction(context.http_client, sender=sweeper.address, to=Account.create().address, value_wei=1),
    )
    wait_until("frozen sweeper", lambda: read_key_state(context, sweeper.address)[1] == "frozen",
               timeout_seconds=MAINTENANCE_WAIT_SECONDS)  # fmt: skip
    key_id, _ = read_key_state(context, sweeper.address)
    problem_key = f"operator_key_used_elsewhere:{FORK_CHAIN_KEY}:{key_id}"
    wait_until("operator_key_used_elsewhere alert", lambda: count_operator_alerts(context, problem_key) or None,
               timeout_seconds=60)  # fmt: skip
    list_code, list_output = run_operator_cli(context, "operator", "list-keys", "--chain", FORK_CHAIN_KEY)
    check(list_code == 1 and "frozen" in list_output, f"list-keys: {list_output[-800:]}")
    invoice_ids = pay_pol_invoices_in_one_block(context, count=3, note="e2e frozen sweeper")
    operator_by_invoice = read_sweep_operator_addresses(context, invoice_ids)
    check(sweeper.address not in operator_by_invoice.values(), f"a frozen key swept: {operator_by_invoice}")
    unfreeze_code, unfreeze_output = run_operator_cli(
        context, "operator", "unfreeze-key", "--chain", FORK_CHAIN_KEY, "--address", sweeper.address,
        "--reason", "e2e: that transaction was the owner's",
    )  # fmt: skip
    check(unfreeze_code == 0 and "active again" in unfreeze_output, f"unfreeze-key: {unfreeze_output[-600:]}")
    time.sleep(75)  # a maintenance look past the unfreeze: the skipped nonce is not used elsewhere any more
    check(read_key_state(context, sweeper.address)[1] == "active", "the unfrozen key was frozen again")
    return {"frozen_key": key_id, "sweeps_meanwhile": operator_by_invoice,
            "alerts": count_operator_alerts(context, problem_key)}  # fmt: skip


def scenario_retired_sweeper_returns_to_the_treasury(context: FlowContext) -> dict[str, object]:
    """``operator retire-key``: no more sweeps for the key; its USDC and then its POL go back to the treasury Safe;
    ``setup-transactions`` then removes it as a delegate."""
    safe_address = ensure_treasury(context)
    retired: LocalAccount = SWEEPER_ACCOUNTS[1]
    fund_with_usdc(context.http_client, retired.address, base_units=5 * USDC_BASE_UNITS)
    baseline_operation_id = read_last_operation_id(context)
    safe_usdc_before = read_usdc_balance(context.http_client, safe_address)
    safe_pol_before = read_native_balance(context.http_client, safe_address)
    retire_code, retire_output = run_operator_cli(
        context, "operator", "retire-key", "--chain", FORK_CHAIN_KEY, "--address", retired.address
    )
    check(retire_code == 0 and "retired" in retire_output, f"retire-key: {retire_output[-600:]}")
    invoice_ids = pay_pol_invoices_in_one_block(context, count=2, note="e2e retired sweeper")
    operator_by_invoice = read_sweep_operator_addresses(context, invoice_ids)
    check(retired.address not in operator_by_invoice.values(), f"a retired key swept: {operator_by_invoice}")

    def read_final_return(asset_symbol: str) -> str:
        return run_sql(
            context,
            "SELECT amount_base_units FROM outgoing_operations WHERE kind = 'treasury_return' AND status = 'final' "  # noqa: S608
            f"AND operator_address = '{retired.address}' AND asset_symbol = '{asset_symbol}'",
        )

    usdc_returned = int(wait_until("USDC return", lambda: read_final_return("USDC"), timeout_seconds=240))
    pol_returned = int(
        wait_until(f"{NATIVE_SYMBOL} return", lambda: read_final_return(NATIVE_SYMBOL), timeout_seconds=240)
    )
    check(read_usdc_balance(context.http_client, retired.address) == 0, "the retired key kept USDC")
    check(usdc_returned == 5 * USDC_BASE_UNITS, f"{usdc_returned} USDC base units returned, expected 5 USDC")

    # The same Safe can refill another key during the return. Compare its net balance with both movements.
    def read_top_ups_from_treasury(asset_symbol: str) -> int:
        return int(
            run_sql(
                context,
                "SELECT coalesce(sum(amount_base_units), 0) FROM outgoing_operations "  # noqa: S608
                f"WHERE chain_key = '{FORK_CHAIN_KEY}' AND kind = 'top_up' AND status = 'final' "
                f"AND asset_symbol = '{asset_symbol}' AND id > {baseline_operation_id}",
            )
        )

    check(
        read_usdc_balance(context.http_client, safe_address) - safe_usdc_before + read_top_ups_from_treasury("USDC")
        == usdc_returned,
        "the Safe did not get the USDC",
    )
    check(
        read_native_balance(context.http_client, safe_address)
        - safe_pol_before
        + read_top_ups_from_treasury(NATIVE_SYMBOL)
        == pol_returned,
        f"the Safe did not get the {NATIVE_SYMBOL}",
    )
    check(
        read_native_balance(context.http_client, retired.address) < NATIVE_UNIT_WEI // 5,
        "the retired key kept more than its gas reserve",
    )
    setup_code, setup_output = run_operator_cli(context, "treasury", "setup-transactions", "--chain", FORK_CHAIN_KEY)
    check(
        setup_code == 0 and f"removeDelegate({retired.address}" in setup_output,
        f"setup-transactions after retiring: {setup_output[-600:]}",
    )
    return {"usdc_returned": usdc_returned, "pol_returned_wei": pol_returned, "sweeps_meanwhile": operator_by_invoice}


OPERATOR_KEY_SCENARIOS: list[Callable[[FlowContext], dict[str, object]]] = [
    scenario_sweeps_spread_over_the_sweeper_keys,
    scenario_treasury_tops_up_a_drained_refund_key,
    scenario_treasury_tops_up_a_sweeper_with_no_gas,
    scenario_treasury_allowance_exhausted_alerts_once,
    scenario_sweeper_used_elsewhere_is_frozen,
    scenario_retired_sweeper_returns_to_the_treasury,
]
