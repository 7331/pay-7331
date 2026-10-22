"""Deliberately bad code: every rule in ../house-rules.yaml must fire on this file.

A semgrep rule that finds nothing is indistinguishable from a rule that is broken (an invalid config prints nothing
and exits 0 under --quiet). This file is the proof that each rule actually matches. Not importable, never run, and
outside ruff, ty, vulture and deptry (backend/pyproject.toml).
"""

import asyncio
from dataclasses import dataclass
from typing import Literal, NamedTuple, Protocol, runtime_checkable

import aiohttp
import requests
from pydantic import BaseModel

from app.lifespan.resources import ApiResources
from app.payments.invoices.models import Invoice


@dataclass
class BadValueObject:
    x: int


class BadTuple(NamedTuple):
    value: int


@runtime_checkable
class BadRuntimeProtocol(Protocol):
    value: int


class BadResourceService:
    def __init__(self, resources: ApiResources) -> None:
        self.resources = resources


async def bad_concurrency(a, b):
    await asyncio.gather(a(), b())
    await asyncio.wait_for(a(), timeout=1)


class BadClass:
    @staticmethod
    def bad_helper() -> None: ...

    def _bad_private(self) -> None: ...


def bad_error() -> None:
    from fastapi import HTTPException

    raise HTTPException(status_code=404)


def bad_exception_translation(exc: Exception) -> None:
    raise ValueError(str(exc))


def bad_app_state(request) -> None:
    return request.app.state.redis


def bad_model_copy_update(model) -> None:
    model.model_copy(update={"name": "renamed"})


def bad_include_exclude_on_wire_output(model) -> None:
    model.model_dump(include={"name"})
    model.model_dump_json(exclude={"secret"})


def bad_isinstance_json_parsing(payload) -> None:
    isinstance(payload, dict)
    isinstance(payload, (str, bytes))


def bad_type_dispatch_on_own_classes(row, value) -> None:
    isinstance(row, Invoice)
    isinstance(value, BadClass)


type BadClosedSet = Literal["paid", "expired", "refunded"]


def bad_hand_made_decorator(function):
    import functools

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return function(*args, **kwargs)

    return wrapper


def bad_from_attributes(row) -> object:
    from pydantic import BaseModel, ConfigDict

    class BadFromAttributesModel(BaseModel):
        model_config = ConfigDict(frozen=True, from_attributes=True)

    return BadFromAttributesModel.model_validate(row, from_attributes=True)


class BadInvoiceView(BaseModel):
    invoice_id: str
