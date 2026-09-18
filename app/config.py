"""Configuration settings for the AI service"""
import os
import logging
from pydantic_settings import BaseSettings
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """
    Application settings.
    
    Priority (highest to lowest):
    1. Environment variables
    2. Config server values (loaded at startup)
    3. Default values
    """
    
    # Service Identity
    SERVICE_NAME: str = "ai"
    SERVICE_PORT: int = 5001  # Changed to 5001
    
    # Eureka Service Discovery
    EUREKA_SERVER: str = "http://localhost:9999/eureka/"
    EUREKA_INSTANCE_HOST: str = "localhost"
    EUREKA_ENABLED: bool = True
    
    # Config Server
    # CLOUD_CONFIG_SERVER is the hostname (e.g., "config-server" in docker-compose)
    # Profile determines which config to fetch: ai/default, ai/ocidev, ai/ocistage, ai/ociprod
    CLOUD_CONFIG_SERVER: str = "localhost"
    CONFIG_SERVER_PORT: int = 8888
    CONFIG_SERVER_ENABLED: bool = True
    SPRING_PROFILES_ACTIVE: str = "default"  # Options: default, ocidev, ocistage, ociprod
    
    # Security Service (for token validation)
    # Can be overridden by config server: ai.security.url
    SECURITY_SERVICE_URL: str = "http://localhost:8080"
    
    # Files Service (for image uploads)
    # Can be overridden by config server: ai.files.url
    FILES_SERVICE_URL: str = "http://localhost:8000"
    
    # Redis (from config server: redis.url)
    # Used for rate limiting, request caching, and deduplication
    REDIS_URL: str = ""
    REDIS_ENABLED: bool = False  # Auto-enabled when URL is provided
    
    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 10  # Requests per minute per user
    RATE_LIMIT_PER_HOUR: int = 100  # Requests per hour per user

    # MySQL Database for AI Tracking
    # Can be overridden by config server: ai.db.*
    MYSQL_URL: str = ""  # JDBC URL: jdbc:mysql://localhost:3306/ai?serverTimezone=UTC
    MYSQL_USERNAME: str = "root"
    MYSQL_PASSWORD: str = ""
    AI_TRACKING_ENABLED: bool = False  # Auto-enabled when MYSQL_URL is configured

    # Context limits for conversation tracking (reporting/metadata only — the
    # agent loop does NOT trim on this). 48000 dated from the 64K-context
    # DeepSeek era; 112000 assumed a 128K floor. Both current DeepSeek models
    # (`deepseek-flash` and `deepseek-v4-pro`) document a 1M window, so report
    # against that. The output reservation the old value subtracted is noise at
    # this scale (AGENT_MAX_TOKENS is ~1.6% of the window).
    CONTEXT_LIMIT_DEFAULT: int = 1_000_000  # DeepSeek: 1M context window
    
    # LLM Provider Selection
    # Options: "anthropic", "openai", or "deepseek"
    # Can be overridden by config server: ai.llm.provider
    LLM_PROVIDER: str = "anthropic"
    
    # Anthropic Settings
    # Can be overridden by config server: ai.secrets.anthropicAPIKey
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_HAIKU: str = "claude-haiku-4-5-20251001"      # Fast model for analysis
    CLAUDE_SONNET: str = "claude-sonnet-4-6"             # Balanced model for generation
    
    # OpenAI Settings
    # Can be overridden by config server: ai.secrets.openaiAPIKey
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL_FAST: str = "gpt-4o-mini"    # Equivalent to Claude Haiku
    OPENAI_MODEL_BALANCED: str = "gpt-4o"      # Equivalent to Claude Sonnet

    # DeepSeek Settings
    # Can be overridden by config server: ai.secrets.deepSeekAPIKey
    DEEPSEEK_API_KEY: str = ""
    # `deepseek-flash` is DeepSeek-V4.1-Flash: 1M context, 384K max output, and
    # it accepts image input, so the AppBuilder reads its own screenshots
    # natively instead of paying for a Gemini text description of each one.
    #
    # Both tiers name the same model because both legacy ids we used to set here
    # -- `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` -- are retired
    # and already served by this one at Flash pricing. Keeping two spellings of
    # one model only hid that the "cheap" tier had been vision-capable all along.
    # The remaining separate model, `deepseek-v4-pro`, is dearer and text-only.
    #
    # See _DEEPSEEK_VISION_MODELS in app/services/llm_provider.py: pointing the
    # balanced tier at a text-only model turns the multimodal tool_result path
    # back off on its own.
    DEEPSEEK_MODEL_FAST: str = "deepseek-flash"
    DEEPSEEK_MODEL_BALANCED: str = "deepseek-flash"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_THINKING_ENABLED: bool = True            # Enable thinking/reasoning mode for balanced tier

    # MiniMax Settings — OpenAI-compatible Chat Completions API.
    # Can be overridden by config server: ai.secrets.minimaxAPIKey.
    # Default base URL is the international endpoint; the China endpoint
    # is `https://api.minimaxi.chat/v1` if the user prefers that.
    MINIMAX_API_KEY: str = ""
    MINIMAX_BASE_URL: str = "https://api.minimax.io/v1"
    # Available models as of 2026-06: M2, M2.1, M2.5, M2.7, M3 (each with
    # an optional `-highspeed` low-latency variant). M3 is the current
    # flagship; the `-highspeed` variants trade some quality for speed
    # and lower cost.
    MINIMAX_MODEL_FAST: str = "MiniMax-M2.7-highspeed"  # Fast/cheap tier
    MINIMAX_MODEL_BALANCED: str = "MiniMax-M3"           # Flagship — tool use + reasoning

    # Gemini Settings
    # Chosen as the CFA default after the Phase 8 bench: 1M context window,
    # native vision, ~13× cheaper than Claude Haiku on input — fits the
    # iterate-heavy generate-screenshot-fix loop.
    # API key lives in GOOGLE_API_KEY below (shared with all other Google
    # services — image gen, maps, etc.).
    GEMINI_MODEL_FAST: str = "gemini-2.5-flash-lite"   # Cheapest tier
    GEMINI_MODEL_BALANCED: str = "gemini-2.5-flash"    # CFA default

    # Admin token for the cross-env /api/ai/admin/* endpoints (per-app KB
    # export/import etc.). Must be set per env; if empty the admin routes
    # return 503 — safer than allowing unauthenticated access.
    ADMIN_TOKEN: str = ""

    # ── Headless browser pool (app/services/browser_pool) ──────────────
    # Chromium is shared per worker process and handed out as contexts, so a
    # render costs a ~216 MB renderer instead of a ~595 MB browser tree.
    #
    # SIZING. Every number here is PER GUNICORN WORKER, and we run four, so the
    # cluster ceiling is 4x what you read. A conversation is pinned to the
    # worker that accepted its SSE stream and holds at most one tab per app, so
    # N concurrent users spread over 4 workers means roughly N/4 sessions per
    # worker -- but the kernel balances accepts loosely, so assume a worker can
    # land noticeably more than its share.
    #
    # Rough budget: a tab is ~216 MB, a browser adds ~379 MB of fixed overhead
    # while it is alive, and browsers close after BROWSER_IDLE_TTL_SECONDS with
    # no tabs. Defaults below are sized for ~15 concurrent dev users:
    # 5 sessions x 4 workers = 20 tabs of headroom against 15 users, and
    # 8 contexts x 4 workers = 32 tabs as the absolute ceiling (~6.9 GB, which
    # 15 users cannot actually reach; the realistic peak is ~15 tabs, ~3.2 GB).
    # If dev regularly runs hotter than this, the answer is not bigger numbers
    # on a box with no mem_limit -- it is Chromium as its own service with one
    # pool shared across workers.
    #
    # Live contexts allowed at once. Each one with a page open is a renderer
    # process. Callers past the cap QUEUE (up to the timeout below) rather than
    # spawn, so this is the hard memory bound.
    BROWSER_MAX_CONTEXTS: int = 8
    # How long a caller waits for a free context before failing with a clear
    # error. Longer than the slowest render so a busy burst queues instead of
    # erroring, short enough that a stuck pool surfaces.
    BROWSER_ACQUIRE_TIMEOUT_SECONDS: int = 120
    # Close a browser once it has held zero contexts for this long. Keeps a
    # burst of renders on one browser without leaving Chromium resident on a
    # quiet worker. 0 keeps browsers alive for the process lifetime.
    BROWSER_IDLE_TTL_SECONDS: int = 300
    # Background sweep cadence: reaps idle drive_page sessions, then closes
    # idle browsers. Minimum 15s.
    BROWSER_SWEEP_INTERVAL_SECONDS: int = 60
    # Idle TTL for a persistent drive_page session (its context + tab). This
    # is the backstop; the primary release is the agent run ending.
    BROWSER_SESSION_IDLE_TTL_SECONDS: int = 600
    # Concurrent drive_page sessions per worker, kept below BROWSER_MAX_CONTEXTS
    # so long-lived sessions leave room for one-shot screenshots. This is a soft
    # cap: past it we reclaim finished and idle sessions, but never a live
    # conversation's tab, so a busy worker goes slightly over rather than
    # destroying someone's logged-in session (see _make_room_for_session).
    BROWSER_MAX_SESSIONS: int = 5

    # ── Lore ───────────────────────────────────────────────────────────
    # Curated, growing knowledge about each application (app/services/lore).
    # Requires the AI tracking database; silently no-ops without it.
    LORE_ENABLED: bool = True
    # Record every agent turn as an observation. Turning this off leaves the
    # explicit lore_note tool and the HTTP surface working, and only stops
    # the passive accumulation.
    LORE_OBSERVE_CHAT: bool = True
    # Record every successful definition write as an observation. This is the
    # path that carries real evidence: a build makes hundreds of edits and
    # about five turns, and an edit names the object it happened to.
    LORE_OBSERVE_EDITS: bool = True
    # Pending observations that trigger a background curation pass. 0 disables
    # auto-curation (the /curate endpoint and the admin sweep still work).
    LORE_AUTOCURATE_AT: int = 25
    # ...or this many pending about a SINGLE subject, whichever comes first.
    # An app-wide count is the wrong unit on its own: thirty scattered edits
    # across thirty objects say less than eight against one page, and the
    # second is what produces a good entry. Lower than the app threshold on
    # purpose.
    LORE_AUTOCURATE_SUBJECT_AT: int = 8

    # The tier curation runs on. This used to be hardcoded to "fast", which is
    # what produced 267 observations and zero entries on the first instance:
    # the cheap reasoning model spent its whole output budget thinking and
    # emitted no content at all, so every pass parsed an empty string. Keep it
    # separate from APPBUILDER_PROVIDER's tier — curation is rare (once per ~25
    # observations) and is the hardest single inference in the system.
    LORE_CURATOR_TIER: str = "balanced"
    # Output budget for one curation pass. Must leave room for a reasoning
    # model to think AND then emit the operations JSON: measured 20k chars of
    # reasoning plus 2.7k of content for a two-observation batch, which needs
    # ~5.3k completion tokens. 4000 was the old value and was never enough.
    LORE_CURATOR_MAX_TOKENS: int = 16000
    # Store the redacted model response on the run row. Off in production; on
    # for a debugging window. Always passes through curator.redact first — a
    # raw model response is exactly where a leaked token would land.
    LORE_KEEP_RAW_RESPONSE: bool = False
    # Give up on an observation the model has declined this many times. Without
    # this a row the curator will never use re-enters every batch forever.
    LORE_MAX_CURATION_ATTEMPTS: int = 3
    # Hard ceiling on one curation model call. The provider clients carry no
    # timeout of their own, and curation runs as a detached background task, so
    # without this a hung connection blocks that app's curation forever and
    # leaves the run row open (observed: a pass stuck 67 minutes on zero CPU).
    LORE_CURATOR_TIMEOUT_SECONDS: int = 240

    # Record the agent's own narration as an observation. Off by default: the
    # first 192 chat observations produced zero entries, and the assistant half
    # is self-description ("I'm an expert application builder...") that the
    # curator's own rules can never turn into durable knowledge.
    LORE_OBSERVE_AGENT_NARRATION: bool = False
    # Record an app object inventory once per session.
    LORE_OBSERVE_INVENTORY: bool = True
    # Record failures of tools that EXECUTE rather than edit. Repeated
    # identical failures collapse by fingerprint, which is the gotcha signal.
    LORE_OBSERVE_RUNS: bool = True
    # Put constraints/gotchas for a subject in front of the model in the SAME
    # turn as the first write to it, rather than on the next turn.
    LORE_ADVISE_BEFORE_EDITS: bool = True
    # Fold an app briefing into the system prompt on every request.
    #
    # OFF. Measured on two seeded apps, a 3,800-character briefing rendered 5
    # of 21 entries and 7 of 21: the ranking, not the task, decided what the
    # model saw, and two thirds of the knowledge was invisible. The same
    # entries as an index are under 1,600 characters, so the model can instead
    # see that everything exists and fetch what its task needs — `lore_index`
    # then `lore_get`. Nothing is pushed; the tool descriptions carry the
    # instruction to ask.
    #
    # The cost of this choice is real and worth naming: an agent that never
    # calls `lore_index` works with no lore at all. That is why `lore_index` is
    # a hot tool and why its description says to start there.
    LORE_PUSH_BRIEF: bool = False
    # Budget for that briefing when it is switched back on.
    LORE_BIG_PICTURE_BUDGET: int = 3800
    # Push what is known about ONE object when the agent first touches it.
    #
    # ON, and deliberately not covered by the decision above. The app briefing
    # was a blanket push: it arrived on every request whatever the task, and
    # the ranking chose what the model saw. This one is triggered by the
    # agent's own act of opening an object, carries at most 1,200 characters
    # about that object, and fires once per subject per session. It is closer
    # to an answer than to a broadcast, which is why it survives.
    LORE_PUSH_SUBJECT: bool = True

    # ── Blueprint ──────────────────────────────────────────────────────
    # The plan for an application: what it is MEANT to be, carried as a
    # `blueprint` field on every overridable object (app/services/blueprint).
    # Planning is one model call with a schema and no tools — routing it
    # through the AppBuilder agent would spend ~44K tokens of tool schemas
    # before the first word of a request that has nothing to call.
    BLUEPRINT_TIER: str = "balanced"
    # Output budget for one generation. A whole application plan with a
    # glossary, features and forty objects is a big JSON document, and a
    # reasoning model needs room to think AND then emit it.
    BLUEPRINT_MAX_TOKENS: int = 16000
    # Hard ceiling on one blueprint model call. The provider clients carry no
    # timeout of their own, and this one runs inside a request, so without it a
    # hung connection holds a worker until the browser gives up.
    #
    # Under the gateway's own 60s ceiling on purpose. Set to 180 it was a lie:
    # the gateway returned a bare 504 at 60s while this went on waiting, so the
    # caller got a timeout with no message and the service never learned its
    # request had been abandoned. Failing first means the failure is ours to
    # describe. A generation that genuinely needs longer than this — a plan for
    # a forty-page site — does not belong in a request at all and needs a job.
    BLUEPRINT_TIMEOUT_SECONDS: int = 50
    # Fold a short plan brief into the agent's per-request context. On by
    # default and deliberately small (~1.6K chars): the point is that the agent
    # knows a plan EXISTS, since one that does not know to ask will not ask.
    # The full plan is one blueprint_get away.
    BLUEPRINT_PUSH_BRIEF: bool = True

    # CFA code workspace — where shallow clones of nocode-saas/nocode-ui/
    # nocode-kirun live for code-reading tools. Per-instance mounted volume
    # in prod (/var/cfa/workspace); local dev falls back to siblings of
    # nocode-ai.
    CFA_WORKSPACE_DIR: str = "/var/cfa/workspace"

    # Optional override for service log directory used by tail_service_logs.
    # When empty the tool tries ../nocode-saas/logs relative to nocode-ai.
    MODLIX_LOG_DIR: str = ""

    # Google Settings
    # Can be overridden by config server: ai.secrets.googleAPIKey
    # Used for Google AI services (e.g. image generation)
    GOOGLE_API_KEY: str = ""

    # Google Maps key — separate from the Gemini/LLM key so they can be
    # rotated / restricted independently. Requires "Geocoding API" and
    # "Maps Static API" enabled on the GCP project.
    # Can be overridden by config server: ai.secrets.googleMapsAPIKey
    GOOGLE_MAPS_API_KEY: str = ""

    # Google Maps Map ID — required for Vector rendering with Feature Layers
    # (POSTAL_CODE, LOCALITY, COUNTRY, etc.). Must be a real Map ID from
    # Google Cloud Console — DEMO_MAP_ID does NOT support Feature Layers.
    # Can be overridden by config server: ai.secrets.googleMapID
    GOOGLE_MAP_ID: str = ""

    # Prompt Caching (Anthropic-only feature)
    # Reduces token usage by ~90% for repeated system prompts
    # Automatically disabled when using OpenAI
    PROMPT_CACHING_ENABLED: bool = True
    
    # Legacy - kept for backward compatibility
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
    
    # AIContext path (empty = use bundled aicontext in appbuilder package)
    AICONTEXT_PATH: str = ""
    
    # Website Import Settings
    WEBSITE_IMPORT_TIMEOUT: int = 30  # Timeout for website HTML fetching (seconds)
    SCREENSHOT_TIMEOUT: int = 60  # Timeout for screenshot capture (seconds)
    MAX_HTML_SIZE_MB: int = 10  # Maximum HTML size to process (MB)
    PLACEHOLDER_IMAGE_PATH: str = "api/files/static/file/SYSTEM/appbuilder/sample.svg"  # Default placeholder image

    # Image Upload Settings
    MAX_IMAGE_BASE64_MB: float = 4.5  # Max base64 size before compression (Anthropic limit is 5MB)
    IMAGE_MAX_DIMENSION: int = 1568  # Max pixels on longest side (Anthropic recommendation)

    # Chat attachment persistence.
    #
    # How long a file the user attached to a chat is kept. Stamped on the file
    # itself at upload as `expiresAfterMinutes`, which is what makes the
    # existing FILES_TTL_CLEANUP worker collect it — that job deletes only files
    # that were given a lifetime, so a generated image (uploaded with none) is
    # invisible to it at any age.
    #
    # A setting rather than a literal so retention can be changed per
    # environment. Changing it does NOT re-stamp files already uploaded: their
    # lifetime was fixed at upload, in files_file_system.
    CHAT_ATTACHMENT_TTL_MINUTES: int = 90 * 24 * 60  # 129600 — 90 days
    # Largest single attachment that will be stored. Bigger ones still reach the
    # model (compression handles that separately); they are just not kept.
    MAX_ATTACHMENT_MB: float = 25.0

    # Gateway URL (nocode-saas API gateway)
    # All agent tool calls route through this gateway
    # Can be overridden by config server: ai.gateway.url
    GATEWAY_URL: str = "http://localhost:8080"

    # Standalone mode — when true, the AI service reads the X-Path-Prefix header
    # from incoming requests and prepends it to all outgoing API calls.
    # This allows routing through the webpack dev server with the correct
    # /{appCode}/{clientCode}/page prefix. Has no effect in production.
    STANDALONE_MODE: bool = False

    # Agent Settings
    AGENT_MODEL_TIER: str = "balanced"  # "fast" (Haiku) or "balanced" (Sonnet)
    MAX_AGENT_TURNS: int = 160  # Max tool-use loop iterations per request. A full multi-section site clone (multi-res screenshots + asset copy + per-section build + hover/animation styling + screenshot self-QA) needs more headroom than 100.
    # Max tokens per LLM response. Raised from 16000 (a MiniMax M3-era number)
    # after prod session HHARS1_984fdbf5 stalled 8 times in 19 turns, every
    # stall a call returning exactly 16000 output tokens. On a thinking model
    # reasoning_content is billed as output and comes out of THIS budget, so a
    # turn could spend the lot deliberating and be cut before writing a tool
    # call. `deepseek-flash` documents 384K max output, so the ceiling is ours,
    # not the model's; 32000 clears the largest honest turn in that session
    # (11.6K) with room for the reasoning that shares it. The truncation itself
    # is now recoverable (see AGENT_MAX_TRUNCATION_CONTINUATIONS) — this only
    # makes it rare. Keep it within the smallest max-output of any provider the
    # agent can be pointed at.
    AGENT_MAX_TOKENS: int = 32000
    # How many times ONE turn may be resumed after being cut off at
    # AGENT_MAX_TOKENS. 0 restores the old behaviour (stop, say nothing).
    # Bounded because each resume is a fresh full-context call: a model that
    # truncates every time would otherwise burn the turn budget, and the
    # wallet, restarting the same answer.
    AGENT_MAX_TRUNCATION_CONTINUATIONS: int = 2

    # Which tools ship a FULL schema in the per-turn tools[] payload.
    #   "full" — the curated HOT_TOOLS set (64 tools, ~19.6K tok/turn).
    #   "off"  — none; every tool ships the stripped shape and reaches
    #            execution through _gate_deferred_dispatch's argument
    #            validation, which dispatches a well-formed guess immediately.
    # HOT_TOOLS existed to dodge a first-call synthetic retry that the
    # argument-validating gate made unnecessary; measured, the full set costs
    # 15,031 tokens more than the same tools stripped (the docstring's "3-5K"
    # is a 3-5x understatement) and occupies 13% of DeepSeek's 112K window.
    # "off" is the A/B arm that prices what that buys. Bench both before
    # changing the default.
    CFA_HOT_TOOLS: str = "full"

    # Conversation-history elision. There is NO context management on the
    # OpenAI-compatible path: `context_management` is an Anthropic-only
    # server-side beta, it is not configured for the AppBuilder, and the DeepSeek
    # create call ignores the parameter. So history grows unbounded — the Chit
    # Fund run reached context_percent 100 against a 112K window and hard-stopped
    # with no closing summary, and per-turn latency rose from ~4.5s on short
    # conversations to ~19s on the long ones purely from prefill growth.
    #
    # Old tool_result payloads are the bulk (4K each by default, 32K for
    # decompiles, plus screenshot images). Once the history passes
    # ELIDE_OVER_CHARS, results older than KEEP_RECENT_TURNS assistant turns are
    # replaced by a short stub that keeps a head of the original text. Small
    # results are left alone: they are cheap and often carry the ids the model
    # still needs. Set ELIDE_OVER_CHARS to 0 to disable entirely.
    AGENT_HISTORY_ELIDE_OVER_CHARS: int = 200_000   # ~50K tokens
    AGENT_HISTORY_KEEP_RECENT_TURNS: int = 6
    AGENT_HISTORY_ELIDE_MIN_RESULT_CHARS: int = 1500
    # Screenshots are the real bulk and need a MUCH shorter window than text.
    # Measured: a light-12 run reached 721,910 chars of history while the text
    # pass reclaimed 5,405, because the weight was images sitting inside the
    # 6-turn text window. One screenshot is 100-500KB of base64 and it is paid
    # again on every turn it survives, while the model has already read it and
    # written down what it saw. Kept small, but never zero: the visual critique
    # loop (screenshot -> patch -> screenshot -> compare) needs the previous
    # shot. The newest image is always kept regardless of this number.
    AGENT_HISTORY_KEEP_IMAGES_TURNS: int = 3

    # Per-agent LLM provider overrides (fall back to LLM_PROVIDER if not set)
    APPBUILDER_PROVIDER: str = "deepseek"  # AppBuilder LLM provider — DeepSeek, running the balanced tier (DEEPSEEK_MODEL_BALANCED = deepseek-flash). Native vision means `describe_image`/Gemini-describe is no longer on the screenshot path.
    ADZUMP_PROVIDER: str = "openai"  # Adzump (legacy) LLM provider
    ADZUMP2_PROVIDER: str = "minimax"  # Adzump2 LLM provider
    LEADZUMP_PROVIDER: str = "deepseek"  # LeadZump CRM assistant — same provider and
    # balanced tier as AppBuilder, so the two agents share one model and one set of
    # provider quirks to reason about rather than two.
    # Which backend `generate_image` renders with. "minimax" is image-01 on
    # MINIMAX_BASE_URL; "gemini" is gemini-2.5-flash-image / Nano Banana.
    #
    # MiniMax is the default because of aspect ratio, which is most of what
    # this tool is asked for. Benched 2026-09-16 over 5 prompts: Gemini has no
    # aspect field, so the ratio is a sentence appended to the prompt and the
    # model ignores it — every request came back 1024x1024, including the 16:9
    # and 4:3 ones (3/5 wrong). image-01 takes aspect_ratio as a real field and
    # was exact 5/5. Gemini is ~2x faster (median 9.1s vs 18.5s) and renders
    # cleaner lettering, so pass image_provider="gemini" for a typographic
    # image; it also reproduced a real brand's logo on a prompt asking for an
    # unbranded product, which is why it is no longer the default for the
    # product shots this tool generates for customers.
    #
    # Gemini stays the fallback on two counts, both in _execute_generate_image:
    #   - capability: image-01's only image input is `subject_reference`, ONE
    #     image typed `character`, for carrying a person's likeness across
    #     scenes. A real multi-image edit can only be Gemini.
    #   - availability: a failed MiniMax render retries on Gemini.
    # Both are reported in the tool summary rather than applied silently,
    # because the fallback render will NOT honour the aspect ratio.
    #
    # Note MiniMax's *chat* models cannot do this at all: ask MiniMax-M3 for an
    # image and it prints an image_generation call as text and then narrates
    # "the image has been generated" with nothing attached. Image gen is a
    # separate model on a separate endpoint, never a chat completion.
    IMAGE_PROVIDER: str = "minimax"  # minimax | gemini
    IMAGE_EDIT_FALLBACK_TO_GEMINI: bool = True   # >1 input image → Gemini
    IMAGE_ERROR_FALLBACK_TO_GEMINI: bool = True  # MiniMax render failed → Gemini
    MINIMAX_IMAGE_MODEL: str = "image-01"
    COMPONENT_CATALOG_URL: str = ""  # CDN URL for component-catalog.json (empty = use fallback)
    # Where nocode-ui's generated catalog lives, for a dev box. Accepts the
    # client dir, its dist/ dir, or the JSON file. Empty auto-resolves to a
    # sibling nocode-ui checkout. A local catalog whose `generatedAt` is newer
    # than the CDN one wins, so regenerating after a component change takes
    # effect without editing this.
    COMPONENT_CATALOG_LOCAL_PATH: str = ""
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"
    
    def apply_config_server_values(self, config: Dict[str, Any]):
        """
        Apply values from config server.
        
        Config server provides:
        - ai.security.url -> SECURITY_SERVICE_URL
        - ai.files.url -> FILES_SERVICE_URL
        - ai.secrets.anthropicAPIKey -> ANTHROPIC_API_KEY
        - ai.secrets.openaiAPIKey -> OPENAI_API_KEY
        - ai.secrets.googleAPIKey -> GOOGLE_API_KEY
        - ai.llm.provider -> LLM_PROVIDER
        - redis.url -> REDIS_URL
        """
        if not config:
            return
        
        # Map config server keys to settings
        # Format: (nested_keys_tuple) -> attribute_name
        mappings = {
            ("security", "url"): "SECURITY_SERVICE_URL",
            ("files", "url"): "FILES_SERVICE_URL",
            ("secrets", "anthropicAPIKey"): "ANTHROPIC_API_KEY",
            ("secrets", "openaiAPIKey"): "OPENAI_API_KEY",
            ("secrets", "deepSeekAPIKey"): "DEEPSEEK_API_KEY",
            ("secrets", "minimaxAPIKey"): "MINIMAX_API_KEY",
            ("secrets", "googleAPIKey"): "GOOGLE_API_KEY",
            ("secrets", "googleMapsAPIKey"): "GOOGLE_MAPS_API_KEY",
            ("secrets", "minimaxAPIKey"): "MINIMAX_API_KEY",
            ("secrets", "googleMapID"): "GOOGLE_MAP_ID",
            ("llm", "provider"): "LLM_PROVIDER",
            ("gateway", "url"): "GATEWAY_URL",
            ("componentCatalogUrl",): "COMPONENT_CATALOG_URL",
        }
        
        for keys, attr in mappings.items():
            value = config
            try:
                for key in keys:
                    value = value[key]
                
                # Only apply if not already set via environment
                env_value = os.getenv(attr)
                if not env_value:
                    setattr(self, attr, value)
                    logger.info(f"Applied config server value for {attr}")
            except (KeyError, TypeError):
                pass
        
        # Special handling for Redis URL
        # Priority: 1) ai.redis.url (ai-specific config), 2) redis.url (shared config)
        try:
            # First try ai-specific redis config
            redis_url = config.get("redis", {}).get("url")
            if not redis_url:
                # Fall back to top-level redis config (shared across services)
                # This is fetched separately from config server
                pass

            if redis_url and not os.getenv("REDIS_URL"):
                self.REDIS_URL = redis_url
                self.REDIS_ENABLED = True
                logger.info("Applied config server value for REDIS_URL")
        except (KeyError, TypeError, AttributeError):
            pass

        # Special handling for AI database config (under "db" key like other services)
        # Config structure: ai.db: { url: "jdbc:mysql://...", username: "...", password: "..." }
        try:
            db_config = config.get("db", {})
            if db_config.get("url") and not os.getenv("MYSQL_URL"):
                self.MYSQL_URL = db_config.get("url", "")
                self.MYSQL_USERNAME = db_config.get("username", "root")
                self.MYSQL_PASSWORD = db_config.get("password", "")
                self.AI_TRACKING_ENABLED = True
                logger.info("Applied config server values for AI database")
        except (KeyError, TypeError, AttributeError):
            pass

        # Agent-level config — adzump credentials under ai.adzump.*
        try:
            from app.agents.adzump.config import load_from_config_server as _load_adzump
            _load_adzump(config)
            logger.info("Applied config server values for adzump credentials")
        except Exception as e:
            logger.warning(f"Failed to load adzump config: {e}")


