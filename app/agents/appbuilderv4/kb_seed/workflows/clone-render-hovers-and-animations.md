# Workflow: clone-render-hovers-and-animations

**Goal:** After `extract_site_assets` returns, render the hover-revealed UI
and the animation catalog as LIVE Modlix UI — not skipped, not just described.
Without this, the cloned page has dead nav (hover triggers that do nothing)
and frozen content (no motion on scroll, no transitions on hover).

**Touches services:** ui (pages, styles), core (page-event Kirun functions)

## Inputs

You're reading from the manifest returned by `extract_site_assets`:

```python
manifest['viewports'][<width>]['hovers']      # list of hover entries
manifest['viewports'][<width>]['animations']  # list of animation entries
```

Each hover entry has:
```json
{
  "label": "nav_product",
  "trigger_text": "Product",
  "trigger_selector": "header > nav > button:nth-of-type(2)",
  "hidden_child_selector": "header > nav > div:nth-of-type(3)",
  "handle": "linear-app__root:hover_nav_product_w1440",
  "revealed_text": "Issues · Projects · Cycles · Roadmaps",
  "revealed_items": [
    {"text": "Issues", "href": "/features/issues"},
    {"text": "Projects", "href": "/features/projects"},
    ...
  ],
  "position_hint": "below"
}
```

Each animation entry has:
```json
{
  "selector": ".hero h1",
  "kind": "animation" | "transition",
  "name": "fadeUp" | "transform" | ...,
  "duration": "600ms",
  "easing": "ease-out",
  "delay": "0ms",
  "iterations": "1",
  "keyframes_css": "@keyframes fadeUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: none; } }",
  "trigger_guess": "load" | "scroll" | "hover"
}
```

---

## Hover render — Pattern A: Modlix Popover

**Use when** the hover content is small: a tooltip, an info card, ≤3 items.

```python
import modlix

popover_key = modlix.uuid()
trigger_key = modlix.uuid()
content_key = modlix.uuid()

popover_def = {
    'key': popover_key,
    'name': 'NavProductPopover',
    'type': 'Popover',
    'properties': {
        'position': {'value': hover_entry['position_hint']},  # 'below'|'right'|'overlay'
        'trigger': {'value': 'hover'},
        'showOnHover': {'value': True},
    },
    'children': {trigger_key: True, content_key: True},
}

# Trigger is the original visible element (the nav button).
trigger_def = {
    'key': trigger_key,
    'name': 'NavProductTrigger',
    'type': 'TextButton',  # or Label, depending on source
    'properties': {'label': {'value': hover_entry['trigger_text']}},
}

# Content is built from revealed_items.
content_children = {}
for item in hover_entry['revealed_items']:
    link_key = modlix.uuid()
    content_children[link_key] = True
    components[link_key] = {
        'key': link_key, 'type': 'Link', 'name': f'Link_{item["text"]}',
        'properties': {
            'label': {'value': item['text']},
            'linkPath': {'value': item['href']},
        },
    }

content_def = {
    'key': content_key, 'type': 'Grid', 'name': 'NavProductContent',
    'children': content_children,
}
```

---

## Hover render — Pattern B: Grid + Page.sectionHovered binding

**Use when** the hover content is a full dropdown menu — multiple sections,
headings, ≥4 items, or anything Popover's positioning can't host (mega-menu,
multi-column layouts, items with icons + descriptions).

### B.1 — State slot on the page

Page-level state for each hover trigger:
```python
page_def['properties'].setdefault('eventFunctions', {})
page_def['properties']['initialState'] = {
    # one slot per hover label from the manifest
    f'sectionHovered_{hover_entry["label"]}': {'value': False},
    # ...repeat per hover
}
```

### B.2 — Wire the onMouseEnter / onMouseLeave events on the trigger

Two tiny Kirun page-event functions per hover. Names match the convention
`hover_<label>_enter` / `hover_<label>_leave`.

