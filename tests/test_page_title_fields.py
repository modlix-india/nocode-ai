"""Three fields that all sound like "the title of this page".

  name             the identity, and the URL slug
  title            the display name, shown on the builder tab and in the tree
  properties.title the text in the browser tab

`update_page(title=...)` used to write the third one, so "change this page's
title" put the change somewhere the person asking was not looking: the workspace
tab, the object tree and the Title box all read the display name, and all three
went on showing the old value while the agent reported success. Verified against
a real workspace: the edit reached the draft and the form looked untouched.
"""

from __future__ import annotations

from app.agents.appbuilder.tools.modlix import _page_ops as p_ops
from app.agents.appbuilder.tools.modlix.pages import update_page_tool


def _mutate_page(page: dict, **params) -> dict:
    """Run update_page's mutation over a page document, without the network.

    The mutation is a closure inside the executor, so it is reached by calling
    the executor with a loader that hands back this document. Simpler than
    faking the HTTP client, and it tests the thing that decides the field.
    """
    import asyncio
    from unittest.mock import patch

    async def fake_load_save(name, context, prms, mutate, message):
        err = mutate(page)
        return (err is None), err

    with patch("app.agents.appbuilder.tools.modlix.pages._load_save", fake_load_save):
        asyncio.run(update_page_tool.execute({"name": "contactUs", **params}, {}))
    return page


def test_title_sets_the_display_name():
    page = {"name": "contactUs"}
    _mutate_page(page, title="Contact Us")
    assert page["title"] == "Contact Us"
    assert "title" not in page.get("properties", {}), "must not touch the browser tab"


def test_browser_title_sets_the_tab_text():
    page = {"name": "contactUs"}
    _mutate_page(page, browser_title="Contact Us - Acme")
    assert page["properties"]["title"]["name"] == {"value": "Contact Us - Acme"}
    assert "title" not in page or page.get("title") is None, "must not touch the display name"


def test_both_are_independent():
    page = {"name": "contactUs"}
    _mutate_page(page, title="Contact Us", browser_title="Acme | Contact")
    assert page["title"] == "Contact Us"
    assert page["properties"]["title"]["name"] == {"value": "Acme | Contact"}


def test_a_new_page_gets_a_readable_title_in_both_places():
    """On create they should agree: the alternative is `contactUs` in both."""
    skeleton = p_ops.new_page_skeleton("contactUs", "monkbars", "SYSTEM", title="Contact Us")
    assert skeleton["title"] == "Contact Us"
    assert skeleton["properties"]["title"]["name"] == {"value": "Contact Us"}


def test_a_new_page_without_a_title_carries_neither():
    skeleton = p_ops.new_page_skeleton("home", "monkbars", "SYSTEM")
    assert "title" not in skeleton
    assert skeleton["properties"] == {}


def test_the_tool_says_which_title_is_which():
    """The model picks by reading these, so each must claim its own field and
    disclaim the other. Naming only what a parameter DOES leaves the two
    plausible for the same request, which is how the wrong one got written."""
    params = {p.name: p.description for p in update_page_tool.parameters}

    assert "display name" in params["title"].lower()
    assert "not the browser" in params["title"].lower()

    assert "browser tab" in params["browser_title"].lower()
    assert "not the builder display name" in params["browser_title"].lower()

    # And the tool's own description has to draw the line, because a model that
    # reads descriptions before parameters should not have to guess.
    assert "two titles" in update_page_tool.description.lower()