# Global settings instance
settings = Settings()


async def initialize_settings():
    """
    Initialize settings from config server.
    
    Should be called during application startup.
    """
    from app.services.config_server import initialize_config_from_server
    
    if settings.CONFIG_SERVER_ENABLED:
        config = await initialize_config_from_server()
        settings.apply_config_server_values(config)
    else:
        # No config server — still load adzump config from env vars alone.
        try:
            from app.agents.adzump.config import load_from_config_server as _load_adzump
            _load_adzump({})
        except Exception as e:
            logger.warning(f"Failed to load adzump config from env: {e}")

    # Log final configuration (mask sensitive values)
    logger.info(f"Service: {settings.SERVICE_NAME}")
    logger.info(f"Port: {settings.SERVICE_PORT}")
    logger.info(f"LLM Provider: {settings.LLM_PROVIDER.upper()}")
    logger.info(f"Security URL: {settings.SECURITY_SERVICE_URL}")
    logger.info(f"Files URL: {settings.FILES_SERVICE_URL}")
    
    if settings.LLM_PROVIDER == "anthropic":
        logger.info(f"Anthropic API Key: {'*' * 20 + settings.ANTHROPIC_API_KEY[-8:] if settings.ANTHROPIC_API_KEY else 'NOT SET'}")
        logger.info(f"Models: Haiku={settings.CLAUDE_HAIKU}, Sonnet={settings.CLAUDE_SONNET}")
        logger.info(f"Prompt Caching: {'ENABLED' if settings.PROMPT_CACHING_ENABLED else 'DISABLED'}")
    elif settings.LLM_PROVIDER == "deepseek":
        logger.info(f"DeepSeek API Key: {'*' * 20 + settings.DEEPSEEK_API_KEY[-8:] if settings.DEEPSEEK_API_KEY else 'NOT SET'}")
        logger.info(f"Models: Fast={settings.DEEPSEEK_MODEL_FAST}, Balanced={settings.DEEPSEEK_MODEL_BALANCED}")
    else:
        logger.info(f"OpenAI API Key: {'*' * 20 + settings.OPENAI_API_KEY[-8:] if settings.OPENAI_API_KEY else 'NOT SET'}")
        logger.info(f"Models: Fast={settings.OPENAI_MODEL_FAST}, Balanced={settings.OPENAI_MODEL_BALANCED}")

    if settings.APPBUILDER_PROVIDER != settings.LLM_PROVIDER:
        logger.info(f"AppBuilder Provider Override: {settings.APPBUILDER_PROVIDER.upper()}")
        if settings.APPBUILDER_PROVIDER == "deepseek":
            logger.info(f"DeepSeek API Key: {'*' * 20 + settings.DEEPSEEK_API_KEY[-8:] if settings.DEEPSEEK_API_KEY else 'NOT SET'}")
            logger.info(f"DeepSeek Models: Fast={settings.DEEPSEEK_MODEL_FAST}, Balanced={settings.DEEPSEEK_MODEL_BALANCED}")
        elif settings.APPBUILDER_PROVIDER == "minimax":
            logger.info(f"MiniMax API Key: {'*' * 20 + settings.MINIMAX_API_KEY[-8:] if settings.MINIMAX_API_KEY else 'NOT SET'}")
            logger.info(f"MiniMax Base URL: {settings.MINIMAX_BASE_URL}")
            logger.info(f"MiniMax Models: Fast={settings.MINIMAX_MODEL_FAST}, Balanced={settings.MINIMAX_MODEL_BALANCED}")

    if settings.ADZUMP2_PROVIDER != settings.LLM_PROVIDER:
        logger.info(f"Adzump2 Provider Override: {settings.ADZUMP2_PROVIDER.upper()}")
        if settings.ADZUMP2_PROVIDER == "minimax":
            logger.info(f"MiniMax API Key: {'*' * 20 + settings.MINIMAX_API_KEY[-8:] if settings.MINIMAX_API_KEY else 'NOT SET'}")
            logger.info(f"MiniMax Base URL: {settings.MINIMAX_BASE_URL}")
            logger.info(f"MiniMax Models: Fast={settings.MINIMAX_MODEL_FAST}, Balanced={settings.MINIMAX_MODEL_BALANCED}")

    logger.info(f"Google API Key: {'*' * 20 + settings.GOOGLE_API_KEY[-8:] if settings.GOOGLE_API_KEY else 'NOT SET'}")
    logger.info(f"Redis: {'ENABLED - ' + settings.REDIS_URL[:30] + '...' if settings.REDIS_ENABLED else 'DISABLED'}")
    logger.info(f"Rate Limit: {settings.RATE_LIMIT_PER_MINUTE}/min, {settings.RATE_LIMIT_PER_HOUR}/hour")
    logger.info(f"AI Tracking: {'ENABLED - ' + settings.MYSQL_URL[:50] + '...' if settings.AI_TRACKING_ENABLED else 'DISABLED'}")
    logger.info(f"Gateway URL: {settings.GATEWAY_URL}")
    logger.info(f"Agent: model_tier={settings.AGENT_MODEL_TIER}, max_turns={settings.MAX_AGENT_TURNS}, max_tokens={settings.AGENT_MAX_TOKENS}")
