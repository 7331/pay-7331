"""``GET /api/v1/chains``: chains, assets, decimals and explorer URLs, so pages derive nothing by hand."""

from fastapi import APIRouter

from app.payments.chains.dependencies import ChainsServiceDependency
from app.payments.chains.schemas import ChainListResponse
from app.shared.path_params import API_PREFIX

chains_router = APIRouter(prefix=API_PREFIX, tags=["chains"])


@chains_router.get("/chains")
async def list_chains(*, chains_service: ChainsServiceDependency) -> ChainListResponse:
    """Every chain a new invoice can go to, with its coins and the ones a new invoice starts with ticked."""
    return chains_service.present_chain_list_response()
