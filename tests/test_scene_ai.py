"""The Scene Editor's AI pane.

Two things are worth pinning here and the happy path is neither of them.

The first is that the schema in the prompt comes out of the published catalog
and is never re-typed. A Python mirror of a TypeScript interface does not fail
loudly when it drifts -- the model writes a field that was renamed, the runtime
drops it in silence, and the scene comes back subtly wrong with nothing to read.

The second is what happens to a reply that is not quite what was asked for. This
endpoint sits behind a text box a person is typing into, so every failure has to
end with them knowing what happened. A dropped image, an unreadable file, a model
that returned the bare document instead of the envelope: each one has a defined
outcome, and none of them is silence.
"""

import base64
import json
import os

import pytest

from app.services import scene_ai


def _real_contract():
    path = os.path.expanduser(
        "~/kiran/fincity/nocode-ui/ui-app/client/dist/component-catalog.json"
    )
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return (json.load(fh) or {}).get("scenes") or {}


@pytest.fixture(autouse=True)
def published_contract(monkeypatch):
    contract = _real_contract()
    if not contract:
        pytest.skip("no generated component-catalog.json with a `scenes` block")
    monkeypatch.setattr(scene_ai, "_contract", lambda: contract)
    return contract


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ── the prompt is read from the catalog, not re-typed ────────────────────


class TestSchemaComesFromTheCatalog:
    def test_the_shape_in_the_prompt_is_the_published_shape(self, published_contract):
        text = scene_ai.build_system_prompt()
        shape = published_contract.get("shape") or {}
        assert shape, "the catalog published no shape; the rest of this test proves nothing"
        for name, fields in shape.items():
            assert f"interface {name}" in text
            for field in fields:
                assert field in text

    def test_every_closed_list_reaches_the_prompt(self, published_contract):
        text = scene_ai.build_system_prompt()
        for name, values in (published_contract.get("enums") or {}).items():
            for v in values:
                assert v in text, f"{name} value {v!r} never reached the prompt"

    def test_the_point_cap_reaches_the_prompt(self, published_contract):
        # The number an agent writes past without one to check against. A typed
        # 20000000 looks like a legitimate request all the way to the GPU.
        cap = (published_contract.get("limits") or {}).get("maxPointCount")
        assert cap
        assert str(cap) in scene_ai.build_system_prompt()

    def test_shared_uniforms_are_named_and_the_trap_is_explained(self, published_contract):
        text = scene_ai.build_system_prompt()
        for u in published_contract.get("sharedUniforms") or []:
            assert u in text
        # Naming them is not enough. Declaring one in `uniforms` pins a frozen
        # value over the live one and the shader compiles, renders, and never
        # moves -- so the prompt has to say that, not just list the names.
        assert "uniforms" in text and "frozen" in text

    def test_a_catalog_with_no_scenes_block_says_so_instead_of_inventing_a_schema(self):
        text = scene_ai.build_system_prompt(contract={})
        assert "WARNING" in text
        assert "schema" in text


# ── attachments ──────────────────────────────────────────────────────────


class TestAttachments:
    def test_an_image_becomes_an_image_block(self):
        blocks, notes = scene_ai._attachment_blocks(
            [{"type": "image", "name": "ref.png", "mime_type": "image/png", "data": _b64(PNG)}]
        )
        assert notes == []
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["media_type"] == "image/png"

    def test_a_text_file_is_inlined_under_its_name(self):
        blocks, notes = scene_ai._attachment_blocks(
            [{"name": "palette.json", "mime_type": "application/json", "data": _b64(b'{"a":1}')}]
        )
        assert notes == []
        assert "palette.json" in blocks[0]["text"] and '{"a":1}' in blocks[0]["text"]

    def test_a_binary_asset_is_named_rather_than_inlined(self):
        # A .glb is a legitimate thing to attach and its bytes say nothing in a
        # prompt. Naming it lets the model ask for the URL instead of guessing.
        blocks, notes = scene_ai._attachment_blocks(
            [{"name": "chair.glb", "mime_type": "model/gltf-binary", "data": _b64(b"glTF\x02")}]
        )
        assert "chair.glb" in blocks[0]["text"]
        assert "not included" in blocks[0]["text"]

    def test_an_oversized_attachment_is_refused_out_loud(self):
        big = _b64(b"\x00" * (scene_ai.MAX_ATTACHMENT_BYTES + 1))
        blocks, notes = scene_ai._attachment_blocks(
            [{"name": "huge.png", "mime_type": "image/png", "data": big}]
        )
        # Silence here is the bad outcome: the user sees a scene that ignores
        # their reference and cannot tell whether it was read and disregarded.
        assert blocks == []
        assert any("huge.png" in n and "cap" in n for n in notes)

    def test_undecodable_data_is_refused_out_loud(self):
        blocks, notes = scene_ai._attachment_blocks(
            [{"name": "bad.png", "mime_type": "image/png", "data": "not base64!!"}]
        )
        assert blocks == []
        assert any("bad.png" in n for n in notes)

    def test_attachments_past_the_cap_are_dropped_and_counted(self):
        many = [
            {"name": f"{i}.png", "mime_type": "image/png", "data": _b64(PNG)}
            for i in range(scene_ai.MAX_ATTACHMENTS + 3)
        ]
        blocks, notes = scene_ai._attachment_blocks(many)
        assert len(blocks) == scene_ai.MAX_ATTACHMENTS
        assert any(str(len(many)) in n for n in notes)

    def test_a_long_text_file_is_truncated_visibly(self):
        blob = b"x" * (scene_ai.MAX_TEXT_ATTACHMENT_CHARS + 500)
        blocks, _ = scene_ai._attachment_blocks(
            [{"name": "big.txt", "mime_type": "text/plain", "data": _b64(blob)}]
        )
        assert "[truncated]" in blocks[0]["text"]


