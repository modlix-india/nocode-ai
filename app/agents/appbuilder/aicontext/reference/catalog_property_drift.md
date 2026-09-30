# Catalog vs reality: the wire name is not always the catalog key

`nocode-ui/ui-app/client/scripts/generate-component-catalog.ts` builds
`component-catalog.json`, which is what property validation checks against. It
drifted badly, and the fallout explains several workarounds that were recorded
as separate component bugs.

## What was wrong (fixed 2026-08-26)

Components pull shared properties by reference
(`COMMON_COMPONENT_PROPERTIES.layout`), and the generator resolved those against
a **hand-maintained copy** of the table rather than the real one in
`src/components/util/properties.ts`. The copy held 15 of the 27 keys components
actually reference, so the catalog silently omitted the rest and any consumer
validating against it rejected perfectly valid properties as unknown. Measured:
74 properties missing across 41 components.

A second drop in the same file: components also declare shared properties as a
spread with overrides, `{ ...COMMON_COMPONENT_PROPERTIES.linkPath, group: BASIC }`.
The extractor only handled the bare-reference form, so the spread form returned
null and the property vanished. That is how **Link lost `linkPath`**, its only
means of navigating anywhere, and how **`designType` went missing from 20+
components** including Button, Text, Dropdown and TextBox. Fixing both recovered
114 properties.

Both are fixed. The generator now parses `COMMON_COMPONENT_PROPERTIES` out of
the source with the AST machinery it already used, and the literal remains only
as a fallback for when that file cannot be parsed.

## The rule that outlived the bug: key is not name

**The KEY in `COMMON_COMPONENT_PROPERTIES` is what components reference. The
inner `name` is what a page actually stores.** They differ.

- `linkTargetType` → the real entry is `name: 'target'`, and `Grid.tsx`
  destructures `target`. The published catalog was advertising a property the
  renderer never reads, so anyone setting `linkTargetType` on a Grid got
  silence. **The wire name is `target`.**
- `linkTargetFeatures` → `features`.

When a property "does nothing", check whether you are writing the key rather
than the name.

## Catalog precedence

`catalog.py` compares `generatedAt` and prefers whichever of the CDN copy and
the local copy is newer, so a freshly regenerated local catalog wins. Regenerate
with `npx tsx scripts/generate-component-catalog.ts` in `nocode-ui/ui-app/client`
(writes `dist/component-catalog.json`), then restart.

`COMPONENT_CATALOG_LOCAL_PATH` points at the local copy; empty auto-resolves a
sibling nocode-ui checkout.

## Table properties are still thin

The catalog remains incomplete for Table. A `PLATFORM_SAFE_PROPS` allow-list
softens validation for `tableDesign`, `perPageNumbers`, `pageSize`,
`defaultSize`, `selectionType`, `multiSelect`, `displayMode`, `previewMode`,
`showPagination`, `showPerPage`, `showSeperators`, `showArrows`, `tableLayout`,
`totalPages`, `uniqueKey`, `offlineData`, `showSpinner`, `spinnerType`,
`showPageSelectionDropdown`. If a Table property is in neither the catalog nor
the safe set, add it to the safe set rather than disabling validation.
