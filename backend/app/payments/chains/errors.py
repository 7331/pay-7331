"""Requests naming a chain or asset this deployment does not run."""

from app.shared.errors import AppError


class ChainNotEnabledError(AppError):
    code = "chain_not_enabled"
    status = 400


class AssetNotSupportedError(AppError):
    code = "asset_not_supported"
    status = 400


class ChainKindNotSupportedError(AppError):
    code = "chain_kind_not_supported"
    status = 400
