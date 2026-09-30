---
name: multi-valued properties
description: How multi-valued component properties are stored, and how the patch / update / add / bulk tools auto-wrap sugared input.
type: reference
---

# Multi-valued properties

Some component properties accept an ORDERED LIST of entries rather than a single value:

| Component | Property | Editor type |
|---|---|---|
| Animator (and any component) | `animation` | ANIMATIONOBSERVER |
| Most form inputs | `validation` | (validation editor) |
| Calendar | `disableDates`, `disableDays`, `weekEndDays` | multiValued |
| Chart | `dataSetColors`, `xAxisLabels`, ... (most data inputs) | multiValued |
| Iframe | `sandbox`, `allow` | multiValued |
| Tabs | `tabs` | multiValued |
| FileSelector / FileUpload | `fileCategory`, `restrictUploadType`, `validation` | multiValued |
| PhoneNumber | `countries`, `topCountries` | multiValued |
| Prompt | `quickActionLabels`, `quickActionIcons`, ... | multiValued |
| Stepper | `titles`, `icons`, `images` | multiValued |
| Table | `perPageNumbers`, `childrenKey`, `hasChildrenProperty` | multiValued |
| RangeSlider | `marks` | multiValued |
| SectionGrid | `sectionProperties` | multiValued |
| SSEventListener | `eventName` | multiValued |
| MarkdownEditor | `editType` | multiValued |
| TableDynamicColumn | `excludeColumns`, `columnsOrder`, `includeColumns`, `sortColumns` | multiValued |
| ProductAnalyticsWidget | `funnelSteps` | multiValued |
| ColorPicker | `validation` | multiValued |

(Full list: walk the catalog at https://cdn-dev.modlix.com/js/dist/component-catalog.json — any property with `"multiValued": true`.)

## Stored shape

A multi-valued property is stored as a dict-of-entries, NOT an array:

```json
"<propName>": {
  "<entryKey>": {
    "order": 0,
    "property": {
      "value": {
        "<subField1>": {"value": <raw>},
        "<subField2>": {"value": <raw>},
        ...
      }
    }
  },
  "<anotherEntryKey>": { "order": 1, ... }
}
```

Two non-obvious facts:

1. **The TOP-LEVEL slot has NO `{value: ...}` wrap** — unlike single-valued
   properties. The dict IS the slot value. If you wrap it as
   `"animation": {"value": [...]}`, the renderer crashes with
   `TypeError: Cannot read properties of undefined (reading 'value')` —
   see `make.ts`'s `makePropertiesObject`.

2. **Every sub-field of the rule is individually wrapped in `{value: ...}`** —
   `"animationName": {"value": "_fadeInUp"}`, not just `"animationName": "_fadeInUp"`.
   The platform reads sub-fields uniformly with `.value` indirection.

`<entryKey>` is any unique string (random hex is fine). `order` controls the
sort within this prop's entries.

## How the tools handle it

`patch_component_props`, `update_component_props`, `add_component`, and
`bulk_patch_component_props` all route through `conventions.wrap_props_catalog_aware`,
which detects multi-valued properties via this priority:

1. **Existing stored shape.** If the component already has a multi-valued dict
   stored for this property, treat the input as multi-valued. This is the
   strongest signal; the platform's own data shape wins.
2. **Catalog flag.** Properties marked `"multiValued": true` in the catalog
   (cdn-dev's `component-catalog.json`).
3. **`KNOWN_MULTI_VALUED_PROPS` fallback.** A small allow-list for COMMON
   properties that aren't enumerated per-component in the catalog
   (currently: `animation`, `animationObserver`, `validation`).

Once classified multi-valued, the tool accepts any of:

```python
# Ergonomic: list of rule dicts. One entry per item; auto-generated entry keys.
patch_component_props(properties={
    "animation": [
        {"animationName": "_fadeInUp", "animationDuration": 1200, ...},
        {"animationName": "_zoomIn",   "animationDuration": 800,  ...},
    ]
})

# Single rule as a dict (not wrapped in a list): one entry.
patch_component_props(properties={
    "animation": {"animationName": "_pulse", "animationDuration": 1500}
})

# Already in stored shape: passthrough.
patch_component_props(properties={
    "animation": {"abc123": {"order": 0, "property": {"value": {...}}}}
})
```

All three forms produce the same on-disk shape.

## When to use which tool

| Goal | Tool |
|---|---|
| Set animations on one Animator | `patch_component_props(component_key=..., properties={'animation': [{...}]})` |
| Bump `animationDuration` on every Animator on a page | `bulk_patch_component_props(filter={'type': 'Animator'}, properties={'animation': [{...}]})` |
| Add a validation rule to many TextBoxes | `bulk_patch_component_props(filter={'name_contains': 'email'}, properties={'validation': [{...}]})` |

Note that for multi-valued, a fresh write REPLACES the existing entries. To
APPEND, fetch the component's existing entries first (use `get_component`),
keep them, and pass the combined list.

## Pitfalls

- **Don't wrap the top-level dict in `{value: ...}`** — see point (1) above.
- **`animationName` must be underscore-prefixed** (`_fadeInUp`, `_bounceIn`) —
  see [[add-scroll-animation]] skill.
- **`animationIterationCount` is a string** (`"1"`, not `1`).
- **Corrected 2026-09-25.** This used to say the catalog omitted Animator's
  `animation` property. It does not, and did not: the entry carries
  `animation` with `multiValued: true`. What WAS missing is the shape of one
  entry, so the catalog said "multiValued" and stopped. The generator now emits
  `subProperties` on `animation` with all 18 fields, parsed out of
  `ANIMATION_PROPERTIES` rather than mirrored, so it cannot drift. Read them
  from the catalog instead of guessing.
- Animator is tier `common` as of the same date. At `specialized` it rendered
  as exactly one line and none of its schema reached the prompt.
- `animationName` now carries the real enum, every value underscore-prefixed
  (`_fadeInUp`). It is declared in the source as `enumValues: ANIMATIONS_LIST`,
  an identifier the catalog's AST parser cannot evaluate, so it is resolved
  specially; if it ever comes back as a free-text field, that resolution broke.
- The scroll-timeline fields (`timeline`, `axis`, `scroller`, `rangeStart`,
  `rangeEnd`) are part of the same entry. `timeline` defaults to `none`, which
  is the clock behaviour; `view` and `scroll` scrub the animation to scroll
  position instead.
