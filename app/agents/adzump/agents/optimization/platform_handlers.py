"""Platform handlers for scheduler orchestration.

Provides a small abstraction used by the scheduled runner to discover
accounts, resolve which mapped campaign IDs live under an account and
execute a headless campaign run for a specific platform.

This is intentionally small and focused as a design artifact:
- `PlatformHandler` defines the operations the scheduler needs.
- A tiny registry allows platform handlers to be looked up by normalized
  platform key (e.g. "GOOGLE", "META").
- `GooglePlatformHandler` is a concrete implementation that adapts the
  existing Google adapters and the `OptimizationAgent.run_headless()`
  method.
"""

from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from typing import Callable
# Native types used throughout (dict, list, set, str | None)

from app.core.session import AuthContext
from app.agents.adzump.recommendations.models import (
    CampaignOverview,
)
from app.agents.adzump.agents.optimization.platform_registry import (
    normalize_platform,
)

logger = logging.getLogger(__name__)


class PlatformHandler(ABC):
    """Abstract adapter the scheduler can call for platform-specific work."""

    @abstractmethod
    async def fetch_accessible_accounts(
        self, client_code: str, auth_headers: dict
    ) -> list[dict]:
        """Return a list of account dicts for this client that the scheduler can inspect."""

    @abstractmethod
    async def find_campaign_ids_in_account(
        self,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        campaign_ids: list[str],
    ) -> set[str]:
        """Return the subset of campaign_ids that exist under the provided account."""

    @abstractmethod
    async def fetch_campaign_overview(
        self,
        campaign_id: str,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
    ) -> CampaignOverview | None:
        """Fetch the top-level campaign overview for the given campaign."""

    @abstractmethod
    async def run_campaign_headless(
        self,
        campaign_id: str,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        mapping: dict,
        auth: AuthContext = None,
    ):
        """Run a headless campaign analysis and return the recommendation model or None.

        Should return the same type as `OptimizationAgent.run_headless()` (CampaignRecommendation | None).
        """


_REGISTRY: dict[str, Callable[[], PlatformHandler]] = {}
_INSTANCES: dict[str, PlatformHandler] = {}


def register_platform_handler(key: str, factory: Callable[[], PlatformHandler]) -> None:
    normalized = normalize_platform(key)
    if normalized is None:
        return
    _REGISTRY[normalized] = factory


def get_platform_handler(key: str) -> PlatformHandler | None:
    normalized = normalize_platform(key)
    if not normalized:
        return None
    if normalized not in _INSTANCES:
        factory = _REGISTRY.get(normalized)
        if factory:
            _INSTANCES[normalized] = factory()
    return _INSTANCES.get(normalized)


def reset_handlers() -> None:
    """Clear cached platform handler instances (useful for test isolation)."""
    _INSTANCES.clear()


async def _resolve_campaign_account_for_handler(
    platform: str,
    handler: PlatformHandler,
    campaign_id: str,
    client_code: str,
    auth_headers: dict,
) -> dict[str, str] | None:
    """Resolve which account a campaign belongs to for a given platform handler.

    Iterates accessible accounts, checks campaign membership, and returns
    platform + account metadata if found. Returns None if the campaign
    doesn't exist under any account on this platform.
    """
    accounts = await handler.fetch_accessible_accounts(client_code, auth_headers)
    logger.info(
        "platform_handlers: live_scan platform=%s campaign=%s accounts_to_check=%d",
        platform,
        campaign_id,
        len(accounts),
    )
    for account in accounts:
        account_id = account.get("customer_id")
        login_customer_id = account.get("login_customer_id")
        if not account_id:
            continue

        accessible_ids = await handler.find_campaign_ids_in_account(
            account_id=account_id,
            login_customer_id=login_customer_id,
            client_code=client_code,
            auth_headers=auth_headers,
            campaign_ids=[campaign_id],
        )
        if campaign_id in accessible_ids:
            logger.info(
                "platform_handlers: live_scan FOUND campaign=%s "
                "platform=%s account=%s login=%s",
                campaign_id,
                normalize_platform(platform),
                account_id,
                login_customer_id,
            )
            return {
                "platform": normalize_platform(platform),
                "account_id": account_id,
                "login_customer_id": login_customer_id or "",
            }
    logger.info(
        "platform_handlers: live_scan NOT FOUND campaign=%s platform=%s",
        campaign_id,
        platform,
    )
    return None


async def resolve_campaign_account_for_platform(
    platform: str | None,
    campaign_id: str,
    client_code: str,
    auth_headers: dict,
) -> dict[str, str] | None:
    """Resolve which account owns the campaign for the given platform.

    If the platform is explicit, resolve only in that platform. If the platform
    is unknown, fall back to a cross-platform search through all registered
    platform handlers.
    """
    normalized = normalize_platform(platform) if platform else None
    logger.info(
        "platform_handlers: resolve_campaign_account campaign=%s platform=%s",
        campaign_id,
        normalized or "(unknown — cross-platform search)",
    )
    if normalized:
        handler = get_platform_handler(normalized)
        if handler:
            account_details = await _resolve_campaign_account_for_handler(
                normalized,
                handler,
                campaign_id,
                client_code,
                auth_headers,
            )
            if account_details:
                return account_details

    for handler_platform in _REGISTRY:
        if normalized and normalize_platform(handler_platform) == normalized:
            continue
        handler = get_platform_handler(handler_platform)
        if handler:
            logger.info(
                "platform_handlers: cross-platform fallback trying platform=%s "
                "campaign=%s",
                handler_platform,
                campaign_id,
            )
            account_details = await _resolve_campaign_account_for_handler(
                handler_platform,
                handler,
                campaign_id,
                client_code,
                auth_headers,
            )
            if account_details:
                return account_details

    logger.warning(
        "platform_handlers: resolve_campaign_account FAILED — "
        "campaign=%s not found in any platform",
        campaign_id,
    )
    return None


def _create_google_handler() -> PlatformHandler:
    from app.agents.adzump.agents.optimization.platforms.google.handler import GooglePlatformHandler
    return GooglePlatformHandler()


def _create_meta_handler() -> PlatformHandler:
    from app.agents.adzump.agents.optimization.platforms.meta.handler import MetaPlatformHandler
    return MetaPlatformHandler()


# Register platform handlers so the scheduler can look them up.
register_platform_handler("GOOGLE", _create_google_handler)
register_platform_handler("META", _create_meta_handler)
