"""AppBuilderAgent — builds entire applications through conversation.

Extends BaseAgent with:
- AppBuilder-specific tools (pages, components, events, styles, entities)
- Component catalog integration (when available)
- API catalog integration (when available)
- Progressive tool documentation (group summary + per-turn details)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.agent import BaseAgent
from app.core.session import BaseSession
from app.core.context import BaseContext
from app.core.tools.draft_registry import (
    DraftEntry,
    DraftRegistry,
    drafting,
    is_draftable,
    open_drafts,
)
from app.agents.appbuilder.tools.modlix._draft_surface import draft_mode
from app.agents.appbuilder.tools._shared import (
    FOCUS_APP_KEY,
    SEEN_APPS_KEY,
    app_scope_hint,
)
from app.agents.appbuilder.context import (
    effective_hot_tools,
    deferred_tool_names,
    extract_last_user_text,
    get_relevant_tool_details,
)
from app.config import settings

logger = logging.getLogger(__name__)

# Pulls the app out of a "... not found in app 'X'." tool error, so the
# cross-app hint can say which OTHER apps were candidates. See
# `AppBuilderAgent.annotate_tool_error`.
_APP_IN_ERROR_RE = re.compile(r"not found in app '([^']*)'")


# ── Same-document write collisions ──────────────────────────────────────────
#
# Which tools READ a document, mutate it in memory and save it back. Two of
# these in one parallel batch, aimed at the same document, both fetch the same
# version and the later save discards the earlier edit — `_load_save`
# (pages.py) has no version check, and `save_page` PUTs the whole document.
#
# This was unreachable while the stream assembler collapsed every batch to one
# call. Now that batches genuinely dispatch through `asyncio.gather`, it is
# reachable, so `BaseAgent._batch_write_collision` serialises those batches.
# Creates are deliberately absent: two creates of one name is a loud backend
# conflict, not a silent lost edit.
#
# family → the document kind, so `update_page(name="home")` and
# `update_theme(name="home")` do not look like the same document.
# The tuple is the identity parameters in priority order.
_RMW_TOOLS: dict[str, tuple[str, tuple[str, ...]]] = {}


def _register_rmw(family: str, identity: tuple[str, ...], *names: str) -> None:
    for n in names:
        _RMW_TOOLS[n] = (family, identity)


# Page composition — the big one; every one of these goes through `_load_save`.
_register_rmw(
    "page", ("page_name",),
    "add_component", "add_components", "patch_component_props",
    "patch_component_styles", "bulk_patch_component_props",
    "bulk_patch_component_styles", "remove_component", "move_component",
    "rename_component", "set_styles", "set_bindings",
    "patch_component_bindings", "update_component_props",
    "remove_component_styles", "delete_style_rule", "set_app_page_reference",
)
# Same page document, addressed by `name` instead.
_register_rmw(
    "page", ("name", "page_name"),
    "update_page", "replace_page_definition", "reset_page_composition",
)
# Page EVENT functions live inside the page document, so they collide with
# page composition edits too — hence the same "page" family.
_register_rmw(
    "page", ("page_name",),
    "create_page_event_function", "save_page_event_function_from_text",
    "delete_page_event_function", "add_event_step", "update_event_step",
    "remove_event_step", "set_event_step_dependencies",
)
# Kirun functions.
_register_rmw(
    "function", ("function_name", "name"),
    "add_step", "update_step", "remove_step", "set_dependencies",
)
_register_rmw(
    "function", ("name", "function_name"),
    "update_function", "update_server_function", "save_function_from_text",
)
# One-document-per-name objects.
_register_rmw("theme", ("name",), "update_theme", "patch_theme_variables")
_register_rmw("style", ("name",), "update_style")
_register_rmw("storage", ("name",), "update_storage")
_register_rmw("schema", ("name",), "update_schema")
_register_rmw("template", ("name",), "update_template", "update_template_part")
_register_rmw("notification", ("name",), "update_notification",
              "set_notification_channel_part")
_register_rmw("connection", ("name",), "update_connection")
_register_rmw("event_definition", ("name",), "update_event_definition")
_register_rmw("event_action", ("name",), "update_event_action")
_register_rmw("uri_path", ("name",), "update_uri_path")
# The app document itself. `update_app`/`set_app_property` rewrite properties,
# and `configure_app_for_customer_signup` writes through the same document.
_register_rmw("app", ("name", "app_code"), "update_app", "set_app_property",
              "configure_app_for_customer_signup")


# Tools whose success moves the session's focus app (see `note_tool_outcome`).
# Every read-modify-write tool qualifies, plus the creates and deletes that
# `_RMW_TOOLS` deliberately omits: for collision detection a create is not a
# lost-update hazard, but for "which app is this session actually building" a
# create is the single strongest signal there is.
#
# Deliberately excluded, despite taking an `app_code`: the `build_*` helpers
# (`build_authority` and the two asset-URL builders) compute a string and write
# nothing, and `generate_image` produces a file rather than app definition work.
# None of them is evidence about where the next edit belongs.
_FOCUS_MOVING_TOOLS: frozenset[str] = frozenset(_RMW_TOOLS) | frozenset({
    "create", "update", "delete",
    "create_app", "create_page", "create_pages", "delete_page",
    "build_page_from_url", "discard_page_draft", "publish_app",
    "create_connection", "delete_connection",
    "create_event_action", "delete_event_action",
    "create_event_definition", "delete_event_definition",
    "create_function", "delete_function",
    "create_server_function", "delete_server_function",
    "create_notification", "delete_notification",
    "create_schema", "delete_schema",
    "create_storage", "delete_storage",
    "create_style", "delete_style",
    "create_template", "delete_template",
    "create_theme", "delete_theme",
    "create_uri_path", "delete_uri_path",
    "create_role", "add_app_reg_entry", "upload_static_asset",
})


class AppBuilderAgent(BaseAgent):
    """Agent that builds no-code applications via tool-use."""

    # Mutating CRUD tools pause for user confirmation before executing. The
    # confirmation mechanism lives in BaseAgent; this set + the message body
    # below are AppBuilder-specific (page/component/app fields).
    CONFIRMATION_TOOLS: set[str] = {"create", "update", "delete", "copy"}

    def __init__(
        self,
        context_builder: BaseContext,
        tools: list | None = None,
        catalog: Any = None,
        api_catalog: Any = None,
        provider: str = "anthropic",
    ) -> None:
        """
        Args:
            context_builder: BaseContext with agent persona and tool groups summary.
            tools: List of ToolDefinitions. Defaults to empty (populated by registry).
            catalog: ComponentCatalog instance (optional).
            api_catalog: ApiCatalog instance (optional).
            provider: LLM provider name ("anthropic" or "openai"). Defaults to Anthropic.
        """
        self._catalog = catalog
        self._catalog_context = catalog.to_prompt_context() if catalog else ""
        self._api_catalog = api_catalog
        self._api_catalog_context = api_catalog.to_prompt_context() if api_catalog else ""

        # Both catalogs are rendered ONCE here and never recomputed for the
        # life of the process, so they belong in the cached prefix, not in the
        # per-request tail that build_dynamic_context produces. They used to be
        # appended there, which re-sent ~10.6K tokens uncached on every turn of
        # every conversation (and, on providers that flatten the system blocks
        # into one string, pushed them behind the per-session app/client line so
        # they fell outside the shared prefix cache entirely).
        static_extra = "\n\n---\n\n".join(
            p for p in (self._catalog_context, self._api_catalog_context) if p
        )
        if static_extra:
            context_builder.set_static_suffix(static_extra)

        # Deferred-schema surface (Phase 3): the LLM sees each tool's name +
        # one-liner description with empty parameters in the API `tools=` field,
        # and pulls full schemas on demand via `get_tool_schema`. The system
        # prompt's tool catalog (see TOOL_GROUPS_SUMMARY in
        # app.agents.appbuilder.context) lists every advertised tool by group.
        # The legacy tool-of-tools router (TOOL_ROUTER) is retired from this
        # agent — kept exported by the registry for other potential callers but
        # not wired here.
        super().__init__(
            name="appbuilder",
            tools=tools or [],
            context_builder=context_builder,
            model_tier=settings.AGENT_MODEL_TIER,
            max_turns=settings.MAX_AGENT_TURNS,
            max_tokens=settings.AGENT_MAX_TOKENS,
            provider=provider,
            defer_schemas=True,
        )

        # Resolved once, after super().__init__ so the registry is settled.
        # Intersected with the tools actually registered, so a name in a
        # deferred family that this deployment filtered out (e.g. describe_image
        # on a vision model) never lands in the withheld set.
        registered = {t.name for t in (tools or [])}
        self._deferred_tool_names = frozenset(deferred_tool_names() & registered)
        logger.info(
            "AppBuilder tool surface: %d advertised up front, %d deferred until first use",
            len(registered) - len(self._deferred_tool_names), len(self._deferred_tool_names),
        )

    def _tool_to_advertised_schema(self, tool: Any) -> dict[str, Any]:
        """Override BaseAgent's deferred-schema renderer for hot tools.

        Tools in `effective_hot_tools()` ship their FULL Anthropic-shape schema in the
        tools[] payload (not the stripped {"type":"object","properties":{}} the
        deferred pattern uses for the long tail). Reason: the synthetic-retry
        round-trip on first-time calls was costing 1 extra LLM turn per unique
        tool used in a conversation. For multi-write tasks touching 5-7 unique
        tools, that's 5-7 wasted turns per conv.

        Trade-off: ~3-5K extra tokens in the system-prompt prefix per session.
        DeepSeek's automatic prefix caching makes that a one-time cost.

        For tools outside that set, defer to BaseAgent's stripped form — the
        long-tail tools still go through search_tools / get_tool_schema.
        """
        if tool.name in effective_hot_tools():
            return tool.to_anthropic_tool()
        return super()._tool_to_advertised_schema(tool)

    @staticmethod
    def _effective_app_code(session: BaseSession) -> str:
        """The app this session is working in, as the tools see it.

        Mirrors `tools._shared.resolve_app_code` for a call that passes no
        explicit `app_code`: focus app first, then the app the request opened
        with. Everything the agent *tells the model* about the current app has
        to agree with where the tools will actually write, or the prompt and the
        dispatcher disagree — which is precisely the failure this fixes.
        """
        focus = (session.context.get(FOCUS_APP_KEY) or "").strip()
        if focus:
            return focus
        request_app = session.context.get("app_code") or ""
        return request_app or (session.auth.app_code if session.auth else "")

    def note_tool_outcome(
        self,
        tool_name: str,
        tool_input: Any,
        result: Any,
        session: BaseSession,
    ) -> None:
        """Move the session's focus app when a write lands in a named app.

        A session opened from appbuilder's own page carries
        ``app_code="appbuilder"`` from the chat request, and until this hook
        existed nothing ever changed it. So an agent asked to build a CRM would
        create the `crm` app, build its pages with an explicit ``app_code``, and
        then drop that optional argument on the next patch — which resolved back
        to `appbuilder`, where none of those pages exist. In one production
        session a single message fired 13 parallel `patch_component_props` calls
        that all died on `Page 'leads' not found in app 'appbuilder'`.

        Once a write to app X succeeds, X is where the work is, so X becomes the
        default for calls that omit ``app_code``. Only writes count: reading
        another app's page as a reference must not hijack where the next edit
        goes. Failed calls do not count either — a 404 against the wrong app is
        the symptom, not evidence about intent.
        """
        if tool_name not in _FOCUS_MOVING_TOOLS:
            return
        if not getattr(result, "success", False):
            return
        if not isinstance(tool_input, dict):
            return
        app = tool_input.get("app_code")
        app = app.strip() if isinstance(app, str) else ""
        if not app:
            return

        written = session.context.setdefault(SEEN_APPS_KEY, [])
        if isinstance(written, list) and app not in written:
            written.append(app)

        if session.context.get(FOCUS_APP_KEY) == app:
            return
        session.context[FOCUS_APP_KEY] = app
        # The grounding block names an app and lists its pages, so it is stale
        # once it describes an app we are no longer in. Drop it here so the next
        # turn refetches, but only on a genuine change: the first write to the
        # app the request already opened with sets a focus without moving one.
        # An unset marker means the cache predates this key (a session restored
        # from the DB), and there is no way to tell what it describes — drop it.
        if session.context.get("_preflight_grounding_app") != app:
            session.context.pop("_preflight_grounding", None)
        logger.info(
            "session %s: focus app -> '%s' (after %s)",
            session.session_id or "?", app, tool_name,
        )

    def annotate_tool_error(
        self,
        tool_name: str,
        tool_input: Any,
        result: Any,
        session: BaseSession,
    ) -> str | None:
        """Name the other candidate apps when an object is missing from this one.

        Fires only on a "not found in app 'X'" miss where this session has
        written to some app other than X. That combination is almost always a
        dropped `app_code` rather than a genuinely absent object, and the tool
        raising the error has no way to know it — only the session does.
        """
        error = getattr(result, "error", "") or ""
        if "not found in app" not in error:
            return None
        searched = _APP_IN_ERROR_RE.search(error)
        return app_scope_hint(
            {SEEN_APPS_KEY: session.context.get(SEEN_APPS_KEY) or []},
            searched.group(1) if searched else "",
        ) or None

    def write_conflict_key(self, tool_name: str, tool_input: Any) -> str | None:
        """See `BaseAgent.write_conflict_key`. None for anything not in `_RMW_TOOLS`.

        An identity that cannot be resolved from the arguments yields a
        family-wide wildcard rather than None, so two same-family mutations with
        unreadable targets serialise instead of racing. `app_code` is part of the
        key because the same page name in two apps is two documents; a missing
        one resolves to the session app identically for every call in a batch, so
        the empty placeholder compares correctly.
        """
        entry = _RMW_TOOLS.get(tool_name)
        if entry is None:
            return None
        family, identity_params = entry
        if not isinstance(tool_input, dict):
            return f"{family}:*:*"
        target = ""
        for name in identity_params:
            value = tool_input.get(name)
            if isinstance(value, str) and value.strip():
                target = value.strip()
                break
        app = tool_input.get("app_code")
        app = app.strip() if isinstance(app, str) and app.strip() else ""
        return f"{family}:{target or '*'}:{app}"

    def withheld_tool_names(self, session: BaseSession) -> set[str]:
        """Keep the deferred families out of `tools=` until the session wants them.

        Advertising all 232 tools costs ~26K tokens on every turn of every
        conversation, and whole families (messaging, the security admin tail,
        image ops) go untouched in most of them.

        A withheld tool stays discoverable — it is listed in the system prompt's
        tool index and `search_tools` searches the full registry — so the LLM
        finds it, calls `get_tool_schema`, and from that point it is in
        `fetched_schemas` and advertised normally. One turn, once per session,
        only for sessions that actually need the family.
        """
        deferred = self._deferred_tool_names
        if not deferred:
            return set()
        fetched = session.context.get("fetched_schemas") or ()
        return {name for name in deferred if name not in fetched}

    async def run(
        self,
        user_message: str,
        session: BaseSession,
        event_stream: Any,
        image_blocks: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> None:
        """Run one turn with the caller's open drafts in scope.

        A chat embedded in an editor tells us which objects the user has open and
        unsaved. For exactly those, tools read the user's copy and their writes are
        held there instead of saved, so the change can be looked at before it is
        committed. A caller that declares nothing (the plain chat page) holds
        nothing and gets precisely the behaviour it always had; the registry is
        still built, because it is also how a write that really happened gets
        reported back.
        """
        draft_token = draft_mode.set(bool(session.context.get("draft_mode")))
        declared = getattr(session, "open_drafts", None)
        # Built even when the caller declares nothing, because it carries the
        # event stream as well as the held objects. A turn with nothing declared
        # still writes, and the surface that asked for the turn is still showing
        # what was written; with no registry there was nobody to tell.
        registry = DraftRegistry(session_id=session.session_id)
        registry.stream = event_stream
        if declared:
            for d in declared:
                # A caller may name the kind or the API it saves to. Resolving the
                # second from the first here keeps that mapping in exactly one
                # place, the same table the intercept matches against, so the two
                # cannot disagree about what a path means.
                kind = d.get("kind") or ""
                if not kind and d.get("api"):
                    kind = DraftRegistry.resolve(d["api"])[0] or ""
                if not kind or not d.get("id"):
                    logger.warning("ignoring an undeclarable open draft: %s", d.get("api") or d)
                    continue
                registry.declare(DraftEntry(
                    kind=kind,
                    id=str(d["id"]),
                    name=d.get("name", ""),
                    app_code=d.get("app_code", ""),
                    doc=d.get("doc") or {},
                    overlay=d.get("overlay"),
                ))
        token = open_drafts.set(registry)
        drafting_token = drafting.set(await self._drafting_now(session))
        try:
            await super().run(
                user_message, session, event_stream, image_blocks, model_override,
            )
        finally:
            drafting.reset(drafting_token)
            open_drafts.reset(token)
            draft_mode.reset(draft_token)

    async def _drafting_now(self, session: BaseSession) -> bool:
        """Settle once, before any tool runs, where this turn's writes go.

        Every write in the turn reads this, so it cannot be decided per call:
        two tools disagreeing would put half a change in the draft and half of
        it live. Deciding it here also means the probe happens once instead of
        on every save.

        Never assumed. `?draft=true` is an ordinary query parameter, and a
        deployment that predates the draft surface neither rejects nor honours
        it: Spring drops what it does not know and performs an ordinary live
        update. So the answer is no unless the deployment is asked and answers.
        """
        if not session.context.get("draft_mode"):
            return False

        app_code = session.context.get("app_code") or (
            session.auth.app_code if session.auth else ""
        )
        if not app_code:
            return False

        from app.agents.appbuilder.tools.modlix import _draft_surface as ds
        from app.core.tools.http_client import SaasClient
        from app.config import settings

        try:
            return await ds.supported(
                SaasClient(settings.GATEWAY_URL),
                self._draft_probe_headers(session),
                app_code,
            )
        except Exception:  # noqa: BLE001 - a probe must never take the turn down
            logger.warning("draft support probe failed, writes stay live", exc_info=True)
            return False

    def build_tool_context(self, session: BaseSession) -> dict[str, Any]:
        """Extend BaseAgent's context with appbuilder-specific fields.

        ALSO pre-marks every hot tool in `fetched_schemas` so the
        dispatch gate at `_gate_deferred_dispatch` passes on first call —
        matching the full-schema advertisement above. The schema is already
        in the LLM's tools[] payload, so a synthetic retry would be pure
        overhead.
        """
        ctx = super().build_tool_context(session)
        hot = effective_hot_tools()
        fetched = ctx.get("fetched_schemas")
        if isinstance(fetched, list):
            for name in hot:
                if name not in fetched:
                    fetched.append(name)
        elif isinstance(fetched, set):
            fetched.update(hot)
        if session.auth:
            ctx["app_code"] = session.context.get("app_code") or session.auth.app_code
            ctx["client_code"] = session.auth.client_code
        # Where writes have actually been landing, and every app they touched.
        # `resolve_app_code` prefers the focus over `app_code` above; the seen
        # list only feeds the "did you mean another app?" hint on a miss.
        ctx[FOCUS_APP_KEY] = session.context.get(FOCUS_APP_KEY, "")
        ctx[SEEN_APPS_KEY] = session.context.get(SEEN_APPS_KEY, [])
        ctx["session_context"] = session.context
        return ctx

    async def build_dynamic_context(self, session: BaseSession) -> str:
        """Build per-request dynamic context.

        Includes: auth info, pre-flight app grounding, relevant tool group
        details, and learned knowledge. The component and API catalogs are
        deliberately absent — they are process-static and live in the cached
        static suffix instead.
        """
        parts: list[str] = []

        if session.auth:
            app_code = self._effective_app_code(session)
            parts.append(
                f"Current session:\n"
                f"- Client: {session.auth.client_code}\n"
                f"- App: {app_code}\n"
            )

        editor = self._build_editor_context(session)
        if editor:
            parts.append(editor)

        drafts_note = self._build_open_drafts_context(session)
        if drafts_note:
            parts.append(drafts_note)

        draft_note = await self._build_draft_surface_context(session)
        if draft_note:
            parts.append(draft_note)

        # Pre-flight grounding: fetch app definition + top pages once per
        # session so the agent walks in knowing the structure. Saves 3-10
        # "list_pages" / "get_app" round-trips on most conversations.
        grounding = await self._build_preflight_grounding(session)
        if grounding:
            parts.append(grounding)

        # Big picture: what this app already knows about itself. Pushed rather
        # than left to a tool call, because the failure it prevents is the agent
        # confidently redoing something this app decided against months ago,
        # and an agent that does not know to ask will not ask.
        from app.services.lore import context as lore_context
        lore_brief = await lore_context.big_picture(session)
        if lore_brief:
            parts.append(lore_brief)

        # Progressive tool docs: inject detailed reference for relevant groups
        tool_details = get_relevant_tool_details(session.messages)
        if tool_details:
            parts.append(tool_details)

        # The component + API catalogs are NOT appended here — they are static
        # for the process lifetime and go into the context builder's cached
        # static suffix (see __init__).

        # Learning loop: inject relevant knowledge from past sessions
        enhancement = await self._build_learning_enhancement(session)
        if enhancement:
            parts.append(enhancement)

        return "\n\n".join(parts)

    # Editor context fields the sidekick sends, in the order they read best.
    # Anything else in the payload is ignored: the caller is a page definition,
    # and an unrecognised key must not become prompt text by accident.
    _EDITOR_CONTEXT_FIELDS: tuple[tuple[str, str], ...] = (
        # Surface goes first: it says what kind of thing the names below are, and
        # without it "Invites" reads as a page name and sends the agent hunting
        # with the page tools.
        ("surface", "Screen"),
        ("active_object", "Looking at"),
        # Every open tab, the active one included, so "also" would be wrong.
        ("open_tabs", "Open tabs"),
        ("open_tab_ids", "Ids of the open objects"),
    )

    # Each value is page-supplied, so a page bug (a whole tab record instead of a
    # name, say) must cost a truncated line rather than a blown-up prompt.
    _EDITOR_CONTEXT_MAX_CHARS = 400

    # active_data is a serialised payload rather than a label, so it gets its own,
    # larger allowance. The client already caps it; this is the backstop for a
    # caller that does not.
    _ACTIVE_DATA_MAX_CHARS = 8000

    def _build_editor_context(self, session: BaseSession) -> str:
        """Render what the caller's editor has open, when the caller is one.

        Chats embedded in the appbuilder workspace/org shell send this so the
        agent can answer about the object in front of the user without spending
        a discovery round-trip on `list_pages` / `get_app` first.
        """
        ctx = session.context.get("editor_context")
        if not isinstance(ctx, dict):
            return ""

        lines: list[str] = []
        for field, label in self._EDITOR_CONTEXT_FIELDS:
            value = ctx.get(field)
            if not value or not isinstance(value, str):
                continue
            value = value.strip()
            if not value:
                continue
            if len(value) > self._EDITOR_CONTEXT_MAX_CHARS:
                value = value[: self._EDITOR_CONTEXT_MAX_CHARS] + "..."
            lines.append(f"- {label}: {value}")

        active_data = ctx.get("active_data")
        if isinstance(active_data, str) and active_data.strip():
            body = active_data.strip()[: self._ACTIVE_DATA_MAX_CHARS]
            lines.append(
                "- What that screen is currently showing. This is ONE PAGE of "
                "results under the filters in force, not the whole set: trust a "
                "total/totalElements count over the number of rows you can see, "
                "and never tell the user a list is complete on the strength of "
                f"this alone.\n{body}"
            )

        if not lines:
            return ""

        return (
            "What the user has open in front of them right now. Treat what they "
            "are looking at as the subject of anything they say without naming a "
            "target.\n\nThe screen contents below are already on the user's "
            "screen, so answer questions about them directly rather than "
            "re-fetching. Reach for a tool only for what is NOT here: anything "
            "beyond the rows shown, or any change they ask you to make. Names on "
            "this screen are not necessarily app objects, so do not feed them to "
            "the page or storage tools without checking what they are first.\n"
            + "\n".join(lines)
        )

    async def _build_draft_surface_context(self, session: BaseSession) -> str:
        """Tell the agent its edits are going somewhere the user has to approve.

        Without this the agent reports work as done, and the user looks at the
        live app and sees nothing. It also has to know the ordinary page URL
        renders LIVE, or it screenshots its own change, sees the old page, and
        starts debugging a problem that does not exist. That happened.
        """
        if not session.context.get("draft_mode"):
            return ""

        app_code = session.context.get("app_code") or (
            session.auth.app_code if session.auth else ""
        )
        if not app_code:
            return ""

        from app.agents.appbuilder.tools.modlix import _draft_surface as ds
        from app.core.tools.http_client import SaasClient
        from app.config import settings

        client = SaasClient(settings.GATEWAY_URL)
        headers = self._draft_probe_headers(session)
        if not await ds.supported(client, headers, app_code):
            # Say nothing. Promising a review step this deployment cannot provide
            # is worse than saying nothing, because the user would then look for
            # a draft that does not exist and trust that live is untouched.
            return ""

        return (
            "Your definition edits in this app go to its DRAFT surface, not live. "
            "They are real and saved, but only visible on the draft surface until "
            "someone publishes them.\n"
            "- Tell the user their changes are ready for review, and give them the "
            "draft link from `get_draft_link`.\n"
            "- To LOOK at your own change, screenshot the draft host from "
            "`get_draft_link`. The ordinary page URL renders the live app and will "
            "not show your work, so a screenshot of it proves nothing.\n"
            "- Never publish because you finished. `publish_app` needs the user to "
            "ask for it.\n"
            "- Creating an object is never drafted; a new page or storage exists "
            "immediately. Only edits to existing definitions are held back."
        )

    @staticmethod
    def _draft_probe_headers(session: BaseSession) -> dict[str, str]:
        """Auth headers for a probe made outside a tool call.

        Delegates to AuthContext rather than assembling a subset. An earlier
        version built its own dict and left out X-Forwarded-Host, so the gateway
        resolved the request against localhost, found no app, and answered 403 --
        which the probe then read as "this deployment has no draft surface" and
        quietly kept writing live.
        """
        return session.auth.to_headers() if session.auth else {}

    @staticmethod
    def _build_open_drafts_context(session: BaseSession) -> str:
        """Tell the agent which of its writes will be saved and which will not.

        This asymmetry is invisible from inside a tool call: every write returns
        success either way. The agent is the only thing that can explain it to the
        user, so it has to know. Getting this wrong in the confident direction is
        the worst outcome available here: telling someone a theme change is
        waiting for their approval when it went live across the whole app.
        """
        declared = getattr(session, "open_drafts", None)
        if not declared:
            return ""

        # The workspace names the API rather than the kind, so fall back to it
        # rather than printing a blank where the object type should be.
        def _kind_of(d: dict) -> str:
            return d.get("kind") or (
                DraftRegistry.resolve(d["api"])[0] if d.get("api") else ""
            )

        def _label(d: dict) -> str:
            return f"{_kind_of(d) or 'object'} '{d.get('name') or d.get('id')}'"

        # Two different fates, and the agent has to be able to tell the user
        # which one their change got. An object the server will draft is written
        # there and the tab refetches it; one it will not is kept in the browser
        # and waits for a Save that only the user can press.
        to_draft = [d for d in declared if drafting.get() and is_draftable(_kind_of(d))]
        to_browser = [d for d in declared if d not in to_draft]
        dirty_drafted = [d for d in to_draft if d.get("dirty")]

        lines: list[str] = []
        if to_browser:
            lines.append(
                "The user has these open in front of them, unsaved: "
                + ", ".join(_label(d) for d in to_browser) + ".\n"
                "Your edits to those appear on their screen straight away but are NOT "
                "saved. They review them and press Save themselves, so never tell them "
                "to reload, and never claim you have saved one."
            )
        if to_draft:
            lines.append(
                "These are open in a tab that reads this app's DRAFT: "
                + ", ".join(_label(d) for d in to_draft) + ".\n"
                "Your edits to those go to the draft and the tab refreshes itself, so "
                "the user sees them without reloading. They are saved, but only on the "
                "draft surface until somebody publishes."
            )
        if dirty_drafted:
            lines.append(
                "Careful: "
                + ", ".join(_label(d) for d in dirty_drafted)
                + " has edits the user has typed and not saved, and you cannot see "
                "them -- you are reading the draft, they are in the browser. Editing "
                "it now would give them a version of their object without their own "
                "work in it. Ask them to save first."
            )
        lines.append(
            "Everything else you touch IS saved the moment you touch it, including "
            "creating and deleting. When you change something the user does not have "
            "open, say which object it was and how far it reaches: a theme or style "
            "edit changes every page in the app.\n"
            "Listings read live state, so a drafted or unsaved rename will not show up "
            "in list_pages or list_storages. That is expected, not a stale cache."
        )
        return "\n".join(lines)

    _NAMED_PAGE_REF_KEYS: tuple[str, ...] = (
        "defaultPage", "loginPage", "shellPage", "forbiddenPage",
        "notFoundPage", "signUp", "forgotPasswordPage",
        "termsConditionPage", "privacyPolicyPage",
    )

    @staticmethod
    def _extract_named_page_refs(props: dict[str, Any]) -> list[tuple[str, str]]:
        """Pull (key, pageName) pairs from app properties. Handles both
        string values and ComponentProperty-shape `{"value": "..."}` dicts."""
        out: list[tuple[str, str]] = []
        for key in AppBuilderAgent._NAMED_PAGE_REF_KEYS:
            v = props.get(key)
            if isinstance(v, str) and v:
                out.append((key, v))
                continue
            if isinstance(v, dict):
                inner = v.get("value")
                if isinstance(inner, str) and inner:
                    out.append((key, inner))
        return out

    @staticmethod
    def _format_grounding(app_code: str, app_obj: dict | None, page_names: list[str]) -> str:
        """Render the fetched grounding into a Markdown section."""
        lines = [f"## Pre-flight grounding for app `{app_code}`",
                 "(fetched once at session start — use directly; don't re-fetch)"]
        if app_obj:
            page_refs = AppBuilderAgent._extract_named_page_refs(app_obj.get("properties") or {})
            if page_refs:
                lines.append("**Named page references (from application properties):**")
                lines.extend(f"- `{key}` → page `{name}`" for key, name in page_refs)
        if page_names:
            shown = page_names[:25]
            lines.append(f"**Pages in app ({len(page_names)} total, first {len(shown)}):**")
            lines.append("  " + ", ".join(f"`{n}`" for n in shown))
            if len(page_names) > len(shown):
                lines.append(f"  …and {len(page_names) - len(shown)} more. Use `list_pages` if you need them.")
        lines.append("")
        lines.append("Use these names directly. Don't call `list_pages` / `get_app` "
                     "again unless you need page contents (use `get_page`/`get_page_summary` for those).")
        return "\n".join(lines)

    @staticmethod
    def _first_app_from_response(resp: Any) -> dict | None:
        if not getattr(resp, "success", False) or not resp.data:
            return None
        content = (resp.data or {}).get("content") or []
        return content[0] if content else None

    @staticmethod
    def _page_names_from_response(resp: Any) -> list[str]:
        if not getattr(resp, "success", False) or not resp.data:
            return []
        return [
            p.get("name") for p in (resp.data or {}).get("content") or []
            if isinstance(p.get("name"), str)
        ]

    async def _fetch_grounding(self, session: BaseSession, app_code: str) -> tuple[dict | None, list[str]]:
        """Issue the two gateway calls and parse their responses.

        Uses the same singleton SaasClient + auth headers that every tool uses
        (`get_saas_client()` + `AuthContext.to_headers()`), so the round-trip
        looks identical to a tool call on the wire.

        Returns (app_obj_or_None, page_names). Empty/failed responses come
        back as None / []; the caller handles the "nothing to inject" case.
        """
        try:
            from app.agents.appbuilder.tools._shared import get_saas_client
            client = get_saas_client()
            headers = session.auth.to_headers()
            app_r = await client.get("/api/ui/applications", headers=headers,
                                     params={"appCode": app_code})
            pages_r = await client.get("/api/ui/pages", headers=headers,
                                       params={"appCode": app_code, "page": 0, "size": 30})
        except Exception as e:  # noqa: BLE001
            logger.debug("Pre-flight grounding fetch failed (%s: %s)", type(e).__name__, e)
            return None, []

        return self._first_app_from_response(app_r), self._page_names_from_response(pages_r)

    async def _build_preflight_grounding(self, session: BaseSession) -> str:
        """Fetch app definition + top page names once per session, cache on
        session.context, and format as a system-prompt section.

        Why: every new conversation otherwise starts with the agent calling
        `get_app` + `list_pages` + `search_page_components` (3-10 round-trips)
        before doing any actual work. Injecting this context up-front skips
        that entire pre-flight phase. Cached because it doesn't change within
        a conversation (an app's structure is stable; page CRUD invalidates
        but we accept the staleness for now — agent re-reads only when its
        action requires fresh data).

        Failure is silent — if the gateway is down or the app doesn't exist,
        we omit the section rather than blocking the conversation. The agent
        will fall back to its previous behaviour (call get_app itself).
        """
        if not session.auth:
            return ""
        app_code = self._effective_app_code(session)
        if not app_code:
            return ""
        # Keyed by app, not just present/absent. The block names the app and
        # lists its pages, so a cache that outlived a focus change would keep
        # telling the model it is in `appbuilder` looking at TestPage3 while its
        # own tools write to `crm`. `note_tool_outcome` evicts on the move; this
        # is the belt to that braces, and covers a focus set any other way.
        cached = session.context.get("_preflight_grounding")
        if isinstance(cached, str) and session.context.get("_preflight_grounding_app") == app_code:
            return cached

        session.context["_preflight_grounding_app"] = app_code
        app_obj, page_names = await self._fetch_grounding(session, app_code)
        if not app_obj and not page_names:
            session.context["_preflight_grounding"] = ""
            return ""
        text = self._format_grounding(app_code, app_obj, page_names)
        session.context["_preflight_grounding"] = text
        return text

    async def _build_learning_enhancement(self, session: BaseSession) -> str:
        """Retrieve and format learned knowledge for prompt injection."""
        try:
            from app.learning.prompt_enhancer import get_prompt_enhancer

            last_user_msg = extract_last_user_text(session.messages)
            if not last_user_msg:
                return ""

            return await get_prompt_enhancer().build_enhancement(
                agent_name=self.name,
                user_message=last_user_msg,
                session_context=session.context,
            )
        except Exception as e:
            logger.debug("Prompt enhancement skipped: %s", e)
            return ""
