"""Nightly scheduler runner for headless campaign optimization.

Runs OptimizationAgent.run_headless() for mapped campaigns without chat
or LLM interaction, then stores results in recommendation storage.

One campaign failure does not stop the batch. Returns summary stats for
scheduler logging and alerting."""

from __future__ import annotations


import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone


from app.agents.adzump.agents.optimization.agent import get_optimization_agent
from app.agents.adzump.agents.optimization.platform_registry import (
    normalize_platform,
)
from app.agents.adzump.agents.optimization.platform_handlers import (
    get_platform_handler,
    PlatformHandler,
)
from app.agents.adzump.agents.optimization.resolver import (
    resolve_platform_and_account,
)
from app.agents.adzump.services.business_storage import fetch_campaign_mappings
from app.agents.adzump.services.recommendation_storage import (
    recommendation_storage_service,
)

from app.core.session import AuthContext

logger = logging.getLogger(__name__)

_MAX_CONCURRENT_CAMPAIGNS = 5
_GLOBAL_OPTIMIZATION_SEMAPHORE: asyncio.Semaphore | None = None

def _get_semaphore() -> asyncio.Semaphore:
    global _GLOBAL_OPTIMIZATION_SEMAPHORE
    if _GLOBAL_OPTIMIZATION_SEMAPHORE is None:
        _GLOBAL_OPTIMIZATION_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_CAMPAIGNS)
    return _GLOBAL_OPTIMIZATION_SEMAPHORE


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SchedulerRunSummary:
    """Summary metrics for a scheduler batch run."""
    client_code: str
    started_at: str
    finished_at: str = ""
    accounts_processed: int = 0
    campaigns_attempted: int = 0
    campaigns_succeeded: int = 0
    campaigns_failed: int = 0
    failed_campaign_ids: list[str] = field(default_factory=list)
    platforms_processed: list[str] = field(default_factory=list)
    skipped_platforms: dict[str, int] = field(default_factory=dict)


