"""Checks on the OG card service's own logic, not on any model's output.

No live model and no network: the two render backends, the upload and the
reference fetch are all monkeypatched, the way `test_image_provider_routing`
does it. What is left is the part that decides things, which is the part that
has been wrong before.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.services import og_image_ai as O
from app.agents.appbuilder.tools.modlix import visuals as V


def _png(width: int, height: int, colour=(120, 80, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


def _dims(raw: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(raw))
    return img.width, img.height


class TestRequestModel:
    """The body the builder actually sends, captured off the wire.

    The pane builds its payload with `System.Make`, which interpolates every
    slot of a `resultShape` whether the path resolves or not, so the two
    reference-image slots arrive as nulls when nobody picked a file. `List[str]`
    rejected that with a 422 before the handler ran, which is every request that
    does not use a reference image.
    """

    # Verbatim from a Playwright capture of the Generate button on
    # workspace/sitezump, no reference image chosen.
    FROM_THE_WIRE = {
        "page_name": "global",
        "image_urls": [None, None],
        "prompt": "A calm abstract plate in warm orange",
        "app_code": "sitezump",
        "client_code": "SYSTEM",
    }

    def test_accepts_the_body_the_builder_sends(self):
        from app.agents.appbuilder.router import OgImageRequest

        m = OgImageRequest(**self.FROM_THE_WIRE)
        assert m.image_urls == [None, None]
        assert m.prompt == "A calm abstract plate in warm orange"

    def test_accepts_one_filled_slot(self):
        from app.agents.appbuilder.router import OgImageRequest

        m = OgImageRequest(**{**self.FROM_THE_WIRE,
                              "image_urls": [None, "https://example.com/a.png"]})
        assert m.image_urls[1].endswith("a.png")

    def test_every_optional_slot_tolerates_null(self):
        """`System.Make` sends null for any slot whose path did not resolve.

        An empty box, an app with no description, a page scope nobody armed:
        each arrives as an explicit null, not an omitted key. This asserts the
        whole surface rather than the two fields that happened to break.
        """
        from app.agents.appbuilder.router import OgImageRequest

        nulled = {k: None for k in
                  ("app_code", "client_code", "page_name", "style_notes",
                   "image_provider", "site_name", "site_description")}
        m = OgImageRequest(prompt="x", image_urls=[None, None], **nulled)
        for k in nulled:
            assert getattr(m, k) == "", k

    @pytest.mark.asyncio
    async def test_null_slots_collect_to_nothing(self):
        # The nulls must not survive as reference images, or the provider
        # routing would send a bare prompt to Gemini and come back square.
        assert await O.collect_reference_images([None, None], None) == []
        assert await O.collect_reference_images([None, "  "], []) == []


class TestCardGeometry:
    """Whatever the backend returns, the card is the size LinkedIn documents."""

    def test_crops_a_16_9_render_to_the_card(self):
        out, w, h = O.to_card(_png(1920, 1080))
        assert (w, h) == (O.OG_WIDTH, O.OG_HEIGHT)
        assert _dims(out) == (O.OG_WIDTH, O.OG_HEIGHT)

    def test_crops_a_square_render_to_the_card(self):
        # The Gemini case. It ignores aspect_ratio and always returns square, so
        # this path runs on every reference-image render.
        out, _, _ = O.to_card(_png(1024, 1024))
        assert _dims(out) == (O.OG_WIDTH, O.OG_HEIGHT)

    def test_pads_nothing_and_crops_a_tall_render(self):
        out, _, _ = O.to_card(_png(600, 1600))
        assert _dims(out) == (O.OG_WIDTH, O.OG_HEIGHT)

    def test_emits_jpeg(self):
        out, _, _ = O.to_card(_png(1920, 1080))
        # JPEG's magic number. The tool upstream promises PNG; the card is JPEG
        # because WhatsApp drops to a small-icon layout on a large file.
        assert out[:3] == b"\xff\xd8\xff"

    def test_flattens_transparency_instead_of_blackening_it(self):
        buf = io.BytesIO()
        Image.new("RGBA", (1920, 1080), (255, 255, 255, 0)).save(buf, format="PNG")
        out, _, _ = O.to_card(buf.getvalue())
        assert _dims(out) == (O.OG_WIDTH, O.OG_HEIGHT)

    def test_keeps_a_photographic_card_under_the_whatsapp_limit(self):
        # Noise is the worst case for the quality ladder: a flat colour would
        # pass at any setting and prove nothing.
        import random

        random.seed(0)
        noisy = Image.new("RGB", (1920, 1080))
        noisy.putdata([
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(1920 * 1080)
        ])
        buf = io.BytesIO()
        noisy.save(buf, format="PNG")
        out, _, _ = O.to_card(buf.getvalue())
        assert len(out) <= O.OG_MAX_BYTES, f"card is {len(out)} bytes"


class TestFilename:
    """A name nothing has served before, because overwriting does not work."""

    def test_stamps_the_content(self):
        assert O.card_filename(b"one") != O.card_filename(b"two")

    def test_is_stable_for_the_same_bytes(self):
        assert O.card_filename(b"same") == O.card_filename(b"same")

    def test_is_a_jpg(self):
        assert O.card_filename(b"x").startswith("og-")
        assert O.card_filename(b"x").endswith(".jpg")


class TestProviderRouting:
    """image-01 honours aspect; its one reference slot means a person's face."""

    def test_no_reference_goes_to_minimax(self):
        provider, _ = O.pick_provider([], "")
        assert provider == "minimax"

    def test_any_reference_goes_to_gemini(self):
        # Stricter than visuals._render, which only reroutes above one image.
        # The single-reference case is the one that misbehaves quietly.
        provider, _ = O.pick_provider([("image/png", b"x")], "")
        assert provider == "gemini"

    def test_an_explicit_request_is_honoured(self):
        provider, _ = O.pick_provider([("image/png", b"x")], "minimax")
        assert provider == "minimax"


