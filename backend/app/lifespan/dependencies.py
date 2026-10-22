"""The request dependencies that hand a request its process's ``ApiResources``.

``resources_of`` is the one reader of the lifespan state. ``DatabaseSession`` is one transaction per request, the unit of
work: committed when the route returns, rolled back when it raises. ``scope="function"`` commits before the response
is sent, so a client never reads its own write before it is committed.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Scope

from app.lifespan.resources import ApiResources
from app.shared.request import read_client_ip_address


def resources_of(connection_scope: Scope) -> ApiResources:
    """The API process's resources, from the lifespan state the server copies into each connection's scope."""
    return connection_scope["state"]["resources"]


def get_resources(request: Request) -> ApiResources:
    return resources_of(request.scope)


ResourcesDependency = Annotated[ApiResources, Depends(get_resources)]


def get_client_ip_address(request: Request, *, resources: ResourcesDependency) -> str:
    return read_client_ip_address(request, trusted_proxy_addresses=resources.trusted_proxy_addresses)


ClientIpAddressDependency = Annotated[str, Depends(get_client_ip_address)]


async def get_database_session(resources: ResourcesDependency) -> AsyncIterator[AsyncSession]:
    async with resources.database.session_factory() as session, session.begin():
        yield session


def get_redis(resources: ResourcesDependency) -> Redis:
    return resources.redis_client.client


DatabaseSession = Annotated[AsyncSession, Depends(get_database_session, scope="function")]
RedisDependency = Annotated[Redis, Depends(get_redis)]