# ── parsing the reply ────────────────────────────────────────────────────


class TestReplyParsing:
    def test_code_fences_and_prose_are_tolerated(self):
        assert scene_ai._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert scene_ai._extract_json('Sure!\n{"a": 1}\nHope that helps.') == {"a": 1}
        assert scene_ai._extract_json("not json at all") is None
        assert scene_ai._extract_json("") is None

    def test_a_bare_document_is_accepted_when_the_envelope_is_missing(self):
        # The model did the work and got the wrapper wrong. Throwing the scene
        # away over that is a failure the user cannot act on: they did not
        # write the prompt and cannot fix it.
        doc = {"objects": [{"id": "a"}], "camera": {"type": "perspective"}}
        assert scene_ai._unwrap_scene(doc) is doc

    def test_a_reply_with_no_document_in_it_is_not_mistaken_for_one(self):
        assert scene_ai._unwrap_scene({"message": "I could not do that"}) is None
        assert scene_ai._unwrap_scene({"scene": "a string"}) is None


# ── end to end, with the provider stubbed ────────────────────────────────


class _Provider:
    def __init__(self, content, vision=True):
        self.content = content
        self.vision = vision
        self.seen = None

    def supports_vision(self):
        return self.vision

    async def create_completion(self, **kw):
        self.seen = kw
        return {"content": self.content}


@pytest.fixture
def stub(monkeypatch):
    def install(content, vision=True):
        p = _Provider(content, vision)
        monkeypatch.setattr(scene_ai, "get_llm_provider", lambda: p)
        return p

    return install


GOOD = {
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
    "environment": {},
    "timeline": {"driver": "time", "tracks": []},
    "interactions": [],
}


@pytest.mark.asyncio
async def test_a_good_reply_comes_back_as_a_scene(stub):
    stub(json.dumps({"scene": GOOD, "message": "Added a box."}))
    out = await scene_ai.generate_scene(prompt="a box", component_type="ScrollScene")
    assert out["scene"]["objects"][0]["id"] == "hero"
    assert out["message"] == "Added a box."


@pytest.mark.asyncio
async def test_validation_problems_come_back_WITH_the_scene_not_instead_of_it(stub):
    # Refusing to hand back a flawed scene leaves the user with nothing to
    # edit; handing one back silently leaves them with a scene that does not
    # do what they asked and nothing to read.
    empty = {**GOOD, "objects": []}
    stub(json.dumps({"scene": empty, "message": "Done."}))
    out = await scene_ai.generate_scene(prompt="empty it")
    assert out["scene"] is not None
    assert any("no objects" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_the_model_notes_reach_the_user(stub):
    stub(json.dumps({"scene": GOOD, "message": "Done.", "notes": ["You still need a model URL."]}))
    out = await scene_ai.generate_scene(prompt="a chair", component_type="ModelViewer")
    assert "You still need a model URL." in out["warnings"]


@pytest.mark.asyncio
async def test_a_non_json_reply_leaves_the_scene_alone_and_says_why(stub):
    stub("I'm afraid I can't do that.")
    out = await scene_ai.generate_scene(prompt="x", scene=GOOD)
    assert out["scene"] is None
    assert out["warnings"]


@pytest.mark.asyncio
async def test_images_are_dropped_LOUDLY_on_a_model_that_cannot_see(stub):
    p = stub(json.dumps({"scene": GOOD, "message": "Done."}), vision=False)
    out = await scene_ai.generate_scene(
        prompt="like this",
        attachments=[{"name": "r.png", "mime_type": "image/png", "data": _b64(PNG)}],
    )
    assert any("cannot read images" in w for w in out["warnings"])
    assert not any(b.get("type") == "image" for b in p.seen["messages"][0]["content"])


@pytest.mark.asyncio
async def test_the_current_scene_is_sent_so_an_unsaved_edit_can_be_revised(stub):
    p = stub(json.dumps({"scene": GOOD, "message": "Done."}))
    await scene_ai.generate_scene(prompt="warmer", scene=GOOD)
    sent = p.seen["messages"][0]["content"][0]["text"]
    assert "hero" in sent and "Change it" in sent


@pytest.mark.asyncio
async def test_each_component_type_is_briefed_differently(stub):
    # The same document renders in all four and a good one differs completely
    # between them. A model told only "a scene" writes the average.
    seen = set()
    for ct in ("ShaderBackground", "ParticleField", "ModelViewer", "ScrollScene"):
        p = stub(json.dumps({"scene": GOOD, "message": "ok"}))
        await scene_ai.generate_scene(prompt="x", component_type=ct)
        seen.add(p.seen["messages"][0]["content"][0]["text"].split("\n")[0])
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_an_empty_prompt_is_refused(stub):
    stub("{}")
    with pytest.raises(ValueError):
        await scene_ai.generate_scene(prompt="   ")
