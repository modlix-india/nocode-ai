---
name: add-scroll-animation
description: Wrap a Modlix component (or group of components) in an Animator so it animates when it enters the viewport. Use when the user asks for scroll-triggered effects like "fade up when this section appears", "slide in from the left", "bounce in", etc.
---

# add-scroll-animation

Use this skill when the user wants components to animate as they scroll into view. The platform's `Animator` component is an IntersectionObserver-driven wrapper — its children fire CSS keyframe animations when their visibility crosses a threshold.

## When to use

- "Animate the feature grid when it scrolls into view"
- "Make the testimonials fade up as they appear"
- "Add a bounce-in effect to the CTAs when they reach the viewport"
- "I want this section to slide in from the right when visible"

## When NOT to use

- Page-load animations (these fire once at mount, not on scroll) — use the global keyframes in `appbuilderstyle` (e.g. `hero-rise`) directly on the target component's `animationName` + `animationDelay` styleProperties.
- Hover / focus state animations — set the style directly on the component with `patch_component_styles(pseudo_state="hover")`.
- Continuous animation (e.g. an infinite spinner) — set `animation` styleProperties on the component directly, no Animator needed.

## How Animator works

`Animator` wraps a subtree. On `componentDidMount` it spawns an `IntersectionObserver` watching itself. When the wrapper crosses one of the configured visibility thresholds, the matching animation triggers on the children (via CSS `animation` shorthand applied to the inner `_childContainer`).

DOM shape:
```
.comp.compAnimator                  ← outer wrapper, this is what the observer watches
  └─ ._childContainer { animation: <name> <duration> <easing> <delay> ... }
      └─ <your children rendered here>
```

Because the animation is applied to a SINGLE inner container, every direct child animates as one block. To stagger child elements, wrap each in its own Animator with different `animationDelay`.

## Property shape (CRITICAL — multi-valued shape, NOT plain array)

`animation` is a **multi-valued property** using the ANIMATIONOBSERVER editor. The stored shape is:

```json
"properties": {
  "animation": {
    "<entryKey>": {
      "order": 0,
      "property": {
        "value": {
          "animationName":          {"value": "fadeInUp"},
          "animationDuration":      {"value": 800},
          "animationDelay":         {"value": 0},
          "animationTimingFunction":{"value": "cubic-bezier"},
          "timingFunctionExtra":    {"value": "0.16, 1, 0.3, 1"},
          "animationIterationCount":{"value": 1},
          "animationDirection":     {"value": "normal"},
          "animationFillMode":      {"value": "both"},
          "condition":              {"value": true},
          "observation":            {"value": "entering"},
          "enteringThreshold":      {"value": 0.15},
          "numOfObservations":      {"value": 1}
        }
      }
    }
  }
}
```

Key shape facts:
1. **No outer `{value: ...}` wrap** on the top-level `animation` slot — it's a `{<entryKey>: <entry>}` dict directly. Different from single-valued props.
2. Each entry has an `order` field (integer) for sorting + a `property.value` field containing the rule.
3. **Every sub-field of the rule is individually wrapped in `{value: ...}`**.
4. `entryKey` is the unique id — a random hex string is fine.

Trying to write `"animation": {"value": [rule, rule, ...]}` (the array-style shape) will crash the page render with `TypeError: Cannot read properties of undefined (reading 'value')`. The `make.ts` `makePropertiesObject` handler iterates `Object.entries(propertyValues[name])` for multi-valued props, expecting each entry to be `{order, property: {value: {...}}}`.

Field reference:

| Field | Purpose | Typical value |
|---|---|---|
| `key` | Unique id within this Animator's array | random hex string |
| `animationName` | CSS keyframe to run (see list below) | `fadeInUp`, `slideInLeft`, etc. |
| `animationDuration` | Milliseconds | 600–1000 for entrance feel |
| `animationDelay` | Milliseconds — wait before starting after trigger | 0 for immediate, 100–300 for staggered groups |
| `animationTimingFunction` | `ease` \| `ease-in` \| `ease-out` \| `ease-in-out` \| `linear` \| `cubic-bezier` \| `steps` | `cubic-bezier` for premium feel |
| `timingFunctionExtra` | Only when `cubic-bezier` or `steps` — the params inside the parens | `0.16, 1, 0.3, 1` (Out Expo) for entrance |
| `animationIterationCount` | 1, 2, `infinite` | 1 for entrances |
| `animationDirection` | `normal` \| `reverse` \| `alternate` \| `alternate-reverse` | `normal` for entrances |
| `animationFillMode` | `none` \| `forwards` \| `backwards` \| `both` | `both` so the element stays visible after |
| `condition` | Boolean — whether this rule is active | `true` |
| `observation` | `none` \| `entering` \| `exiting` | `entering` for scroll-in |
| `enteringThreshold` | 0.0–1.0 — fraction of element visible to trigger | 0.2 (fire when 20% visible) |
| `exitingThreshold` | 0.0–1.0 — only if `observation: exiting` | typically 0.1 |
| `numOfObservations` | 0 = unlimited, N = fire only N times | 1 for entrance-once |

## Available keyframes (animations.css)

**Entrance (use these for scroll-in):**
- Fade — `fadeIn`, `fadeInUp`, `fadeInDown`, `fadeInLeft`, `fadeInRight`, plus `*Big` variants for more distance
- Slide-like via fade-direction-big (e.g. `fadeInUpBig`) — common entrance default
- Bounce in — `bounceIn`, `bounceInUp`, `bounceInDown`, `bounceInLeft`, `bounceInRight`
- Zoom in — `zoomIn`, `zoomInUp`, `zoomInDown`, `zoomInLeft`, `zoomInRight`
- Flip in — `flipInX`, `flipInY`
- Rotate in — `rotateIn`, `rotateInUpLeft`, `rotateInUpRight`, `rotateInDownLeft`, `rotateInDownRight`
- Light speed — `lightSpeedIn`
- Roll in — `rollIn`

**Exit (use for scroll-out, paired with `observation: exiting`):**
- `fadeOut*`, `bounceOut*`, `zoomOut*`, `flipOut*`, `rotateOut*`, `lightSpeedOut`, `rollOut`, `hinge`

**Attention-grabbers (no in/out, just emphasize):**
- `bounce`, `flash`, `pulse`, `rubberBand`, `shake`, `swing`, `tada`, `wobble`

## How to apply

1. **Identify the target subtree.** Usually a Grid section: a feature row, a card row, a testimonial set. Avoid wrapping huge sections (the whole hero) — wrap the meaningful unit.

2. **Create an Animator as the new parent** of that subtree:
   ```
   add_component(
     page_name="homeTwo",
     parent_key="<original parent of target>",
     component_type="Animator",
     component_key="<target>Anim",
     name="<target>Anim",
     display_order=<same as target had>,
     properties={
       "animation": [ {... animation rule object ...} ]
     }
   )
   ```

3. **Move the target into the Animator** so it becomes the child:
   ```
   move_component(
     page_name="homeTwo",
     component_key="<target>",
     new_parent_key="<target>Anim",
     display_order=0
   )
   ```

4. **For multiple sibling sections that should animate independently**, repeat — one Animator per section, each with its own keyframe + delay. Don't share an Animator across siblings; the animation applies to its single `_childContainer`, so they'd animate together as a block.

## Recommended defaults for entrance animations

For consistent feel across the appbuilder app, use **fadeInUp · 800ms · Out Expo · fire-once at 15% visibility**. Match the hero page-load reveal so section-scroll-in feels of the same family.

For staggered children inside a row, use one Animator per child with varying `animationDelay`: 0, 80, 160, 240, 320ms across siblings.

## Three platform invariants (any one wrong will silently break the Animator)

**(0) componentDefinition outer key MUST equal the component's `key` field.**
This is a platform-wide invariant, but it's especially easy to miss when
writing components into the page JSON directly (as opposed to via
`add_component` which handles it for you). The CORRECT shape:

```json
"trustAnim": {
  "key": "trustAnim",     ← matches the outer key
  "name": "trustAnim",
  "type": "Animator",
  ...
}
```