Enter event (sets the slot to true):
```python
import modlix
# Compile from text — see kirun/dsl.md for full grammar.
enter_fn_text = f"""
event hover_{hover_entry['label']}_enter() {{
  System.SetStore(name = "Page.sectionHovered_{hover_entry['label']}", value = true)
}}
"""
modlix.post(f'/api/ui/pageEventFunctions',
            {'appCode': app_code, 'pageName': page_name,
             'functionName': f'hover_{hover_entry["label"]}_enter',
             'definition': enter_fn_text})
```

Leave event (300ms delay so the user can travel into the menu):
```python
leave_fn_text = f"""
event hover_{hover_entry['label']}_leave() {{
  System.Delay(milliseconds = 300)
  System.SetStore(name = "Page.sectionHovered_{hover_entry['label']}", value = false)
}}
"""
modlix.post(f'/api/ui/pageEventFunctions',
            {'appCode': app_code, 'pageName': page_name,
             'functionName': f'hover_{hover_entry["label"]}_leave',
             'definition': leave_fn_text})
```

### B.3 — The trigger Grid wires the events

```python
trigger_def = {
    'key': trigger_key, 'type': 'Grid', 'name': f'HoverTrigger_{hover_entry["label"]}',
    'eventFunctions': {
        'onMouseEnter': {'value': f'hover_{hover_entry["label"]}_enter'},
        'onMouseLeave': {'value': f'hover_{hover_entry["label"]}_leave'},
    },
    'children': {label_key: True},  # the visible "Product" text
}
```

### B.4 — The hidden menu Grid binds visibility

```python
menu_def = {
    'key': menu_key, 'type': 'Grid', 'name': f'HoverMenu_{hover_entry["label"]}',
    'bindingPath': {
        'type': 'VALUE',
        'value': f'Page.sectionHovered_{hover_entry["label"]}',
    },
    # Modlix Grid hides itself when bindingPath resolves to a falsy value.
    'styleProperties': {
        modlix.uuid(): {
            'resolutions': {'ALL': {
                'position': {'value': 'absolute'},
                # Match position_hint:
                #   'below'   → top: 100%; left: 0
                #   'right'   → top: 0; left: 100%
                #   'overlay' → top: 0; left: 0
                'top': {'value': '100%' if hover_entry['position_hint'] == 'below' else '0'},
                'left': {'value': '100%' if hover_entry['position_hint'] == 'right' else '0'},
                'zIndex': {'value': '100'},
            }}
        }
    },
    'children': {item_key_1: True, item_key_2: True, ...},  # built from revealed_items
}
```

The menu is PRE-BUILT with the items from `revealed_items`. It's not
"hover-discovery" — it's always there, just visually toggled.

### B.5 — Accessibility note

Keyboard nav (Tab into the trigger → Enter to toggle a focus-trapped menu) is
a separate concern. For v1, the hover-only menu is acceptable; flag a TODO if
the source has visible focus-trap behavior.

---

## Animation render — Pattern C: Keyframe in a global style doc

Use for every `kind: 'animation'` entry. Collect ALL keyframes_css blocks
from all animations, paste into ONE global style doc.

```python
import modlix

# Collect every distinct keyframes_css from the manifest
all_animations = (
    manifest['viewports']['1440']['animations']
    + manifest['viewports']['768']['animations']
    + manifest['viewports']['375']['animations']
)
seen_names = set()
keyframes_blocks = []
for a in all_animations:
    if a['kind'] != 'animation' or not a.get('keyframes_css'):
        continue
    if a['name'] in seen_names:
        continue
    seen_names.add(a['name'])
    keyframes_blocks.append(a['keyframes_css'])

# Compose the style doc HTML
style_html = '<style>\n' + '\n'.join(keyframes_blocks) + '\n</style>'

# Create the global style doc once per session
modlix.post('/api/ui/styles', {
    'appCode': app_code,
    'name': 'clone_animations',
    'definition': style_html,
})
```

Apply via the component's styleProperties:

```python
# Direct animation property on a target component
target_def['styleProperties'][modlix.uuid()] = {
    'resolutions': {'ALL': {
        'animation': {'value': f"{a['name']} {a['duration']} {a['easing']}"
                              f" {a.get('delay','0s')} {a.get('iterations','1')}"
                              " both"},
    }}
}
```

---

## Animation render — Pattern D: Transition (direct styleProperty)

