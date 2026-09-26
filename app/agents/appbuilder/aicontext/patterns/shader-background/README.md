---
name: shader-background
description: Put a real WebGL shader behind a hero or a section using the ShaderBackground component. Use when a design calls for animated gradients, aurora, flowing colour or a living background — this is NOT something to approximate with CSS gradients.
---

# shader-background

`ShaderBackground` renders a fullscreen quad with a fragment shader. It is the
component to reach for whenever a source design shows a background that MOVES:
aurora ribbons, flowing colour, a gradient that breathes, a mesh that leans
toward the cursor.

## When to use

- "the hero has this animated gradient thing"
- an aurora / northern-lights backdrop
- colour that drifts or flows behind content
- a background that reacts to the pointer

## When NOT to use

- A static gradient. `background: linear-gradient(...)` on a Grid is cheaper,
  needs no WebGL context, and looks identical.
- A video loop. Use `Video` with a poster.
- Behind a whole page. One scene is a hero; four scenes on one page eat most of
  the browser's WebGL context budget.

## Start with a preset

`list_scene_presets(kind="background")`, then set the name. That is the whole
job for most designs, and it keeps the page definition to one string:

```
patch_component_props(
    page_name="home", component_key="hero",
    properties={"preset": "aurora", "colorA": "#0b1026", "colorB": "#2b5cff"},
)
```

`colorA` / `colorB` / `colorC` override the preset's colours. An unset one keeps
the preset's own, so you can change one without flattening the rest.

## Children render ON TOP

Anything you put inside a ShaderBackground sits over the shader — a headline, a
whole Grid, a form. That is the normal way to build a hero. Keep text readable
with the `overlay` sub-component's background rather than by dimming the shader:
dimming the shader loses the effect you chose it for.

## Writing your own shader

Set `preset: "custom"` and put GLSL in `fragmentShader`. Five uniforms are bound
for every shader:

| uniform | type | |
|---|---|---|
| `uTime` | float | seconds, multiplied by the component's `speed` |
| `uResolution` | vec2 | canvas size in pixels |
| `uPointer` | vec2 | pointer in NDC, -1..1 |
| `uPointerActive` | float | 0 before the pointer arrives and after it leaves, 1 while it is on |
| `uProgress` | float | 0..1 from the timeline |

**Their VALUES are bound; their DECLARATIONS are not added for you.** Declare
each one you use or the shader will not compile. And do not declare a scene
uniform with one of those names: yours shadows the live value with a frozen one,
so the effect silently stops responding.

**`uPointerActive` is not optional if your shader uses `uPointer`.** At rest
`uPointer` is parked far off-canvas so a shader that pushes AWAY from the pointer
has no hole in it before anyone touches it. A shader that pulls TOWARD the
pointer reads that parked value as a real position and sends its whole effect
off-screen — it renders flat until the pointer first arrives, with no error.
Gate on `uPointerActive`:

```glsl
vec2 nudge = uPointer * uPointerActive;
```

That is a real bug that shipped: `gradientMesh` rendered as a flat dark
rectangle until someone moved the mouse over it.

## Check it before you believe it

```
compile_shader(fragment=...)       # errors with YOUR line numbers
render_scene_check(page_name=...)  # does it actually draw anything
```

A shader that compiles cleanly and renders black is the failure that actually
happens. `compile_shader` passes it. Only `render_scene_check` catches it.

## What it costs

One WebGL context out of roughly six per page. three.js loads as a separate
chunk, so pages without a scene pay nothing. Renders a single frame under
`prefers-reduced-motion`, and stops rendering entirely when scrolled out of
view. Set `poster` for the case where WebGL is unavailable — without one the
component falls back to `fallbackColor` and, failing that, to nothing.
