"""The scene authoring tools.

The tests that matter here are about the FAILURE MESSAGES, not the happy path.
A scene that renders black is the failure that actually happens, and every one
of these checks exists because the silent version of it would send the agent
editing the wrong thing: rewriting a working shader because the checker had no
GPU, or hunting a layout bug when the real problem is that the scene has no
light in it.
"""

import json

import pytest

from app.agents.appbuilder.tools.modlix.scenes import (
    TOOLS,
    _annotate_compile_log,
    _apply_scene_patch,
    _frames_differ,
    _paged,
    validate_scene_document,
)


@pytest.fixture(autouse=True)
def published_contract(monkeypatch):
    """Install the REAL published contract for the duration of each test.

    Validation reads its enumerations from the catalog rather than from a
    Python copy, so with no catalog loaded it runs structural checks only and
    says so. Tests that assert on enum behaviour therefore have to supply one,
    and supplying the real generated file rather than a hand-written stub is
    the point: a stub would drift from the platform exactly the way the Python
    mirror this design avoids would have.
    """
    import json as _json
    import os
    from app.agents.appbuilder.tools.modlix import scenes as mod

    path = os.path.expanduser(
        "~/kiran/fincity/nocode-ui/ui-app/client/dist/component-catalog.json"
    )
    contract = {}
    if os.path.exists(path):
        with open(path) as fh:
            contract = (_json.load(fh) or {}).get("scenes") or {}
    if not contract:
        pytest.skip("no generated component-catalog.json with a `scenes` block to test against")
    monkeypatch.setattr(mod, "_scene_contract", lambda: contract)
    return contract


def scene(**over):
    base = {
        "version": 1,
        "objects": [
            {
                "id": "hero",
                "name": "Hero",
                "visible": True,
                "source": {"kind": "primitive", "shape": "box"},
                "material": {"color": "#ffffff"},
            }
        ],
        "lights": [{"id": "key", "type": "directional", "intensity": 2, "position": [1, 2, 3]}],
        "shaders": [],
        "camera": {"type": "perspective"},
        "timeline": {"driver": "time", "tracks": []},
        "interactions": [],
        "environment": {},
    }
    base.update(over)
    return base


class TestToolSurface:
    def test_every_tool_is_registered(self):
        names = {t.name for t in TOOLS}
        assert names == {
            "list_scene_presets", "get_scene", "validate_scene", "set_scene",
            "patch_scene", "compile_shader", "render_scene_check",
            "set_scroll_animation", "fetch_external_asset",
        }

    def test_every_required_parameter_is_documented(self):
        # The registry-wide invariant, asserted locally too so a broken tool
        # fails in the file that owns it rather than somewhere else.
        for tool in TOOLS:
            for p in tool.parameters:
                assert p.description, f"{tool.name}.{p.name} has no description"


