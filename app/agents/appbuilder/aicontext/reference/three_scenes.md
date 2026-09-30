# three.js scenes on the Modlix platform

Platform-wide rules for the four WebGL components. Per-app choices ("we used a
particle field for this site's hero") belong in that app's lore, not here.

## The four components

| component | for | children |
|---|---|---|
| `ShaderBackground` | animated gradients, aurora, flowing colour | render ON TOP |
| `ParticleField` | motes, starfields, dust, orb clouds | render ON TOP |
| `ModelViewer` | a .glb the visitor can turn, with clickable parts | render ON TOP |
| `ScrollScene` | a 3D scene scrubbed by scroll position | render ON TOP |

All four accept children, which is how a headline sits over a hero.

## Preset first, document second

A component's `preset` is a nine-character string that the runtime rebuilds the
scene from on every render. That is the cheap, correct default.

Writing a `scene` document makes the page OWN the scene. From then on, changes
to the built-in preset cannot reach it — which is what you want once a scene has
been tuned, and a needless cost before that. A resolved document carrying GLSL
is several KB of page JSON, which is why `get_scene` is paged.

The two are mutually exclusive: writing a document clears the preset name in the
same change, so nothing has to guess which one is live.

## The five bound uniforms

`uTime`, `uResolution`, `uPointer`, `uPointerActive`, `uProgress`.

Their VALUES are bound for every shader. Their DECLARATIONS are not added for
you — declare each one you use or the shader will not compile. Do not declare a
scene uniform with one of these names: yours shadows the live value with a
frozen one, and the effect silently stops responding.

### uPointerActive is not decoration

At rest `uPointer` is parked far outside the -1..1 range. That is deliberate: a
shader that pushes AWAY from the pointer would otherwise start with a hole
bitten out of its centre before anyone touched it.

The cost is that a shader which pulls TOWARD the pointer reads the parked value
as a real position and sends its effect off-screen. `gradientMesh` shipped that
way and rendered as a flat dark rectangle until the mouse moved over it. Gate
any pointer-attracting maths on `uPointerActive`.

## Budgets

- **~6 WebGL contexts per page.** Each scene takes one. Four scenes on one page
  is most of the budget.
- **three.js is ~816KB, in async chunks.** A page with no scene downloads none
  of it. Measured: no initial chunk contains three.
- **Points cost a fragment pass each.** The runtime caps a field at 200,000, and
  a large count slows the WHOLE page, not just that component.

## Accessibility and performance, which are not optional

Every scene, without being asked:

- renders exactly ONE frame under `prefers-reduced-motion`
- stops rendering when scrolled out of view
- stops rendering in a hidden tab
- caps device pixel ratio (default 2)
- falls back to `poster`, then `fallbackColor`, where WebGL is unavailable or
  the context is lost — never a blank box

## A canvas has no height

It is an empty box: with no intrinsic content it collapses to zero unless
something says otherwise. Each component carries a default `min-height`, but a
page that sets an explicit height overrides it. If a scene is "not rendering",
measure the canvas before touching the shader.

## Verification, in order

1. `validate_scene` — structural, and reports the CONSEQUENCE of each problem.
2. `compile_shader` — real GLSL compiler, errors against your line numbers.
3. `render_scene_check` — the only one that catches a shader that compiles and
   renders black.

**gl.readPixels does not work on a live page.** The renderer has no
`preserveDrawingBuffer`, so the buffer is cleared before any read from outside
the frame callback can reach it. Measured against a working ModelViewer:
readPixels reported a uniformly black canvas while the model was plainly visible
in the screenshot. Anything that samples pixels must sample the SCREENSHOT.

## Two failures that are silent

- **A mistyped track target is dropped without error.** Only
  `objects.<id>.<path>`, `shaders.<id>.<uniform>` and `camera.<path>` can be
  addressed. Anything else leaves the track in the document, visible in the
  editor, animating nothing.
- **Renaming an object id orphans every track that addressed it.** The object
  simply stops animating. The Scene Editor rewrites them for you; a hand edit
  does not.

## Colours and the theme

All four scene components carry `colorScheme`, and on a scene it does something
different from everywhere else in the platform. On an ordinary component the
scheme is a CSS class the theme keys off. A canvas has no such surface — a class
on the container cannot reach a uniform inside a fragment shader — so the scheme
is read in JavaScript instead: it names a theme colour, and the scene shades
that one colour into a deep, a mid and a bright tone.

| value | reads |
|---|---|
| `_preset` | **nothing.** The scene keeps the colours its preset was tuned with. This is the default. |
| `_primary` … `_quinary` | theme Colour One … Colour Five, the same mapping Button and CheckBox use |

Where each component spends the palette:

- **ShaderBackground** — `uColorA` / `uColorB` / `uColorC`, deep to bright.
- **ParticleField** — `uColorA` / `uColorB`, the bright and deep ends, so a
  particle still has somewhere to shade between.
- **ScrollScene** — the object material colour, the lit mid tone.
- **ModelViewer** — the highlight a picked mesh takes, and nothing else. A
  loaded model arrives with its own materials and recolouring it from a theme
  would throw away what somebody exported.

Two rules worth holding on to:

- **An explicit colour always wins.** `colorA` set on the component beats the
  scheme. The scheme is a default, not an override.
- **`_preset` is the default on purpose.** Setting a scheme is a choice somebody
  makes; no existing scene changes appearance on its own.

If the theme colour cannot be parsed — a named colour such as `rebeccapurple`,
which the shader colour parser does not read — the scheme resolves to nothing
and the preset's colours stay. That is deliberate: substituting a guess would
turn an unreadable theme into an invisible scene.

## Building a scene from a description

`POST /api/ai/appbuilder/scene` takes `{prompt, scene, componentType,
attachments}` and returns `{scene, message, warnings}`. It backs the Scene
Editor's AI pane, where a person describes a scene and optionally pastes a
reference image, and it is stateless: the whole current document goes with the
request, so an unsaved scene can be revised without anything being written.

Two things about it are worth knowing before relying on the result:

- **It saves nothing.** The editor puts the returned document on its own undo
  stack. Nothing reaches the page until somebody presses Save.
- **`warnings` arrives alongside a successful scene, not instead of it.** It
  carries the same sentences `validate_scene` produces, plus anything the model
  flagged — an asset URL that still has to be supplied, an image that was too
  large to read. A scene can come back usable and still have something in it
  that only the person reading can resolve.
