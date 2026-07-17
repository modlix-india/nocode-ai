"""Pydantic models for the deterministic page analyzer.

Kept Python 3.9-safe: Optional[...]/List[...]/Dict[...] (no `X | None`), since
this repo runs on 3.9 and pydantic v2 resolves field annotations at runtime.

These models grow across milestones (M1 populates a subset). The eventual
output (`analysis.json`) is a build-ready, Modlix-shaped component plan.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Rect(BaseModel):
    """Document-relative bounding box (px)."""

    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


class NodeBreakpoint(BaseModel):
    """How one stamped element renders at a single breakpoint."""

    breakpoint: str  # "desktop" | "tablet" | "mobile"
    visible: bool = False
    rect: Optional[Rect] = None


class NodeObservation(BaseModel):
    """M1: one stamped DOM element observed across breakpoints.

    `mxa_id` is the stable identity stamped on the live DOM once (at desktop)
    and read back at each breakpoint, so the same element is matched across
    widths without re-walking the tree.
    """

    mxa_id: str
    tag: str
    parent_mxa_id: Optional[str] = None
    breakpoints: List[NodeBreakpoint] = Field(default_factory=list)

    def visible_at(self, breakpoint: str) -> bool:
        for bp in self.breakpoints:
            if bp.breakpoint == breakpoint:
                return bp.visible
        return False


class ComponentNode(BaseModel):
    """A kept (meaningful) DOM element classified to a Modlix component.

    The tree is the pruned/collapsed structure: redundant no-paint wrapper divs
    are dropped and their children re-parent to the nearest kept ancestor.
    `styleProperties` (M2/M5) and `visibility` (M1/M5) are attached later.
    """

    mxa_id: str
    component_type: str  # Modlix type (Grid/Text/Button/Image/Link/...)
    tag: str
    name: Optional[str] = None
    text: Optional[str] = None
    href: Optional[str] = None
    src: Optional[str] = None
    alt: Optional[str] = None
    input_type: Optional[str] = None
    classes: List[str] = Field(default_factory=list)
    role_attr: Optional[str] = None
    recognized_as: Optional[str] = None  # heuristic hint (button-like/carousel/nav/...)
    svg_html: Optional[str] = None  # raw outerHTML for <svg> (markup isn't otherwise captured)
    style_properties: Dict[str, Any] = Field(default_factory=dict)
    visibility: Dict[str, bool] = Field(default_factory=dict)  # breakpoint -> visible
    children: List["ComponentNode"] = Field(default_factory=list)


class SectionAnalysis(BaseModel):
    """One top-level section/grid of the page."""

    index: int
    mxa_id: Optional[str] = None
    name: str
    role: str  # nav | hero | content | cta | footer
    rect: Optional[Rect] = None
    heading_text: str = ""
    screenshots: Dict[str, str] = Field(default_factory=dict)  # breakpoint -> rel path
    roots: List[ComponentNode] = Field(default_factory=list)


class BreakpointInfo(BaseModel):
    """Per-breakpoint page-level summary."""

    name: str
    width: int
    height: int
    observed_count: int = 0  # elements found stamped at this breakpoint
    visible_count: int = 0
    active_media: List[str] = Field(default_factory=list)


class PageAnalysis(BaseModel):
    """Top-level analyzer output. M1 fills url/breakpoints/observations."""

    url: str
    analyzed_at: str
    run_dir: Optional[str] = None
    stage: str = "m1"
    breakpoints: List[BreakpointInfo] = Field(default_factory=list)
    total_elements: int = 0
    observations: List[NodeObservation] = Field(default_factory=list)
    sections: List[SectionAnalysis] = Field(default_factory=list)
    full_tree: Optional["ComponentNode"] = None  # complete DOM tree for faithful render
    font_faces: List[str] = Field(default_factory=list)  # harvested @font-face cssText
    keyframes: List[str] = Field(default_factory=list)  # harvested @keyframes cssText (animations)
    root_custom_properties: Dict[str, str] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)


# Self-referential children + forward refs need an explicit rebuild on pydantic v2.
ComponentNode.model_rebuild()
PageAnalysis.model_rebuild()