class TestValidationSaysWhatWillHappen:
    """Each message names the CONSEQUENCE, not just the rule.

    "expected one of [...]" tells a model what is wrong and nothing about why
    the canvas is black, so it has no way to prioritise the fix.
    """

    def test_an_empty_scene_says_it_renders_empty(self):
        out = validate_scene_document(scene(objects=[]))
        assert any("renders empty" in p for p in out)

    def test_all_objects_hidden_says_it_renders_empty(self):
        s = scene()
        s["objects"][0]["visible"] = False
        assert any("renders empty" in p for p in validate_scene_document(s))

    def test_an_unlit_standard_material_says_it_renders_black(self):
        s = scene(lights=[], environment={})
        out = validate_scene_document(s)
        assert any("BLACK" in p for p in out)

    def test_a_points_object_needs_no_light(self):
        # Points and shader-painted objects light themselves. Warning about
        # them would train the agent to ignore the warning that matters.
        s = scene(lights=[], environment={})
        s["objects"][0]["source"] = {"kind": "points", "count": 100}
        assert not any("BLACK" in p for p in validate_scene_document(s))

    def test_a_gltf_with_no_url_says_nothing_loads(self):
        s = scene()
        s["objects"][0]["source"] = {"kind": "gltf", "url": ""}
        assert any("nothing loads" in p for p in validate_scene_document(s))

    def test_an_object_with_no_id_explains_why_that_matters(self):
        s = scene()
        del s["objects"][0]["id"]
        out = " ".join(validate_scene_document(s))
        assert "BY id" in out or "by id" in out

    def test_duplicate_object_ids_are_caught(self):
        s = scene()
        s["objects"].append(dict(s["objects"][0]))
        assert any("share the id" in p for p in validate_scene_document(s))

    def test_a_shader_that_never_writes_a_colour_is_caught(self):
        s = scene(shaders=[{"id": "x", "fragment": "void main() { float a = 1.0; }"}])
        s["objects"][0]["material"] = {"shaderId": "x"}
        assert any("gl_FragColor" in p for p in validate_scene_document(s))

    def test_an_empty_shader_source_is_caught(self):
        s = scene(shaders=[{"id": "x", "fragment": "   "}])
        assert any("cannot compile" in p for p in validate_scene_document(s))

    def test_a_material_naming_a_missing_shader_is_caught(self):
        s = scene()
        s["objects"][0]["material"] = {"shaderId": "ghost"}
        assert any("does not define" in p for p in validate_scene_document(s))

    def test_a_track_with_one_key_animates_nothing(self):
        s = scene(timeline={"driver": "scroll", "tracks": [
            {"target": "objects.hero.rotation.y", "keys": [{"t": 0, "v": 0}]}
        ]})
        assert any("animates nothing" in p for p in validate_scene_document(s))

    def test_keys_out_of_order_are_caught(self):
        # They are sampled in order and assumed to ascend, so part of the range
        # interpolates BACKWARDS, which reads as the easing being wrong.
        s = scene(timeline={"driver": "scroll", "tracks": [
            {"target": "objects.hero.rotation.y",
             "keys": [{"t": 0.9, "v": 0}, {"t": 0.2, "v": 90}]}
        ]})
        assert any("BACKWARDS" in p for p in validate_scene_document(s))

    def test_an_unaddressable_track_target_is_caught(self):
        s = scene(timeline={"driver": "scroll", "tracks": [
            {"target": "lights.key.intensity", "keys": [{"t": 0, "v": 0}, {"t": 1, "v": 2}]}
        ]})
        assert any("dropped in silence" in p for p in validate_scene_document(s))

    def test_a_track_pointing_at_a_missing_object_is_caught(self):
        s = scene(timeline={"driver": "scroll", "tracks": [
            {"target": "objects.ghost.rotation.y", "keys": [{"t": 0, "v": 0}, {"t": 1, "v": 1}]}
        ]})
        assert any("no object for" in p for p in validate_scene_document(s))

    def test_an_interaction_with_no_event_is_caught(self):
        s = scene(interactions=[{"on": "click", "targetId": "hero", "event": ""}])
        assert any("does nothing" in p for p in validate_scene_document(s))

    def test_a_clean_scene_reports_nothing(self):
        assert validate_scene_document(scene()) == []

    def test_a_non_object_does_not_raise(self):
        # This reaches straight from a tool parameter, so it takes whatever the
        # model sent. Throwing here would lose the useful message.
        assert validate_scene_document("nonsense")
        assert validate_scene_document(None)
        assert validate_scene_document([1, 2, 3])


class TestPatchMerging:
    def test_an_object_merges_by_id_rather_than_replacing(self):
        # The whole reason patch_scene exists: changing one material colour
        # must not mean round-tripping kilobytes of GLSL through a capped
        # tool result.
        out, _ = _apply_scene_patch(
            scene(), {"objects": [{"id": "hero", "material": {"color": "#ff0000"}}]}
        )
        assert out["objects"][0]["material"]["color"] == "#ff0000"
        # Everything else about the object survives.
        assert out["objects"][0]["source"]["shape"] == "box"
        assert out["objects"][0]["name"] == "Hero"

    def test_an_unknown_id_is_added_rather_than_dropped(self):
        out, notes = _apply_scene_patch(
            scene(), {"objects": [{"id": "extra", "source": {"kind": "quad"}}]}
        )
        assert [o["id"] for o in out["objects"]] == ["hero", "extra"]
        assert any("Added object 'extra'" in n for n in notes)

    def test_an_entry_with_no_id_is_reported_not_silently_dropped(self):
        out, notes = _apply_scene_patch(scene(), {"objects": [{"source": {"kind": "quad"}}]})
        assert len(out["objects"]) == 1
        assert any("no id" in n for n in notes)

    def test_camera_merges_key_by_key(self):
        out, _ = _apply_scene_patch(scene(), {"camera": {"fov": 30}})
        assert out["camera"]["fov"] == 30
        assert out["camera"]["type"] == "perspective"

    def test_the_original_is_never_mutated(self):
        original = scene()
        snapshot = json.dumps(original)
        _apply_scene_patch(original, {"objects": [{"id": "hero", "name": "X"}]})
        assert json.dumps(original) == snapshot