class ScheduledOptimizationRunner:
    """Batch runner for scheduled optimization jobs."""

    def __init__(self) -> None:
        self.agent = get_optimization_agent()
        self._summary_lock: asyncio.Lock | None = None

    def _get_summary_lock(self) -> asyncio.Lock:
        if self._summary_lock is None:
            self._summary_lock = asyncio.Lock()
        return self._summary_lock

    async def run_all(
        self,
        client_code: str,
        auth_headers: dict,
        auth: AuthContext = None,
    ) -> SchedulerRunSummary:
        """Run headless optimization for all mapped campaigns for a client."""
        started_at = _utcnow()
        summary = SchedulerRunSummary(
            client_code=client_code,
            started_at=started_at,
        )

        logger.info(
            "scheduled_runner.run_all: client=%s started=%s",
            client_code, started_at,
        )

        try:
            campaign_mapping = await fetch_campaign_mappings(client_code, auth_headers)
        except Exception:
            logger.exception(
                "scheduled_runner.run_all: setup failed client=%s", client_code
            )
            summary.finished_at = _utcnow()
            return summary

        if not campaign_mapping:
            logger.info(
                "scheduled_runner.run_all: no campaign mappings client=%s", client_code
            )
            summary.finished_at = _utcnow()
            return summary

        grouped, unknown = self._group_mappings_by_platform(campaign_mapping)
        platform_tasks = []
        for platform, platform_mapping in grouped.items():
            from app.agents.adzump.agents.optimization.platform_registry import get_provider
            provider = get_provider(platform)
            if not provider or not provider.capabilities.has_scheduler:
                count = len(platform_mapping)
                summary.skipped_platforms[platform] = count
                logger.info(
                    "scheduled_runner: platform=%s skipped campaigns=%d "
                    "reason=optimization_scheduler_unsupported_or_not_implemented",
                    platform,
                    count,
                )
                continue

            handler = get_platform_handler(platform)
            if handler:
                summary.platforms_processed.append(platform)
                platform_tasks.append(
                    self._process_platform(
                        client_code=client_code,
                        auth_headers=auth_headers,
                        campaign_mapping=platform_mapping,
                        platform=platform,
                        summary=summary,
                        handler=handler,
                        auth=auth,
                    )
                )
            else:
                count = len(platform_mapping)
                summary.skipped_platforms[platform] = count
                logger.info(
                    "scheduled_runner: platform=%s skipped campaigns=%d "
                    "reason=optimization_not_implemented",
                    platform,
                    count,
                )

        if platform_tasks:
            results = await asyncio.gather(*platform_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.error(
                        "scheduled_runner: platform processing task failed: %s",
                        res,
                        exc_info=res,
                    )

        if unknown:
            await self._process_unknown_platform_campaigns(
                client_code=client_code,
                auth_headers=auth_headers,
                campaign_mapping=unknown,
                summary=summary,
                auth=auth,
            )

        summary.finished_at = _utcnow()
        logger.info(
            "scheduled_runner.run_all done: client=%s accounts=%d "
            "campaigns=%d succeeded=%d failed=%d",
            client_code,
            summary.accounts_processed,
            summary.campaigns_attempted,
            summary.campaigns_succeeded,
            summary.campaigns_failed,
        )
        return summary

    def _group_mappings_by_platform(
        self, campaign_mapping: dict[str, dict]
    ) -> tuple[dict[str, dict[str, dict]], dict[str, dict]]:
        grouped: dict[str, dict[str, dict]] = {}
        unknown: dict[str, dict] = {}
        for campaign_id, mapping in campaign_mapping.items():
            platform_value = mapping.get("platform")
            if platform_value:
                platform = normalize_platform(platform_value)
                if platform:
                    grouped.setdefault(platform, {})[campaign_id] = mapping
                else:
                    unknown[campaign_id] = mapping
            else:
                unknown[campaign_id] = mapping
        return grouped, unknown

    async def _update_summary(
        self,
        summary: SchedulerRunSummary,
        *,
        accounts_processed: int = 0,
        campaigns_attempted: int = 0,
        campaigns_succeeded: int = 0,
        campaigns_failed: int = 0,
        failed_campaign_ids: list[str] | None = None,
        skipped_platforms: dict[str, int] | None = None,
    ) -> None:
        async with self._get_summary_lock():
            summary.accounts_processed += accounts_processed
            summary.campaigns_attempted += campaigns_attempted
            summary.campaigns_succeeded += campaigns_succeeded
            summary.campaigns_failed += campaigns_failed
            if failed_campaign_ids:
                summary.failed_campaign_ids.extend(failed_campaign_ids)
            if skipped_platforms:
                for platform, count in skipped_platforms.items():
                    summary.skipped_platforms[platform] = (
                        summary.skipped_platforms.get(platform, 0) + count
                    )

    async def _process_platform(
        self,
        client_code: str,
        auth_headers: dict,
        campaign_mapping: dict[str, dict],
        platform: str,
        summary: SchedulerRunSummary,
        handler: PlatformHandler,
        auth: AuthContext = None,
    ) -> None:
        if not campaign_mapping:
            return
        try:
            accounts = await handler.fetch_accessible_accounts(
                client_code, auth_headers=auth_headers
            )
        except Exception:
            logger.exception(
                "scheduled_runner.platform: account fetch failed client=%s", client_code
            )
            await self._update_summary(
                summary,
                campaigns_failed=len(campaign_mapping),
                failed_campaign_ids=list(campaign_mapping.keys()),
            )
            return

        if not accounts:
            logger.info(
                "scheduled_runner.platform: no accounts found client=%s", client_code
            )
            await self._update_summary(
                summary,
                campaigns_failed=len(campaign_mapping),
                failed_campaign_ids=list(campaign_mapping.keys()),
            )
            return

        remaining_campaign_ids = set(campaign_mapping.keys())
        matched_campaign_ids: set[str] = set()

        platform = normalize_platform(platform) or platform

        for account in accounts:
            account_id = account.get("customer_id")
            login_customer_id = account.get("login_customer_id")
            await self._update_summary(summary, accounts_processed=1)

            accessible_ids = await handler.find_campaign_ids_in_account(
                account_id=account_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
                campaign_ids=sorted(remaining_campaign_ids),
            )
            if not accessible_ids:
                continue

            account_mapping = {
                campaign_id: campaign_mapping[campaign_id]
                for campaign_id in accessible_ids
                if campaign_id in campaign_mapping
            }
            if not account_mapping:
                continue

            matched_campaign_ids.update(account_mapping.keys())
            remaining_campaign_ids.difference_update(account_mapping.keys())

            await self._process_account(
                account_id=account_id,
                login_customer_id=login_customer_id,
                client_code=client_code,
                auth_headers=auth_headers,
                campaign_mapping=account_mapping,
                platform=platform,
                summary=summary,
                handler=handler,
                auth=auth,
            )

            if not remaining_campaign_ids:
                break

        unmatched = sorted(set(campaign_mapping.keys()) - matched_campaign_ids)
        if unmatched:
            logger.warning(
                "scheduled_runner.platform: %d %s campaign(s) not found in any accessible account "
                "(skipping — stale mapping or restricted access): %s",
                len(unmatched),
                platform,
                unmatched[:20],
            )
            await self._update_summary(
                summary,
                skipped_platforms={f"{platform}_NOT_ACCESSIBLE": len(unmatched)},
            )

    async def _process_unknown_platform_campaigns(
        self,
        client_code: str,
        auth_headers: dict,
        campaign_mapping: dict[str, dict],
        summary: SchedulerRunSummary,
        auth: AuthContext = None,
    ) -> None:
        async def _run_one(campaign_id: str, mapping: dict) -> None:
            async with _get_semaphore():
                try:
                    resolved = await resolve_platform_and_account(
                        campaign_id, client_code, auth_headers, {}
                    )
                    platform = resolved.get("platform")
                    account_id = resolved.get("account_id")
                    login_customer_id = resolved.get("login_customer_id")

                    if not platform or not account_id or not login_customer_id:
                        logger.warning(
                            "scheduled_runner.unknown: platform/account resolution failed campaign=%s — skipping",
                            campaign_id,
                        )
                        await self._update_summary(
                            summary,
                            skipped_platforms={"UNRESOLVED": 1},
                        )
                        return

                    from app.agents.adzump.agents.optimization.platform_registry import get_provider
                    provider = get_provider(platform)
                    if not provider or not provider.capabilities.has_scheduler:
                        logger.info(
                            "scheduled_runner.unknown: platform=%s scheduler unsupported campaign=%s — skipping",
                            platform,
                            campaign_id,
                        )
                        await self._update_summary(
                            summary,
                            skipped_platforms={platform: 1},
                        )
                        return

                    await self._update_summary(summary, campaigns_attempted=1)

                    handler = get_platform_handler(platform)
                    if not handler:
                        logger.info(
                            "scheduled_runner.unknown: platform=%s not implemented campaign=%s",
                            platform,
                            campaign_id,
                        )
                        await self._update_summary(
                            summary,
                            campaigns_failed=1,
                            failed_campaign_ids=[campaign_id],
                        )
                        return

                    rec = await handler.run_campaign_headless(
                        campaign_id=campaign_id,
                        account_id=account_id,
                        login_customer_id=login_customer_id,
                        client_code=client_code,
                        auth_headers=auth_headers,
                        mapping=mapping,
                        auth=auth,
                    )

                    if rec is not None:
                        await recommendation_storage_service.store(
                            rec, client_code, auth_headers=auth_headers
                        )
                        await self._update_summary(summary, campaigns_succeeded=1)
                        logger.info(
                            "scheduled_runner.unknown: stored campaign=%s platform=%s",
                            campaign_id,
                            platform,
                        )
                    else:
                        await self._update_summary(
                            summary,
                            campaigns_failed=1,
                            failed_campaign_ids=[campaign_id],
                        )
                except Exception:
                    logger.exception(
                        "scheduled_runner.unknown: campaign=%s failed", campaign_id
                    )
                    await self._update_summary(
                        summary,
                        campaigns_failed=1,
                        failed_campaign_ids=[campaign_id],
                    )

        results = await asyncio.gather(
            *[
                _run_one(cid, mapping)
                for cid, mapping in campaign_mapping.items()
            ],
            return_exceptions=True,
        )
        for exc in results:
            if isinstance(exc, BaseException):
                logger.error(
                    "scheduled_runner.unknown: unhandled exception escaped _run_one: %s",
                    exc,
                    exc_info=exc,
                )

    async def _process_account(
        self,
        account_id: str,
        login_customer_id: str,
        client_code: str,
        auth_headers: dict,
        campaign_mapping: dict[str, dict],
        platform: str,
        summary: SchedulerRunSummary,
        handler: PlatformHandler,
        auth: AuthContext = None,
    ) -> None:
        """Run headless optimization for mapped campaigns in a single account."""

        async def _run_one(campaign_id: str, mapping: dict) -> None:
            async with _get_semaphore():
                await self._update_summary(summary, campaigns_attempted=1)
                try:
                    rec = await handler.run_campaign_headless(
                        campaign_id=campaign_id,
                        account_id=account_id,
                        login_customer_id=login_customer_id,
                        client_code=client_code,
                        auth_headers=auth_headers,
                        mapping=mapping,
                        auth=auth,
                    )

                    if rec is not None:
                        await recommendation_storage_service.store(
                            rec, client_code, auth_headers=auth_headers
                        )
                        await self._update_summary(summary, campaigns_succeeded=1)
                        logger.info(
                            "scheduled_runner: stored campaign=%s", campaign_id
                        )
                    else:
                        await self._update_summary(
                            summary,
                            campaigns_failed=1,
                            failed_campaign_ids=[campaign_id],
                        )

                except Exception:
                    logger.exception(
                        "scheduled_runner: campaign=%s failed", campaign_id
                    )
                    await self._update_summary(
                        summary,
                        campaigns_failed=1,
                        failed_campaign_ids=[campaign_id],
                    )

        results = await asyncio.gather(
            *[
                _run_one(cid, mapping)
                for cid, mapping in campaign_mapping.items()
            ],
            return_exceptions=True,
        )
        for exc in results:
            if isinstance(exc, BaseException):
                logger.error(
                    "scheduled_runner: unhandled exception escaped _run_one: %s",
                    exc,
                    exc_info=exc,
                )



_scheduled_optimization_runner: ScheduledOptimizationRunner | None = None


def get_scheduled_optimization_runner() -> ScheduledOptimizationRunner:
    global _scheduled_optimization_runner
    if _scheduled_optimization_runner is None:
        _scheduled_optimization_runner = ScheduledOptimizationRunner()
    return _scheduled_optimization_runner