Use for every `kind: 'transition'` entry. NO global style doc needed —
goes straight on the target component.

```python
target_def['styleProperties'][modlix.uuid()] = {
    'resolutions': {'ALL': {
        'transition': {'value': f"{a['name']} {a['duration']} {a['easing']}"
                                f" {a.get('delay', '0s')}"},
    }}
}
```

For hover-triggered transitions, also add the hover-state values via a
pseudo-state styleProperty entry — or a separate styleProperties entry
on a hover-paired class (depends on the platform's hover-state convention).
When the source's `trigger_guess` is `hover`, also check whether the target
component has explicit hover state styles in the source.

---

## Animation render — Pattern E: Scroll-triggered (IntersectionObserver Kirun event)

Use for every animation with `trigger_guess: 'scroll'`. The keyframe sits
inactive until the element enters viewport.

### E.1 — Keyframe stays paused-at-start

Modify the keyframes_css block before pasting it into the style doc, OR add
a default styleProperty that keeps the element invisible until activated:

```python
# Resting state — apply to the target
target_def['styleProperties'][modlix.uuid()] = {
    'resolutions': {'ALL': {
        'opacity': {'value': '0'},
        'transform': {'value': 'translateY(20px)'},
        'transition': {'value': 'opacity 600ms ease, transform 600ms ease'},
    }}
}

# Active state — applied when class 'is-visible' is added
# (goes in the global style doc too)
style_html += '\n.is-visible { opacity: 1 !important; transform: none !important; }'
```

### E.2 — Page onLoad Kirun event wires IntersectionObserver

```python
onload_fn_text = """
event clone_setup_scroll_animations() {
  System.RunJS(code = '''
    if (window.__sa_setup) return;
    window.__sa_setup = true;
    const selectors = [
      // dump selectors here from manifest animations with trigger_guess='scroll'
      "<selector_1>", "<selector_2>", ...
    ];
    const obs = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) e.target.classList.add('is-visible');
      }
    }, {threshold: 0.1});
    for (const sel of selectors) {
      document.querySelectorAll(sel).forEach((el) => obs.observe(el));
    }
  ''')
}
"""
modlix.post('/api/ui/pageEventFunctions',
            {'appCode': app_code, 'pageName': page_name,
             'functionName': 'clone_setup_scroll_animations',
             'definition': onload_fn_text})

# Wire it on the page's onLoad event
page_def['properties']['eventFunctions']['onLoad'] = {
    'value': 'clone_setup_scroll_animations',
}
```

---

## Verification per pattern

| Pattern | What compare_to_source flags BEFORE the render | What it should NOT flag AFTER |
|---|---|---|
| A (Popover) | "missing dropdown menu", "nav button does nothing on hover" | Popover content matches `revealed_text` |
| B (Grid + binding) | Same as A, plus "menu items not visible" | Hidden menu Grid appears on hover in `screenshot_page` |
| C (Keyframe) | "no entrance animation", "hero text static" | Keyframe class present on target in DOM |
| D (Transition) | "card hover-state missing", "no fade on link" | Transition fires on hover (visible in `drive_page` + hover step) |
| E (Scroll-trigger) | "elements pop in", "no scroll reveal" | IntersectionObserver attached; `is-visible` added on scroll |

---

## Anti-patterns

- ❌ Building the hover menu only when the user hovers (no pre-built content).
  The menu is part of the page DOM from the start; visibility is the only
  thing that toggles.
- ❌ Adding `@keyframes` to a single component's `styleProperties` — Modlix
  rejects `@keyframes` inside styleProperties. They MUST live in a global
  style doc.
- ❌ Skipping scroll-triggered animations because "they're animations the user
  has to scroll to see anyway". Source visitors see them; clone visitors
  expect them. Wire them.
- ❌ Re-running `extract_site_assets` to "look at the hover again". The
  manifest already has every hover entry with the screenshot handle, the
  revealed_text, and the items. Reading the manifest is the answer.

---

## Related workflows

- `clone-with-real-assets-and-fonts`: foundation flow — harvest assets and
  fonts. THIS workflow picks up after that one returns.
- `replace-page-atomic`: how to PUT the composed page back atomically.
