---
name: scroll-scene
description: Scrollytelling — a 3D scene scrubbed by scroll position, so it runs forwards as the visitor scrolls down and backwards as they scroll up. Works off a horizontal scroller too.
---

# scroll-scene

`ScrollScene` renders a 3D scene whose timeline position IS the scroll position.
The object turns exactly as far as the visitor has scrolled, and unwinds when
they scroll back.

## When to use

- a section that tells a story as it scrolls past
- an object that assembles, turns or rises with the page
- a camera that pushes in as a section arrives
- the same, driven by a CAROUSEL or a horizontal strip

## When NOT to use

- A one-shot entrance. That is the `Animator` with `observation`, and it costs
  no WebGL context.
- Motion on an ordinary DOM element. That is the `Animator` with `timeline`,
  which scrubs any existing keyframes animation to scroll. Reach for a 3D scene
  only when the thing moving is actually three-dimensional.

## The two knobs that matter

**`mode`** decides what progress MEANS:

- `view` — 0 as this component enters the viewport, 1 as it leaves. What a
  section reveal wants.
- `scroll` — 0 at the top of the scroller, 1 at the bottom. What a page-length
  progress indicator wants.

**`axis`** — `block` for the usual vertical scroll, `inline` for a HORIZONTAL
scroller. `inline` is the interesting one: a Carousel or a Grid with
`overflow-x` can drive the scene, and there is no CSS-only way to do that here.

`rangeStart` and `rangeEnd` trim the travel, so the scene can finish before the
section leaves.

## Pin it while it plays

A scene that scrolls off the screen while it animates is mostly invisible. The
scrollytelling arrangement is a sticky scene inside a tall section:

```
patch_component_styles(component_key="scene",
                       css_props={"position": "sticky", "top": "60px",
                                  "height": "520px"})
```

with `mode: "scroll"` and a tall spacer after it. The scene then holds still on
screen while the page scrolls past it, which is what "scrollytelling" actually
looks like.

## Publish the progress

`bindingPath` writes the 0..1 progress into page data, so other components can
react to the same scroll without each measuring it again:

```
patch_component_bindings(
    component_key="scene",
    binding_paths={"bindingPath": {"type": "VALUE", "value": "Page.progress"}},
)
```

A Text bound to `Page.progress` is also the quickest way to see whether the
scroll is reaching the component at all.

## The thing that wastes the most time

**A Modlix page scrolls inside `.comp.compPage`, not the document.**
`window.scrollTo(0, 800)` does nothing, every stop reads the same progress, and
it looks exactly like the scroll driver being broken. When driving a page to
check this, scroll the element that actually has `scrollHeight > clientHeight`.

`scroller: "nearest"` is the default and is right; `root` means the document and
almost never is.

## Checking it

```
render_scene_check(page_name="story", expect_motion=False)
```

`expect_motion=False` because nothing has scrolled — two identical frames are
CORRECT for a scroll-driven scene, and the default check would fail it.

To see the motion, `drive_page` with scroll actions and a screenshot at each
stop. Compare the same region of the canvas at each: the component sits at a
different y at every stop, so a full-page shot is not comparable frame to frame.

## Reduced motion

A visitor who asked for reduced motion gets ONE still frame and no scroll
subscription at all, taken at `reducedMotionProgress` (0.5 by default). Not the
start: t=0 is usually a scene that has not arrived yet — offscreen, or scaled to
nothing — so freezing there shows an empty box.
