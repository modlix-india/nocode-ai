"""What the sub-agents produced, keyed by channel.

Separate from ``campaign_spec`` (``tools/campaign_data.py``), which holds only what the user
stated - agent output there would break the traceability guard on ``set_campaign_spec``.
Each channel needs different content, so each gets a block and exactly one is populated.
Blocks hold the sub-agent's ``model_dump(mode="json")``, which is what the session stores.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import Enum
from typing import Any, ClassVar, NamedTuple

from pydantic import BaseModel

from app.agents.adzump.agents.campaign.google.keyword.models import AdGroupStatus
from app.agents.adzump.agents.campaign.google.keyword.themes import resolve_theme_ids

SESSION_KEY = "campaign_build"
LEGACY_KEYWORD_KEY = "keyword_research"  # pre-envelope sessions; read-side only


class AdvertisingChannelType(str, Enum):
    """Google's own enum, transcribed from
    https://github.com/googleapis/googleapis/blob/master/google/ads/googleads/v23/enums/advertising_channel_type.proto
    """

    SEARCH = "SEARCH"
    DISPLAY = "DISPLAY"
    SHOPPING = "SHOPPING"
    HOTEL = "HOTEL"
    VIDEO = "VIDEO"
    MULTI_CHANNEL = "MULTI_CHANNEL"
    LOCAL = "LOCAL"
    SMART = "SMART"
    PERFORMANCE_MAX = "PERFORMANCE_MAX"
    LOCAL_SERVICES = "LOCAL_SERVICES"
    TRAVEL = "TRAVEL"
    DEMAND_GEN = "DEMAND_GEN"


class Channel(str, Enum):
    """Which Google campaign type to build. Meta has its own objective model and
    leaves ``campaign_spec["channel"]`` unset."""

    SEARCH = "SEARCH"
    DEMAND_GEN = "DEMAND_GEN"

    @property
    def google_channel_type(self) -> AdvertisingChannelType:
        """What Google calls this channel. Ours is the set we build; Google's is the set that
        exists, so the two are mapped rather than assumed to stay identical."""
        return _CHANNELS[self].google_type

    @property
    def chip_label(self) -> str:
        """The choice the user is offered. Being in this enum IS the statement that a channel
        can be built, so a new one appears in the ask by existing."""
        return _CHANNELS[self].chip

    @classmethod
    def from_value(cls, value: str | None) -> Channel | None:
        """Map free text to the enum, or None. The LLM writes whatever the user said
        ("Demand Gen", "demand-gen"), so exact-match lookup would miss and silently
        fall back to Search. Word boundaries keep "search" out of longer words.

        The LONGEST match wins, so "demand gen, not search" resolves to Demand Gen. Reading
        it off the table's order instead would make a reorder silently change the answer.
        """
        v = (value or "").strip().lower()
        if not v:
            return None
        hits = [
            (len(word), channel)
            for channel, spec in _CHANNELS.items()
            for word in spec.words
            if re.search(rf"\b{re.escape(word)}\b", v)
        ]
        return max(hits, key=lambda h: h[0])[1] if hits else None


class _ChannelSpec(NamedTuple):
    """Everything one channel is. A single table, because three side-tables keyed by Channel
    meant a new channel had to be added to each - and missing one fails quietly."""

    google_type: AdvertisingChannelType
    words: tuple[str, ...]  # how the user might say it
    chip: str  # how it is offered


_CHANNELS: dict[Channel, _ChannelSpec] = {
    Channel.SEARCH: _ChannelSpec(
        AdvertisingChannelType.SEARCH,
        ("search",),
        "Search - capture people already looking",
    ),
    Channel.DEMAND_GEN: _ChannelSpec(
        AdvertisingChannelType.DEMAND_GEN,
        ("demand gen", "demand-gen", "demandgen", "demand_gen"),
        "Demand Gen - reach people on YouTube, Discover and Gmail",
    ),
}


class ChannelBuild(BaseModel):
    """One channel's slots. ``required`` are the ones a campaign cannot launch without -
    extend as each lands, since naming a slot before it is built marks every campaign
    incomplete."""

    required: ClassVar[tuple[str, ...]] = ()
    # Slot -> what the user sees for it, in panel order. Declared beside the slot so the
    # orchestrator never branches on channel to describe a panel it does not own.
    shows: ClassVar[dict[str, str]] = {}

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(s for s in self.required if getattr(self, s) is None)

    @property
    def review_items(self) -> tuple[str, ...]:
        """What is actually on screen to review - only the slots that ran, so a tool that
        has not landed yet is never promised to the user."""
        return tuple(t for s, t in self.shows.items() if getattr(self, s) is not None)

    def gaps(self, spec: dict) -> tuple[str, ...]:
        """Work a filled slot still owes, each naming the tool that finishes it. Written by
        the channel: what counts as unfinished, and what repairs it, differ per channel."""
        return ()


class SearchBuild(ChannelBuild):
    required: ClassVar[tuple[str, ...]] = ("keyword_research",)
    shows: ClassVar[dict[str, str]] = {"keyword_research": "the keyword suggestions"}

    keyword_research: dict[str, Any] | None = None

    def gaps(self, spec: dict) -> tuple[str, ...]:
        """One ad group runs the keyword theme of the same id. The slot fills as soon as ONE
        of them lands, so without this a campaign missing half of them reads as launchable."""
        if self.keyword_research is None:
            return ()
        sets = self.keyword_research.get("themes") or {}
        # The same question research asked, so an absent choice resolves to the same default
        # set it ran - reading the sets instead would never notice one that failed.
        wanted = resolve_theme_ids(spec)
        done = AdGroupStatus.COMPLETE.value
        unfinished = []
        for ad_group in wanted:
            kset = sets.get(ad_group)
            name = (kset or {}).get("label") or ad_group.replace("_", " ").title()
            if kset is None:
                unfinished.append(f"{name} has no keywords")
            # A set written before `status` existed defaults to complete, as the model does.
            elif kset.get("status", done) != done:
                unfinished.append(f"{name} has no negatives")
        if not unfinished:
            return ()
        # One item, naming no ad group of its own: both repairs go through the same tool, and
        # a prescription that picked one would override the ad group the user actually named.
        return (
            f"unfinished ad groups ({'; '.join(unfinished)}) - call `manage_keywords` and "
            "pass the user's message VERBATIM as user_message. The keyword agent reads it "
            "and researches or finishes whichever ad group they meant. Do NOT rebuild the "
            "campaign, and do NOT substitute your own wording for theirs.",
        )


class DemandGenBuild(ChannelBuild):
    # channel_controls is declared but not required: the emitter falls back to the defaults
    # for the ad type, so a campaign built before the tool ran still posts correctly.
    required: ClassVar[tuple[str, ...]] = ("audience",)
    shows: ClassVar[dict[str, str]] = {
        "audience": "the audience targeting",
        "channel_controls": "where the ads will show",
        "creative": "the creative",
    }

    audience: dict[str, Any] | None = None
    channel_controls: dict[str, Any] | None = None
    # Declared ahead of its tool so ad_type_for has one place to read from - see AGENT.md.
    creative: dict[str, Any] | None = None


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
    """The only path that writes a slot. A build for the wrong channel is replaced rather
    than merged - its slots describe a campaign type this one is not."""
    build = _load(session_ctx)
    if build is None or build.channel is not channel:
        build = CampaignBuild(channel=channel)
    setattr(build.block, slot, value)
    session_ctx[SESSION_KEY] = build.model_dump(mode="json")
    # Migration is one-way. Leaving the pre-envelope key behind would keep a second copy
    # that no longer receives writes, and the two would drift apart from here.
    session_ctx.pop(LEGACY_KEYWORD_KEY, None)


def _accessors(
    channel: Channel, slot: str
) -> tuple[Callable[[dict], dict | None], Callable[[dict, dict], None]]:
    """The read/write pair for one slot, generated so the two can never disagree about which
    channel owns it. Callers deal in what they produced, not where it is kept - so keyword
    code never imports Channel. The reader returns a copy; changing it needs the writer."""

    def get(session_ctx: dict) -> dict | None:
        build = _load(session_ctx)
        if build is None or build.channel is not channel:
            return None
        return getattr(build.block, slot)

    def put(session_ctx: dict, value: dict) -> None:
        _put(session_ctx, channel, slot, value)

    return get, put


# One line per slot. Add the field to the channel's block above, then a line here; add the
# name to that block's `required` only once its tool exists.
keyword_research, set_keyword_research = _accessors(Channel.SEARCH, "keyword_research")
audience, set_audience = _accessors(Channel.DEMAND_GEN, "audience")
channel_controls, set_channel_controls = _accessors(
    Channel.DEMAND_GEN, "channel_controls"
)
creative, set_creative = _accessors(Channel.DEMAND_GEN, "creative")


def build_dump(session_ctx: dict) -> dict | None:
    """The whole build, for handing a throwaway sub-session's output to the session that keeps
    it. Channel-neutral by necessity - the campaign sub-agent does not know which channel's
    tool ran, and one channel's slot returns None for every other."""
    build = _load(session_ctx)
    return build.model_dump(mode="json") if build else None


