"""Screening refusals (the pages show the code's own copy)."""

from app.shared.errors import AppError


class SignUpRefusedError(AppError):
    code = "sign_up_refused"
    status = 403


class SignUpScreeningUnavailableError(AppError):
    code = "sign_up_screening_unavailable"
    status = 503