class TestCompileLogAnnotation:
    def test_line_numbers_point_at_the_authors_source(self):
        # The driver counts from the top of what IT was handed, which includes
        # the runtime preamble. Reporting those raw sends the agent editing a
        # line it never wrote.
        src = "void main() {\n  gl_FragColor = nope;\n}"
        log = "ERROR: 0:4: 'nope' : undeclared identifier"
        out = _annotate_compile_log(log, src, preamble_lines=2)
        assert "line 2" in out
        assert "gl_FragColor = nope;" in out

    def test_a_line_inside_the_preamble_says_so(self):
        out = _annotate_compile_log("ERROR: 0:1: bad", "void main(){}", preamble_lines=5)
        assert "runtime preamble" in out

    def test_an_unparseable_line_passes_through(self):
        assert "something odd" in _annotate_compile_log("something odd", "x", 0)


class TestFrameComparison:
    def test_identical_frames_report_no_movement(self):
        a = {"raw": [(10, 20, 30)] * 100}
        assert _frames_differ(a, {"raw": [(10, 20, 30)] * 100}) == 0.0

    def test_a_changed_frame_reports_movement(self):
        a = {"raw": [(0, 0, 0)] * 100}
        b = {"raw": [(255, 255, 255)] * 100}
        assert _frames_differ(a, b) == 1.0

    def test_compression_noise_does_not_count_as_movement(self):
        # Without a threshold every screenshot pair "moves" and the check
        # passes on a completely static scene.
        a = {"raw": [(100, 100, 100)] * 100}
        b = {"raw": [(102, 101, 100)] * 100}
        assert _frames_differ(a, b) == 0.0

    def test_mismatched_frames_report_nothing_rather_than_guessing(self):
        assert _frames_differ({"raw": [(0, 0, 0)]}, {"raw": []}) == 0.0


class TestPaging:
    def test_the_whole_body_comes_back_when_no_cap_is_asked_for(self):
        body = "x" * 9000
        assert _paged(body, {}) == body

    def test_a_truncated_read_says_exactly_how_to_continue(self):
        # A silent truncation is worse than useless here: the agent would write
        # back a document missing its tail.
        out = _paged("x" * 9000, {"max_chars": 100})
        assert "offset=100" in out
        assert "of 9000" in out

    def test_the_last_chunk_carries_no_continuation_note(self):
        out = _paged("x" * 150, {"offset": 100, "max_chars": 100})
        assert "offset=" not in out

    def test_a_nonsense_offset_does_not_raise(self):
        assert _paged("abc", {"offset": "banana", "max_chars": "nope"}) == "abc"


class TestContractDrivenChecks:
    """These run only because the published contract is loaded.

    With no catalog the validator says so and does structural checks only,
    which is the honest behaviour but means these particular mistakes go
    unreported. That is the cost of not mirroring the types in Python, and it
    is the right trade: a mirror drifts silently, a missing catalog announces
    itself.
    """

    def test_an_unknown_source_kind_names_the_legal_ones(self):
        s = scene()
        s["objects"][0]["source"] = {"kind": "mesh"}
        out = " ".join(validate_scene_document(s))
        assert "'gltf'" in out or "gltf" in out
        assert "coerced to a default" in out

    def test_a_point_count_over_the_published_cap_is_caught(self, published_contract):
        cap = published_contract["limits"]["maxPointCount"]
        s = scene()
        s["objects"][0]["source"] = {"kind": "points", "count": cap * 100}
        out = " ".join(validate_scene_document(s))
        assert "clamp" in out
        # The consequence, not just the number: a large count slows the whole
        # page, which is the part an author would not guess.
        assert "WHOLE page" in out

    def test_a_count_under_the_cap_is_fine(self, published_contract):
        s = scene()
        s["objects"][0]["source"] = {"kind": "points", "count": 5000}
        assert not any("clamp" in p for p in validate_scene_document(s))

    def test_shadowing_a_runtime_uniform_is_caught(self, published_contract):
        # Declaring a document uniform named uTime freezes the live value, so
        # the effect stops responding while everything still "works".
        shared = published_contract["sharedUniforms"][0]
        s = scene(shaders=[{
            "id": "x",
            "fragment": f"uniform float {shared};\nvoid main() {{ gl_FragColor = vec4({shared}); }}",
            "uniforms": [{"name": shared, "type": "float", "value": 0}],
        }])
        out = " ".join(validate_scene_document(s))
        assert "SHADOWS" in out

    def test_a_uniform_declared_in_glsl_but_absent_from_the_scene_is_caught(self):
        s = scene(shaders=[{
            "id": "x",
            "fragment": "uniform float uWobble;\nvoid main() { gl_FragColor = vec4(uWobble); }",
            "uniforms": [],
        }])
        out = " ".join(validate_scene_document(s))
        assert "stays at zero" in out

    def test_the_runtime_uniforms_are_not_reported_as_missing(self, published_contract):
        # They are bound for every shader, so demanding a value for them would
        # be advice that makes the shader worse.
        decls = "\n".join(f"uniform float {u};" for u in published_contract["sharedUniforms"]
                          if not u.startswith("uPointer") and u != "uResolution")
        s = scene(shaders=[{
            "id": "x",
            "fragment": decls + "\nvoid main() { gl_FragColor = vec4(uTime); }",
            "uniforms": [],
        }])
        assert not any("stays at zero" in p for p in validate_scene_document(s))

    def test_an_unknown_easing_name_is_caught(self):
        s = scene(timeline={"driver": "scroll", "tracks": [
            {"target": "objects.hero.rotation.y", "ease": "bouncyWobble",
             "keys": [{"t": 0, "v": 0}, {"t": 1, "v": 90}]}
        ]})
        assert any("bouncyWobble" in p for p in validate_scene_document(s))

    def test_an_unknown_camera_type_is_caught(self):
        s = scene(camera={"type": "isometric"})
        assert any("isometric" in p for p in validate_scene_document(s))