class TestReferenceUrlGuards:
    """The caller supplies the URL and the server fetches it. That is SSRF."""

    @pytest.mark.asyncio
    async def test_rejects_a_non_http_scheme(self):
        with pytest.raises(O.OgImageError, match="http"):
            await O.fetch_reference_image("file:///etc/passwd")

    @pytest.mark.asyncio
    async def test_rejects_loopback(self):
        with pytest.raises(O.OgImageError, match="public address"):
            await O.fetch_reference_image("http://127.0.0.1/x.png")

    @pytest.mark.asyncio
    async def test_rejects_localhost_by_name(self):
        with pytest.raises(O.OgImageError, match="public address"):
            await O.fetch_reference_image("http://localhost/x.png")

    @pytest.mark.asyncio
    async def test_rejects_the_cloud_metadata_address(self):
        # 169.254.169.254 is where instance credentials live on every major
        # cloud. It is the reason this check exists.
        with pytest.raises(O.OgImageError, match="public address"):
            await O.fetch_reference_image("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.asyncio
    async def test_rejects_a_private_range(self):
        with pytest.raises(O.OgImageError, match="public address"):
            await O.fetch_reference_image("http://10.0.0.5/x.png")

    @pytest.mark.asyncio
    async def test_rejects_a_non_image_content_type(self, monkeypatch):
        monkeypatch.setattr(O, "_is_public_address", lambda host: True)

        class Resp:
            status_code = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"<html>"

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return Resp()

        monkeypatch.setattr(O.httpx, "AsyncClient", lambda **kw: Client())
        with pytest.raises(O.OgImageError, match="not an image"):
            await O.fetch_reference_image("https://example.com/page")

    @pytest.mark.asyncio
    async def test_rejects_an_oversized_body(self, monkeypatch):
        monkeypatch.setattr(O, "_is_public_address", lambda host: True)

        class Resp:
            status_code = 200
            headers = {"content-type": "image/png"}
            content = b"x" * (O.MAX_REFERENCE_BYTES + 1)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return Resp()

        monkeypatch.setattr(O.httpx, "AsyncClient", lambda **kw: Client())
        with pytest.raises(O.OgImageError, match="over the"):
            await O.fetch_reference_image("https://example.com/big.png")

    @pytest.mark.asyncio
    async def test_refuses_to_follow_a_redirect(self, monkeypatch):
        # A public URL redirecting to an internal address is the standard way
        # past the address check, so redirects are not followed at all.
        monkeypatch.setattr(O, "_is_public_address", lambda host: True)

        class Resp:
            status_code = 302
            headers = {"location": "http://169.254.169.254/"}
            content = b""

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return Resp()

        monkeypatch.setattr(O.httpx, "AsyncClient", lambda **kw: Client())
        with pytest.raises(O.OgImageError, match="redirects"):
            await O.fetch_reference_image("https://example.com/r")

    @pytest.mark.asyncio
    async def test_caps_how_many_references_one_request_may_carry(self):
        attachments = [
            {"type": "image", "mime_type": "image/png", "data": base64.b64encode(b"x").decode()}
            for _ in range(O.MAX_REFERENCE_IMAGES + 1)
        ]
        with pytest.raises(O.OgImageError, match="limit is"):
            await O.collect_reference_images([], attachments)


@pytest.fixture
def stub_backends(monkeypatch):
    """The two renderers, the upload and the asset recorder. No network."""
    calls: dict = {}

    async def fake_minimax(api_key, prompt, model, aspect, input_images=None):
        calls["provider"] = "minimax"
        calls["aspect"] = aspect
        return _png(1920, 1080), ""

    async def fake_gemini(api_key, prompt, model, input_images=None):
        calls["provider"] = "gemini"
        return _png(1024, 1024), ""

    async def fake_upload(local_path, page_name, folder, filename, app_code, client_code, headers,
                          mime_type="image/png"):
        calls["upload"] = {
            "folder": folder, "filename": filename, "page": page_name,
            "mime": mime_type, "app": app_code, "client": client_code,
        }
        rel = f"/api/files/static/file/{client_code}/{app_code}/{page_name}/{folder}/{filename}"
        return rel, f"https://gw.test{rel}", ""

    monkeypatch.setattr(V, "_generate_via_minimax", fake_minimax)
    monkeypatch.setattr(V, "_generate_via_gemini", fake_gemini)
    monkeypatch.setattr(V, "_upload_generated_static", fake_upload)

    from app.config import settings

    monkeypatch.setattr(settings, "MINIMAX_API_KEY", "test", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", "test", raising=False)
    return calls


class TestGenerate:
    """The published card, with the assembling agent stubbed out.

    The agent is one seam: `assemble_card`. Stubbing it keeps these about what
    `generate_og_image` does with the result -- crop, encode, name, upload --
    which is the deterministic part and the part that has been wrong before.
    """

    @staticmethod
    def _agent(card_bytes=None, warnings=None, provider="minimax"):
        async def fake(*, instruction, render, logos, work_dir, auth):
            from pathlib import Path
            p = Path(work_dir) / "plate.png"
            p.write_bytes(card_bytes if card_bytes is not None else _png(1200, 630))
            return str(p), list(warnings or []), provider, "image-01"
        return fake

    @pytest.mark.asyncio
    async def test_renders_crops_and_publishes(self, stub_backends, monkeypatch):
        import app.services.og_card as C

        monkeypatch.setattr(C, "assemble_card", self._agent())
        out = await O.generate_og_image(prompt="A calm plate", app_code="sitezump",
                                        client_code="SYSTEM")
        assert out["width"] == O.OG_WIDTH
        assert out["height"] == O.OG_HEIGHT
        assert out["type"] == "image/jpeg"
        assert out["url"].startswith("https://gw.test/api/files/static/file/SYSTEM/")
        # The folder is fixed, so an app's cards are findable as a group.
        assert stub_backends["upload"]["folder"] == "og"
        assert stub_backends["upload"]["mime"] == "image/jpeg"

    @pytest.mark.asyncio
    async def test_carries_the_agents_warnings_out(self, stub_backends, monkeypatch):
        import app.services.og_card as C

        monkeypatch.setattr(C, "assemble_card",
                            self._agent(warnings=["rendered on gemini"]))
        out = await O.generate_og_image(prompt="x", app_code="a", client_code="SYSTEM")
        assert any("gemini" in w for w in out["warnings"])

    @pytest.mark.asyncio
    async def test_falls_back_when_the_agent_assembles_nothing(self, stub_backends, monkeypatch):
        """A stalled agent must not cost the person their card."""
        import app.services.og_card as C

        async def nothing(*, instruction, render, logos, work_dir, auth):
            return None, ["the assembling step stopped early"], "", ""

        monkeypatch.setattr(C, "assemble_card", nothing)

        class P:
            async def create_completion(self, **kw):
                return {"content": "A wide calm gradient."}

        monkeypatch.setattr("app.services.llm_provider.get_llm_provider", lambda *a, **k: P())
        out = await O.generate_og_image(prompt="x", app_code="a", client_code="SYSTEM")
        assert out["width"] == O.OG_WIDTH
        assert any("plain generated plate" in w for w in out["warnings"])

    @pytest.mark.asyncio
    async def test_requires_a_prompt(self, stub_backends):
        with pytest.raises(O.OgImageError, match="prompt is required"):
            await O.generate_og_image(prompt="   ", app_code="a", client_code="SYSTEM")

    @pytest.mark.asyncio
    async def test_an_unusable_logo_does_not_lose_the_card(self, stub_backends, monkeypatch):
        """An SVG logo is refused, and the card is still made without it."""
        import app.services.og_card as C

        async def boom(urls, attachments):
            raise O.OgImageError("that logo is image/svg+xml, which cannot be composited")

        monkeypatch.setattr(O, "collect_reference_images", boom)
        monkeypatch.setattr(C, "assemble_card", self._agent())
        out = await O.generate_og_image(
            prompt="use the logo at https://x.test/logo.svg",
            app_code="a", client_code="SYSTEM")
        assert out["width"] == O.OG_WIDTH
        assert any("svg" in w.lower() for w in out["warnings"])


class TestBrief:
    """What reaches the renderer is a description, not what was typed.

    A text-to-image model takes a picture description. People type requests. The
    case that prompted this: "Can you pelase generate a og image for
    https://sitezump.ai?" went to image-01 verbatim and came back a crowd of
    anime characters, because the only concrete tokens in it are a misspelling
    and a URL the model cannot open.
    """

    REQUEST = "Can you pelase generate a og image for https://sitezump.ai?"

    def test_the_site_is_put_in_front_of_the_model(self):
        msg = O._brief_request(self.REQUEST, "SiteZump AI",
                               "Build high-converting landing pages with AI")
        assert "SiteZump AI" in msg
        assert "landing pages" in msg
        assert self.REQUEST in msg

    def test_omits_what_it_does_not_know(self):
        msg = O._brief_request("a calm plate", "", "")
        assert msg == "They typed: a calm plate"

    @pytest.mark.asyncio
    async def test_uses_the_brief_the_model_returns(self, monkeypatch):
        class P:
            async def create_completion(self, **kw):
                return {"content": "  A wide abstract gradient in warm amber, soft light.  "}

        monkeypatch.setattr("app.services.llm_provider.get_llm_provider", lambda *a, **k: P())
        brief, warn = await O.build_brief(self.REQUEST, "SiteZump AI", "Landing pages")
        assert brief == "A wide abstract gradient in warm amber, soft light."
        assert warn == ""

    @pytest.mark.asyncio
    async def test_a_dead_model_falls_back_to_the_prompt(self, monkeypatch):
        # A brief is an improvement, not a precondition. Losing the model must
        # not lose the user their click.
        def boom(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr("app.services.llm_provider.get_llm_provider", boom)
        brief, warn = await O.build_brief("a calm plate", "", "")
        assert brief == "a calm plate"
        assert "used it as typed" in warn

    @pytest.mark.asyncio
    async def test_a_question_back_is_not_a_brief(self, monkeypatch):
        class P:
            async def create_completion(self, **kw):
                return {"content": "What sort of image would you like?"}

        monkeypatch.setattr("app.services.llm_provider.get_llm_provider", lambda *a, **k: P())
        brief, warn = await O.build_brief("a calm plate", "", "")
        assert brief == "a calm plate"
        assert "unusable" in warn

    @pytest.mark.asyncio
    async def test_the_fallback_renders_the_brief_not_the_prompt(self, stub_backends, monkeypatch):
        """On the fallback path the brief is still what reaches the renderer."""
        import app.services.og_card as C

        async def nothing(*, instruction, render, logos, work_dir, auth):
            return None, [], "", ""

        monkeypatch.setattr(C, "assemble_card", nothing)

        class P:
            async def create_completion(self, **kw):
                return {"content": "A wide abstract gradient in warm amber, soft light."}

        monkeypatch.setattr("app.services.llm_provider.get_llm_provider", lambda *a, **k: P())
        sent = {}

        async def spy(provider, prompt, *a, **kw):
            sent["prompt"] = prompt
            return _png(1920, 1080), provider, "image-01", "", ""

        monkeypatch.setattr(V, "_render", spy)
        await O.generate_og_image(prompt=self.REQUEST, app_code="sitezump",
                                  client_code="SYSTEM", site_name="SiteZump AI")
        assert "sitezump.ai" not in sent["prompt"]
        assert sent["prompt"].startswith("A wide abstract gradient")
