"""The process's ChainsService."""

from typing import Annotated

from fastapi import Depends

from app.lifespan.dependencies import ResourcesDependency
from app.payments.chains.service import ChainsService


def get_chains_service(resources: ResourcesDependency) -> ChainsService:
    return resources.chains_service


ChainsServiceDependency = Annotated[ChainsService, Depends(get_chains_service)]