class TestSSRFGuard:
    """The one non-negotiable guard on `fetch_external_asset`.

    "Any public URL" is the capability asked for. A server that fetches
    whatever a model hands it can otherwise be pointed at its own network, and
    169.254.169.254 in particular hands out instance credentials to anyone who
    can make the server request it.
    """

    @pytest.mark.parametrize("host,why", [
        ("127.0.0.1", "loopback"),
        ("localhost", "loopback by name"),
        ("::1", "loopback over IPv6"),
        ("169.254.169.254", "the cloud metadata endpoint"),
        ("10.0.0.1", "private 10/8"),
        ("192.168.1.1", "private 192.168/16"),
        ("172.16.0.1", "private 172.16/12"),
        ("0.0.0.0", "unspecified"),
    ])
    def test_internal_addresses_are_refused(self, host, why):
        from app.agents.appbuilder.tools.modlix import _safe_fetch as sf
        with pytest.raises(sf.BlockedURL):
            sf.resolve_and_check(host, 80)

    def test_an_ipv4_mapped_loopback_is_refused(self):
        # ::ffff:127.0.0.1 passes is_loopback/is_private on the IPv6 object
        # while still being loopback. Checking only the outer form lets it
        # straight through.
        from app.agents.appbuilder.tools.modlix import _safe_fetch as sf
        with pytest.raises(sf.BlockedURL):
            sf.check_address("::ffff:127.0.0.1", context="test")

    def test_a_public_address_is_allowed(self):
        from app.agents.appbuilder.tools.modlix import _safe_fetch as sf
        sf.check_address("93.184.215.14", context="test")  # does not raise

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scheme", ["file", "ftp", "gopher", "data"])
    async def test_non_http_schemes_are_refused(self, scheme):
        # file:// is a way to read the server's own disk.
        from app.agents.appbuilder.tools.modlix import _safe_fetch as sf
        with pytest.raises(sf.BlockedURL):
            await sf.fetch_public_url(f"{scheme}:///etc/passwd")


