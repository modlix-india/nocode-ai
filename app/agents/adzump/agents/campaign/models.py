"""What the sub-agents produced, keyed by channel.

Separate from ``campaign_spec`` (``tools/campaign_data.py``), which holds only what the user
stated - agent output there would break the traceability guard on ``set_campaign_spec``.
Each channel needs different content, so each gets a block and exactly one is populated.
Blocks hold the sub-agent's ``model_dump(mode="json")``, which is what the session stores.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel

SESSION_KEY = "campaign_build"
LEGACY_KEYWORD_KEY = "keyword_research"  # pre-envelope sessions; read-side only

# Whether to offer the channel choice. Off until the Demand Gen build path exists -
# offering a chip we cannot yet honour is worse than not offering it. Everything else here
# is channel-aware already, and ``resolve_channel`` defaults to Search, so a Google
# campaign behaves exactly as it did before. Flip this on with the audience agent.
DEMAND_GEN_ENABLED = False


class Channel(str, Enum):
    """Which Google campaign type to build. Meta has its own objective model and
    leaves ``campaign_spec["channel"]`` unset."""

    SEARCH = "SEARCH"
    DEMAND_GEN = "DEMAND_GEN"

    @classmethod
    def from_value(cls, value: str | None) -> Channel | None:
        """Map free text to the enum, or None. The LLM writes whatever the user said
        ("Demand Gen", "demand-gen"), so exact-match lookup would miss and silently
        fall back to Search. Word boundaries keep "search" out of longer words."""
        v = (value or "").strip().lower()
        if not v:
            return None
        for channel, keywords in _CHANNEL_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(k)}\b", v) for k in keywords):
                return channel
        return None


# Demand Gen first: its chip label may also carry the word "search".
_CHANNEL_KEYWORDS: dict[Channel, tuple[str, ...]] = {
    Channel.DEMAND_GEN: ("demand gen", "demand-gen", "demandgen", "demand_gen"),
    Channel.SEARCH: ("search",),
}


class ChannelBuild(BaseModel):
    """One channel's slots. ``required`` are the ones a campaign cannot launch without -
    extend as each lands, since naming a slot before it is built marks every campaign
    incomplete."""

    required: ClassVar[tuple[str, ...]] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(s for s in self.required if getattr(self, s) is None)


class SearchBuild(ChannelBuild):
    required: ClassVar[tuple[str, ...]] = ("keyword_research",)

    keyword_research: dict[str, Any] | None = None


class DemandGenBuild(ChannelBuild):
    required: ClassVar[tuple[str, ...]] = ("audience",)

    audience: dict[str, Any] | None = None


class CampaignBuild(BaseModel):
    channel: Channel
    search: SearchBuild | None = None
    demand_gen: DemandGenBuild | None = None

    def model_post_init(self, _context: Any) -> None:
        if self.channel is Channel.SEARCH and self.search is None:
            self.search = SearchBuild()
        elif self.channel is Channel.DEMAND_GEN and self.demand_gen is None:
            self.demand_gen = DemandGenBuild()

    @property
    def block(self) -> ChannelBuild:
        return self.search if self.channel is Channel.SEARCH else self.demand_gen  # type: ignore[return-value]

    @property
    def is_complete(self) -> bool:
        return not self.block.missing


def _load(session_ctx: dict) -> CampaignBuild | None:
    stored = session_ctx.get(SESSION_KEY)
    if stored:
        return CampaignBuild.model_validate(stored)
    legacy = session_ctx.get(LEGACY_KEYWORD_KEY)
    if legacy:
        return CampaignBuild(
            channel=Channel.SEARCH, search=SearchBuild(keyword_research=legacy)
        )
    return None


def _put(session_ctx: dict, channel: Channel, slot: str, value: Any) -> None:
    """The only write path. A build for the wrong channel is replaced rather than
    merged - its slots describe a campaign type this one is not."""
    build = _load(session_ctx)
    if build is None or build.channel is not channel:
        build = CampaignBuild(channel=channel)
    setattr(build.block, slot, value)
    session_ctx[SESSION_KEY] = build.model_dump(mode="json")
    # Migration is one-way. Leaving the pre-envelope key behind would keep a second copy
    # that no longer receives writes, and the two would drift apart from here.
    session_ctx.pop(LEGACY_KEYWORD_KEY, None)


# Payload accessors. Callers deal in what they produced, not in where it is kept or which
# channel owns it - so keyword code never imports Channel, and keyword research cannot be
# stored on a Demand Gen build.


def keyword_research(session_ctx: dict) -> dict | None:
    """The built keyword set, or None. A copy - mutating it needs ``set_keyword_research``."""
    build = _load(session_ctx)
    return build.search.keyword_research if build and build.search else None


def set_keyword_research(session_ctx: dict, dump: dict) -> None:
    _put(session_ctx, Channel.SEARCH, "keyword_research", dump)


def is_build_complete(session_ctx: dict) -> bool:
    """Has this channel's build produced every slot it cannot launch without?"""
    build = _load(session_ctx)
    return build is not None and build.is_complete


def resolve_channel(campaign_spec: dict) -> Channel:
    """Defaults to Search: Search campaigns skip the channel step, as do old sessions."""
    return Channel.from_value(campaign_spec.get("channel")) or Channel.SEARCH