NOT `"<random-uuid>": {key: "trustAnim", ...}`. If they mismatch, the
component renders at runtime (the platform tolerates it), BUT:
- The page editor's component tree breaks (DOM error referencing missing
  components)
- Parent `children: {<outerKey>: true}` references still work because they
  use the outer key, but anything walking `.key` (event handlers, bindings,
  reference resolvers) fails to find the component.

Whenever you write into `componentDefinition` directly, use the component
key as the dict key: `cd[componentKey] = {key: componentKey, ...}`.

## Two value-format gotchas (will silently break if you miss them)

1. **`animationName` enum values are PREFIXED WITH `_`.** The source file
   `nocode-ui/.../Animator/animations.css` declares keyframes without an
   underscore (`@keyframes fadeInUp`), but the build pipeline rewrites them
   to `@keyframes _fadeInUp` in `dist/css/App.css`. The enum values in
   `properties.ts` ANIMATIONS_LIST are likewise prefixed (`_fadeInUp`,
   `_bounceIn`, `_zoomInUp`, etc.). Use the PREFIXED form (`_fadeInUp`) in
   the `animationName.value`. If you use the un-prefixed name (`fadeInUp`),
   the browser can't match a keyframe and the animation silently does
   nothing.

2. **`animationIterationCount` is a STRING, not a number.** Its
   `SCHEMA_STRING_COMP_PROP` accepts values like `"1"`, `"2"`, `"infinite"`.
   Numeric `1` may render the editor unstable / produce stale-typed warnings.

## Helper (Python) for the wrapped shape

```python
import secrets

def make_animation_entry(animation_name="_fadeInUp", delay_ms=0, threshold=0.15):
    """Note: animation_name must include the leading underscore (_fadeInUp,
    not fadeInUp) — the platform's compiled keyframes are underscore-prefixed."""
    return {
        "order": 0,
        "property": {
            "value": {
                "animationName":          {"value": animation_name},
                "animationDuration":      {"value": 800},
                "animationDelay":         {"value": delay_ms},
                "animationTimingFunction":{"value": "cubic-bezier"},
                "timingFunctionExtra":    {"value": "0.16, 1, 0.3, 1"},
                "animationIterationCount":{"value": "1"},          # STRING
                "animationDirection":     {"value": "normal"},
                "animationFillMode":      {"value": "both"},
                "condition":              {"value": True},
                "observation":            {"value": "entering"},
                "enteringThreshold":      {"value": threshold},
                "numOfObservations":      {"value": 1},
            },
        },
    }

# Set on a new Animator component:
animator_properties = {
    "animation": {secrets.token_hex(8): make_animation_entry()},
}
```

Use this directly in `componentDefinition.<animatorUuid>.properties`. Don't wrap in an outer `{value: ...}` — multi-valued props don't have one.

## Validation

After adding, screenshot the page in normal scroll (not initial-load). Animator's IntersectionObserver doesn't fire at page-mount if the element is already visible above the fold — those need page-load animations instead.

## Pitfalls

- **Don't nest Animators that would double-animate.** The inner one fires when its parent intersects; the outer applies its own animation to the same subtree. Pick one level.
- **`numOfObservations: 0` keeps animating every scroll-in/out.** Distracting. Use `1` for entrance-once unless the user explicitly wants repeating.
- **`animationFillMode: 'both'` is important** — without `forwards`, the element snaps back to its hidden state after the animation ends.
- **The IntersectionObserver watches the Animator's outer div.** If you wrap a Grid that's `display: grid`, the wrapper itself takes the full grid cell — observation works correctly. If you wrap an inline element, observation may not work; wrap a block-level container instead.

## Reference

- Component source: `nocode-ui/ui-app/client/src/components/Animator/Animator.tsx`
- Keyframes: `nocode-ui/ui-app/client/src/components/Animator/animations.css`
- Props: `nocode-ui/ui-app/client/src/components/Animator/animatorProperties.ts` — uses `COMMON_COMPONENT_PROPERTIES.animation` (ANIMATIONOBSERVER editor).
