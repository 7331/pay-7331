"""Account webhook refusals."""

from app.shared.errors import ConflictError, NotFoundError


class AccountWebhookNotSetError(ConflictError):
    """A secret rotation or test event while the account has no webhook."""

    code = "webhook_not_set"


class AccountWebhookDisabledError(ConflictError):
    """A test event or resend while the receiver's 410 Gone keeps the webhook disabled; setting the URL again turns it
    back on."""

    code = "webhook_disabled"


class AccountWebhookDeliveryNotFoundError(NotFoundError):
    code = "webhook_delivery_not_found"


class AccountDiscordWebhookNotSetError(ConflictError):
    """A test message while the account has no Discord webhook, or one Discord deleted."""

    code = "discord_webhook_not_set"


class AccountDiscordWebhookInUseError(ConflictError):
    """Setting a Discord webhook another account already has: Discord limits per webhook (2026-10-13)."""

    code = "discord_webhook_in_use"


class AccountReceivesNoOperatorAlertsError(ConflictError):
    """Setting the alert reminder cadence on an account that is not the operator's: no other gets alerts."""

    code = "not_operator_account"