def set_build(session_ctx: dict, dump: dict) -> None:
    """Install a build assembled in another session - not a second way to write a slot.
    Revalidated because this is where the dump crosses a session boundary."""
    session_ctx[SESSION_KEY] = CampaignBuild.model_validate(dump).model_dump(
        mode="json"
    )
    session_ctx.pop(LEGACY_KEYWORD_KEY, None)


def _current(session_ctx: dict) -> CampaignBuild | None:
    """The stored build, unless the user has picked a DIFFERENT channel since.

    Only an explicit, recognised choice disowns a build. resolve_channel's Search default
    would disown a Demand Gen one whenever the channel is missing or unparsed - and an
    orphaned build reads as unbuilt, which invites a rebuild over the user's review.
    """
    build = _load(session_ctx)
    if build is None:
        return None
    chosen = chosen_channel(session_ctx)
    return build if chosen is None or build.channel is chosen else None


def is_build_complete(session_ctx: dict) -> bool:
    """Has this channel's build produced every slot it cannot launch without, and finished
    the work inside them?"""
    return not build_missing(session_ctx) and not build_gaps(session_ctx)


def build_missing(session_ctx: dict) -> tuple[str, ...]:
    """Slots that never arrived, worded as the user reads them. Separate from the gaps below:
    a slot that arrived holding unfinished work did not fail to build."""
    build = _current(session_ctx)
    if build is None:
        return ("the campaign build",)
    block = build.block
    return tuple(block.shows.get(slot, slot) for slot in block.missing)


def build_gaps(session_ctx: dict) -> tuple[str, ...]:
    """Unfinished work inside slots that did arrive, each naming the tool that finishes it."""
    build = _current(session_ctx)
    if build is None:
        return ()
    return build.block.gaps(session_ctx.get("campaign_spec") or {})


def build_review_items(session_ctx: dict) -> tuple[str, ...]:
    """What the review panel is showing, for the prompt that asks the user to check it."""
    build = _current(session_ctx)
    return build.block.review_items if build else ()


def resolve_channel(campaign_spec: dict) -> Channel:
    """Defaults to Search: Search campaigns skip the channel step, as do old sessions."""
    return Channel.from_value(campaign_spec.get("channel")) or Channel.SEARCH


def chosen_channel(session_ctx: dict) -> Channel | None:
    """The channel the user picked, or None. No Search default - changing platform clears
    the channel, so a stale build must not read as Search."""
    return Channel.from_value((session_ctx.get("campaign_spec") or {}).get("channel"))
