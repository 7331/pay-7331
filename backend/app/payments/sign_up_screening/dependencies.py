"""The request-scoped SignUpScreeningService."""

from typing import Annotated

from fastapi import Depends

from app.lifespan.dependencies import RedisDependency, ResourcesDependency
from app.payments.sign_up_screening.service import SignUpScreeningService


def get_sign_up_screening_service(*, redis: RedisDependency, resources: ResourcesDependency) -> SignUpScreeningService:
    return SignUpScreeningService.on_redis(
        redis,
        ip_api_client=resources.invoice_wiring.ip_api_client,
        settings=resources.invoice_wiring.screening_settings,
    )


SignUpScreeningServiceDependency = Annotated[SignUpScreeningService, Depends(get_sign_up_screening_service)]
