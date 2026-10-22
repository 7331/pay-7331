"""Typed log records of the static_deposit_addresses domain. Ids, chains and counts; a creation also names the deposit
and its destination (the recovery path, as ``invoice_create``), never the salt, the seed or the customer reference."""

import logging

from app.shared.logging import LogEvent


class StaticDepositAddressCreated(LogEvent):
    message = "Static deposit address created"
    operation = "static_deposit_address_create"
    static_deposit_address_id: str
    account_id: int
    chain_key: str
    deposit_address: str
    destination_address: str


class StaticDepositPaymentRecorded(LogEvent):
    message = "Money on a static deposit address recorded"
    operation = "static_deposit_scan"
    static_deposit_address_id: str
    asset_symbol: str
    credited: bool
    uncredited_reason: str | None
    attributed: bool


class StaticDepositCreditDeferred(LogEvent):
    message = "Static deposit address left for the next pass: no price for a coin it received"
    operation = "static_deposit_scan"
    level = logging.WARNING
    static_deposit_address_id: str
    asset_symbol: str


class StaticDepositGrossWentDown(LogEvent):
    message = "A static deposit address holds less than its payments explain; nothing recorded"
    operation = "static_deposit_scan"
    level = logging.WARNING
    static_deposit_address_id: str
    asset_symbol: str


class StaticDepositSweepOutcomeApplied(LogEvent):
    message = "Static deposit sweep outcome applied"
    operation = "static_deposit_sweep"
    static_deposit_address_id: str
    operation_id: int
    status: str


class StaticDepositSweepRequestedByHand(LogEvent):
    message = "Static deposit address sweep requested by the operator"
    operation = "static_deposit_sweep"
    static_deposit_address_id: str
    recorded: bool