class TestAssetFormatValidation:
    """The HEADER is checked, not the extension.

    A `.glb` that is really an HTML login page loads as nothing, and the
    author's only clue is an empty canvas.
    """

    def test_a_real_glb_header_is_accepted(self):
        from app.agents.appbuilder.tools.modlix._safe_fetch import looks_like_gltf
        import struct
        ok, detail = looks_like_gltf(b"glTF" + struct.pack("<III", 2, 100, 20))
        assert ok and "2.0" in detail

    def test_glTF_version_1_is_refused_with_the_reason(self):
        from app.agents.appbuilder.tools.modlix._safe_fetch import looks_like_gltf
        import struct
        ok, detail = looks_like_gltf(b"glTF" + struct.pack("<III", 1, 100, 20))
        assert not ok and "version 1" in detail

    def test_an_html_page_says_so_rather_than_just_failing(self):
        from app.agents.appbuilder.tools.modlix._safe_fetch import looks_like_gltf
        ok, detail = looks_like_gltf(b"<!DOCTYPE html><html><body>Sign in</body></html>")
        assert not ok
        assert "HTML page" in detail and "login" in detail

    def test_a_gltf_json_file_is_accepted(self):
        from app.agents.appbuilder.tools.modlix._safe_fetch import looks_like_gltf
        ok, _ = looks_like_gltf(b'{"asset": {"version": "2.0"}, "scenes": []}')
        assert ok

    def test_an_hdr_header_is_accepted(self):
        from app.agents.appbuilder.tools.modlix._safe_fetch import looks_like_hdr
        assert looks_like_hdr(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n")[0]

    def test_a_png_is_not_mistaken_for_a_model(self):
        from app.agents.appbuilder.tools.modlix._safe_fetch import looks_like_gltf, looks_like_image
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
        assert looks_like_image(png)[0]
        assert not looks_like_gltf(png)[0]

    def test_a_truncated_file_does_not_raise(self):
        from app.agents.appbuilder.tools.modlix._safe_fetch import looks_like_gltf
        assert looks_like_gltf(b"gl")[0] is False


class TestShippedSamples:
    """The sample scenes in the pattern docs must validate.

    `pattern_read` surfaces sibling .json files automatically, so these are
    copied verbatim by the agent. A broken example is worse than no example:
    it teaches the mistake and it teaches it confidently.
    """

    SAMPLES = [
        "shader-background/sample-aurora-scene.json",
        "add-3d-scene/sample-model-scene.json",
        "scroll-scene/sample-scroll-scene.json",
    ]

    @pytest.mark.parametrize("rel", SAMPLES)
    def test_the_sample_validates_clean(self, rel):
        import os
        base = os.path.join(
            os.path.dirname(__file__), "..",
            "app/agents/appbuilder/aicontext/patterns",
        )
        path = os.path.join(base, rel)
        assert os.path.exists(path), f"{rel} is referenced by a pattern but missing"
        with open(path) as fh:
            doc = json.load(fh)
        assert validate_scene_document(doc) == []

    @pytest.mark.parametrize("rel", SAMPLES)
    def test_the_sample_is_a_real_scene_not_a_stub(self, rel):
        # Generated from the real preset registry rather than hand-written, so
        # it cannot drift from what the platform actually builds.
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..",
            "app/agents/appbuilder/aicontext/patterns", rel,
        )
        with open(path) as fh:
            doc = json.load(fh)
        assert doc.get("objects"), "a sample with no objects teaches an empty scene"
        assert doc.get("version"), "a sample with no version teaches an unversioned document"


class TestBlankDetectionOnRealShapes:
    """The measurement has to survive a SPARSE scene.

    The first version downsampled to 64x64 before counting colours. Resizing
    averages, and a particle field -- thousands of 1px motes on a dark
    background -- averages straight back into its background: a working field
    measured 2 distinct colours and would have been reported as a flat fill.
    That is the exact outcome this tool exists to prevent, arriving by a route
    nobody would think to test for.
    """

    @staticmethod
    def _png(pixels, size):
        import io
        from PIL import Image
        im = Image.new("RGB", size)
        im.putdata(pixels)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_a_flat_fill_is_detected_as_flat(self):
        from app.agents.appbuilder.tools.modlix.scenes import _image_stats
        png = self._png([(36, 26, 16)] * (200 * 200), (200, 200))
        s = _image_stats(png)
        assert s["distinctColours"] == 1
        assert s["spread"] == 0

    def test_a_sparse_particle_field_is_NOT_detected_as_flat(self):
        from app.agents.appbuilder.tools.modlix.scenes import _image_stats
        # 200x200 dark, with ~0.5% bright motes: the shape that broke it.
        pixels = []
        for i in range(200 * 200):
            pixels.append((120, 160, 255) if i % 211 == 0 else (8, 10, 24))
        s = _image_stats(self._png(pixels, (200, 200)))
        assert s["distinctColours"] > 1, "a sparse field must not read as a flat fill"
        # Its MEAN is low, so a mean-only brightness check would also fail it.
        # The spread is what says something is drawn.
        assert sum(s["mean"]) < 60
        assert s["spread"] > 40

    def test_a_gradient_is_not_flat(self):
        from app.agents.appbuilder.tools.modlix.scenes import _image_stats
        pixels = [(x, x // 2, 255 - x) for _ in range(200) for x in range(200)]
        s = _image_stats(self._png(pixels, (200, 200)))
        assert s["distinctColours"] > 100

    def test_a_truly_black_frame_reads_as_nothing_drawn(self):
        from app.agents.appbuilder.tools.modlix.scenes import _image_stats
        s = _image_stats(self._png([(0, 0, 0)] * (100 * 100), (100, 100)))
        assert sum(s["mean"]) <= 12 and s["spread"] <= 40

    def test_a_corrupt_image_returns_none_rather_than_raising(self):
        from app.agents.appbuilder.tools.modlix.scenes import _image_stats
        assert _image_stats(b"not a png") is None
