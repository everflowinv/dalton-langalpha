"""Brokerage connectors: the ones this build ships, off until switched on.

The list is static and user-independent; what the user owns is a catalog row,
created by the brokerage's sign-in (``create_brokerage_row``, called from its
callback) and from then on toggled through the same apply every other switch on
the page uses. These are the rows that can place orders, so a weaker disable
than the server beside them is the last thing they should have.

Endpoints:
- GET   /api/v1/mcp/brokerages
- PATCH /api/v1/mcp/brokerages/{name}/enabled
- GET   /api/v1/mcp/brokerages/{name}/icon
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response

from src.server.models.mcp_server import (
    BrokerageList,
    CatalogServer,
    EnabledInput,
)
from src.server.services.brand_icons import icon_response
from src.server.services.brokerages import brokerage_by_name
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mcp", tags=["MCP Catalog"])

# DALTON-HOOK(hide-trading): disable brokerage connectors and return empty list
_BROKERAGE_DISABLED_DETAIL = (
    "Brokerage connections are disabled in Dalton (Dalton 已禁用券商连接)"
)


@router.get("/brokerages")
@handle_api_exceptions("list brokerage connectors", logger)
async def list_brokerages(user_id: CurrentUserId) -> BrokerageList:
    """Return an empty brokerage list in Dalton so legacy callers do not crash."""
    # DALTON-HOOK(hide-trading): always return empty list
    return BrokerageList(brokerages=[])


@router.patch("/brokerages/{name}/enabled")
@handle_api_exceptions("toggle brokerage connector", logger)
async def set_brokerage_enabled(
    name: str, body: EnabledInput, user_id: CurrentUserId
) -> CatalogServer:
    """Refuse enabling or toggling brokerage connectors in Dalton."""
    # DALTON-HOOK(hide-trading): reject brokerage toggle with 403
    if brokerage_by_name(name) is None:
        raise HTTPException(status_code=404, detail="Unknown brokerage")
    raise HTTPException(status_code=403, detail=_BROKERAGE_DISABLED_DETAIL)


@router.get("/brokerages/{name}/icon")
async def get_brokerage_icon(name: str) -> Response:
    """The broker's own logo, proxied from where the broker publishes it.

    Unauthenticated because it has nothing to authenticate: the only input is
    a name this build ships, so the answer is the same public logo for every
    caller and no user's configuration is read to produce it. Serving it under
    the user's bearer token was never an option anyway, since an ``<img>`` cannot
    send one, and routing brand art through a fetch-to-blob just to carry a
    credential that guards nothing is machinery for its own sake.

    404 is a normal answer, not an error: a vendor may simply have no usable
    mark, and the row draws its monogram instead. It carries a cache header so
    a page full of rows does not re-ask on every render.
    """
    brokerage = brokerage_by_name(name)
    return await icon_response(*(brokerage.mark_sources if brokerage else ()))
