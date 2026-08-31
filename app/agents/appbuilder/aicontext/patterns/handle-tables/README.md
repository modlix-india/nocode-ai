---
name: handle-tables
description: Build, fix, or extend Modlix Tables — every render mode (COLUMNS / GRID / preview / empty), every layout (static columns / dynamic columns / row template / detail row / tree), and the bindingPath family that wires data, selection, pagination, sort, mode, and personalization. Use when adding a Table, when an existing Table renders empty or wrong, when selection / preview / pagination misbehaves, or when designing tree / hierarchical data UIs.
---

# handle-tables

Modlix Tables look simple but the actual render is governed by a small family of cooperating components (Table + TableColumns + TableColumn + TableGrid + TablePreviewGrid + TableRow + TableEmptyGrid) and **seven** binding paths. Most "Table is broken" reports trace back to one of: wrong child type for the chosen mode, missing `Parent.<field>` binding in cells, missing `uniqueKey`, or misuse of selection / pagination paths. This skill captures the full mental model so you don't have to re-derive it from the source each time.

## When to use this skill

- Adding a new Table to a page (decide COLUMNS vs GRID up front)
- An existing Table renders an empty body / blank cells / wrong row count
- Selection clicks don't populate `bindingPath2`, or preview pane never appears
- Pagination dropdowns or sort headers do nothing
- User wants a hierarchical / tree table or a per-row detail expander
- Mode-switcher icon (column/grid) doesn't show or one of the two icons is invisible
- Personalization (hide / reorder columns) needs to be wired up
- Diagnosing "why does my Table need a `TableColumns` AND a `TableGrid` child?"

## When NOT to use this skill

- One-off cell typography fixes — patch the cell `Text` directly
- Just changing `tableDesign` or `colorScheme` — that's a one-prop edit, no architecture needed
- Pure CSS overrides on `.comp.compTable` rows — that's a style doc task

## The mental model in 60 seconds

1. **`Table`** is the root and owns ALL state — data, page, mode, sort, selection, personalization. Children are passive renderers.
2. **`Table` does not draw cells** — it picks ONE of its child sub-trees to render based on `displayMode` and which children exist:
   - `TableColumns` child → COLUMNS mode (traditional `<table>` with header + rows)
   - `TableGrid` child → GRID mode (card layout, one card per row)
   - Both children → mode-switcher icons appear in the pagination footer
   - `TableEmptyGrid` child → shown instead when data array is empty
   - `TablePreviewGrid` child → shown when a row is selected (governed by `previewMode`)
3. **Cells access row data via `Parent.<field>`** — a row iteration pushes `(bindingPath)[i]` onto `locationHistory`, so a `Text` cell with binding `Parent.name` resolves to row N's `name` field. **No `Parent.<field>` = static text, won't vary per row.**
4. **`uniqueKey` is required** for selection, tree expand, and personalization to work. Default is `'id'`; set it explicitly if your data uses a different field.

## Component reference

### `Table` — root container

- **Parent**: anywhere (typically a Grid or a page root).
- **Children allowed**: `TableEmptyGrid` (0–1), `TableColumns` (0–1), `TableGrid` (0–1), `TablePreviewGrid` (0–1). You usually want at least `TableColumns` OR `TableGrid`. Use both if you want the user to switch modes.
- **Binding paths** (this is the part you have to internalize):
  - `bindingPath`  — **data array** (REQUIRED). Bind to e.g. `Page.users` or `SampleDataStore.table1`.
  - `bindingPath2` — **selection**. Shape governed by `selectionType` (`NONE` | `PATH` | `OBJECT`) and `multiSelect`.
  - `bindingPath3` — **current page number** (0-indexed).
  - `bindingPath4` — **rows per page**.
  - `bindingPath5` — **current mode** (`'COLUMNS'` | `'GRID'`). Set this if you want a code path to switch modes.
  - `bindingPath6` — **sort state**. Format controlled by `sortObjectType` (`spring` | `keyValue` | `stringWithColon` | `stringWithSpace`).
  - `bindingPath7` — **personalization state** (hidden columns / column order). Bind to per-user storage if you want it to persist.
- **Key properties**:
  - `tableDesign` (`_design1`..`_design9`, `_design0`) — row striping / shading. Default `_design1`. The numbered designs map to ten distinct stripe patterns.
  - `colorScheme` (`_primary`..`_quinary`) — drives header / selected-row hue.
  - `tableLayout` (`AUTO` | `FIXED`) — CSS `table-layout`.
  - `displayMode` (`COLUMNS` | `GRID`) — initial mode.
  - `previewMode` (`BOTH` | `COLUMNS` | `GRID`) — in which modes the preview pane appears.
  - `previewGridPosition` (`LEFT` | `RIGHT` | `TOP` | `BOTTOM`) — preview placement.
  - `offlineData` (bool) — `true` → paginate locally from the bound array; `false` → assume server already returns one page, use `totalPages`.
  - `selectionType` (`NONE` | `PATH` | `OBJECT`) — `PATH` stores `"(bindingPath)[i]"`; `OBJECT` stores a copy of the row.
  - `multiSelect` (bool).
  - `showPagination` / `showPerPage` / `showPageSelectionDropdown` (bool).
  - `defaultSize` (number, default 10) — rows per page.
  - `perPageNumbers` (multi-valued, e.g. `[5, 10, 25]`) — dropdown options. In appbuilder it's also accepted as a comma string `"5,10,15"`; non-numeric entries are filtered.
  - `paginationPosition` (`_LEFT` | `_RIGHT` | `_CENTER`) and `paginationDesign` (`_design1`..`_design4`).
  - `uniqueKey` (string, default `'id'`) — REQUIRED for selection, tree, personalization.
  - `showSpinner` (bool) + `spinnerType` (`_circleSpinner` | `_circleSpinner2` | `_circleSpinner3` | `_emptyRow`).
  - Mode-switcher icon overrides: `columnsModeIcon` / `gridModeIcon` (font-icon class) OR `columnsModeImage` / `gridModeImage` / their `*ActiveImage` siblings.
  - Pagination arrow overrides: `previousArrowIcon` / `nextArrowIcon` (font-icon class) OR `previousArrowImage` / `nextArrowImage`.
  - `enablePersonalization` (bool, default true), `disableColumnDragging`, `hideContextMenu`.
  - **Tree mode**: `treeMode` (bool), `childrenKey` (multi-valued, depth-aware — e.g. `['children', 'items']` uses `children` at depth 0 and `items` at depth 1+), `hasChildrenProperty` (multi-valued, used for lazy-load nodes), `defaultExpandLevel` (number, `-1` = all expanded, `0` = none), `showConnectors`, `indentSize`.
  - **Sorting**: `multiSort` (bool), `sortObjectType`, `ascValue` / `descValue`.
- **Events**: `onSelect`, `onPagination`, `onSort`, `onExpandEvent` (tree).
- **Style sub-components**: `comp`, `tableWithPagination`, `tableContainer`, `modesContainer`, `columnsModeIcon` / `selectedColumnsModeIcon` / `columnsModeImage`, `gridModeIcon` / `selectedGridModeIcon` / `gridModeImage`, `previousArrow` / `nextArrow` / `previousText` / `nextText`, `pageNumbers` / `selectedPageNumber` / `ellipsesGrid`, `itemsPerPageDropdown` / `perPageLabel`, `pageSelectionDropdown` / `pageSelectionLabel`.

### `TableColumns` — column-mode container

- **Parent**: `Table`.
- **Children allowed**: `TableColumn` (0–∞), `TableDynamicColumn` (declared but not implemented — see Gotchas), `TableRow` (0–1, detail row).
- **Key properties**:
  - `showHeaders` (bool, default true).
  - `fixedHeader` (bool) — sticky header on scroll.
  - `showEmptyRows` (bool, default true) — render placeholder rows when data is shorter than `defaultSize`.
  - `expandIcon` / `collapseIcon` — tree-mode chevron overrides (font-icon class).
- **Style sub-components**: `comp`, `row`, `header`, `selectedRow`, `rowContainer`, `headerContainer`, `treeExpandButton`, `treeCollapseButton`, `treeLines`, `treeCell`.
- **Notes**: Rows are rendered progressively (5 initial + 3-per-batch) to avoid freezing the browser on large datasets.

### `TableColumn` — one column

- **Parent**: `TableColumns`.
- **Children allowed**: any component (0–1). Typically a `Text` for plain cells, a `Grid` for compound cells, an `Image` for thumbnails, a `Button` for row actions, or a `TextBox` for editable cells.
- **Key properties**:
  - `label` (string) — header text.
  - `sortKey` (string) — field name sent in `onSort` (can differ from the bound field, e.g. computed columns).
  - `initialSortOrder` (`ASC` | `DESC`).
  - `sortAscendingIcon` / `sortDescendingIcon` / `sortNoneIcon` (font-icon class).
  - `leftIcon` / `rightIcon` / `leftIconTitle` / `rightIconTitle` — header icons + tooltips.
  - `tooltipPosition` (`_top` | `_bottom`).
  - `hideIfNotPersonalized` (bool) — hide when personalization marks this field hidden.
  - `disableColumnDragging` (bool).
- **The critical pattern**: the child component MUST bind to `Parent.<field>` to vary per row.
  ```json
  // TableColumn child = Text
  "properties": {
    "text": { "location": { "type": "VALUE", "value": "Parent.name" } },
    "textColor": { "value": "_primaryText" }
  }
  ```
  A raw `text.value: "Alice"` gives you the literal "Alice" on every row. This is the single most common bug.

### `TableGrid` — grid-mode container

- **Parent**: `Table`.
- **Children allowed**: any single component (0–1) — typically a `Grid` styled as a card.
- **Key properties**:
  - `layout` (string) — `grid-template-columns` value, e.g. `"repeat(3, 1fr)"`.
  - `showEmptyGrids` (bool, default false) — render placeholder cards when data is shorter than `defaultSize`.
- **Style sub-components**: `comp` (root grid), `eachGrid` (per-card slot).
- **Rendering**: the single child is cloned once per row in the current page. Inside the card, descendants use `Parent.<field>` exactly like COLUMNS-mode cells.

### `TableRow` — per-row detail/expansion row

- **Parent**: `TableColumns`.
- **Children allowed**: any (0–∞).
- **Key properties**: `rowPosition` (`ABOVE` | `BELOW`).
- **Render**: spans all columns via `colspan`. Always rendered if present; use a `visibility` binding on it (or on its content) to show only when its row is "expanded" by the user.

### `TablePreviewGrid` — selection detail pane

- **Parent**: `Table`.
- **Children allowed**: any (0–∞).
- **Render**: appears when `bindingPath2` is non-empty AND `Table.previewMode` includes the current displayMode. Position is controlled by `Table.previewGridPosition`.
- **Inside the preview**, `Parent.<field>` resolves to the SELECTED row, not iteration row.
- **Properties**: standard container props (`onClick`, `linkPath`, `target`, `layout`, `containerType`, `background`, `readOnly`).

### `TableEmptyGrid` — zero-state

- **Parent**: `Table`.
- **Children allowed**: any (0–∞).
- **Render**: shown in place of TableColumns/TableGrid when the bound data is empty. Not shown while loading (the spinner takes its place).
- **Use for**: a "no records found" message plus an optional CTA button.

### `TableColumnHeader` — internal

- Auto-rendered by `TableColumns` per column. Don't add it manually. It owns the sort icon state and the click handler that fires `Table.onSort`.

### `TableDynamicColumn` — NOT IMPLEMENTED

- Declared in the catalog with properties (`excludeColumns`, `includeColumns`, `columnsOrder`, `dontShowOtherColumns`, `enableSorting`, `sortColumns`, sort icons), but there is no actual React component. The dynamic-column slot inside `TableColumns` is also a no-op. **Don't use this** in a current page — fall back to static `TableColumn`s.

## Common recipes

### A. Plain COLUMNS-mode table with 4 columns

```
Table { bindingPath: SampleDataStore.users, uniqueKey: 'id', selectionType: 'OBJECT' }
└─ TableColumns {}
   ├─ TableColumn { label: 'Name' }
   │  └─ Text { text: Parent.name }
   ├─ TableColumn { label: 'Email' }
   │  └─ Text { text: Parent.email }
   ├─ TableColumn { label: 'Role' }
   │  └─ Text { text: Parent.role }
   └─ TableColumn { label: 'Created' }
      └─ Text { text: Parent.createdAt, luxonFormat: 'yyyy-LL-dd' }
```

Save → page renders 10 rows per page with the default `_design1` stripes.

### B. Switchable COLUMNS + GRID

```
Table { bindingPath: ..., displayMode: 'COLUMNS' }
├─ TableColumns { ... } // as above
└─ TableGrid { layout: 'repeat(auto-fill, minmax(220px, 1fr))' }
   └─ Grid { /* a card */
      ├─ Image { src: Parent.avatar }
      ├─ Text { text: Parent.name, textContainer: 'H4' }
      └─ Text { text: Parent.role, textColor: '_subText' }
   }
```

Mode-toggle icons appear in the pagination footer because BOTH children exist. The single-column-icon-only case happens when one child is missing.

### C. Detail row that expands on click

```
Table { bindingPath: ..., selectionType: 'OBJECT', uniqueKey: 'id' }
└─ TableColumns
   ├─ TableColumn { label: '' } { /* row chevron */ }
   │  └─ Button { designType: '_iconButton', onClick: togglePageDataExpanded }
   ├─ TableColumn ...  // normal columns
   └─ TableRow { rowPosition: 'BELOW' }
      └─ Grid { visibility: 'Page.expanded[Parent.id]' }
         └─ ... full detail layout ...
```

`TableRow` is rendered for every row but its child only paints when `visibility` returns true.

### D. Tree table (nested data)

```
Table {
  bindingPath: Page.folderTree,
  treeMode: true,
  uniqueKey: 'id',
  childrenKey: ['children'],
  hasChildrenProperty: ['hasChildren'],   // for lazy-load
  defaultExpandLevel: 1,
  indentSize: 24,
  onExpandEvent: loadChildrenFn,           // server fetches lazily
}
└─ TableColumns { expandIcon: 'fa fa-chevron-right', collapseIcon: 'fa fa-chevron-down' }
   ├─ TableColumn { label: 'Name' }  // tree chevron auto-injected in FIRST column
   │  └─ Text { text: Parent.name }
   └─ ...
```

Expand/collapse chevrons land in the FIRST `TableColumn`'s cell — don't try to add them yourself.

### E. Preview pane on selection

```
Table { selectionType: 'OBJECT', previewMode: 'BOTH', previewGridPosition: 'RIGHT' }
├─ TableColumns { ... }
└─ TablePreviewGrid {}
   └─ Grid {
      ├─ Text { text: Parent.name, textContainer: 'H3' }
      ├─ Text { text: Parent.email }
      └─ ... full detail ...
   }
```

Inside `TablePreviewGrid`, `Parent.<field>` is the SELECTED row, not an iteration row.

### F. Server-side pagination

```
Table {
  bindingPath: Page.currentPageData,     // ONE page of rows
  offlineData: false,
  totalPages: Page.totalPages,
  bindingPath3: Page.currentPageNumber,  // 0-indexed
  bindingPath4: Page.pageSize,
  onPagination: fetchPageFn,             // re-fetch on change
}
```

With `offlineData: false`, the Table trusts the caller; it won't slice anything itself.

### G. Multi-select with an actions toolbar

```
Table { selectionType: 'OBJECT', multiSelect: true, bindingPath2: Page.selected }
```

`Page.selected` becomes an array of row copies. Toolbar above the table reads `Page.selected.length` to decide enabled state.

## Validated style recipes

These are confirmed working via `patch_component_styles` against a live appbuilder Table. The `sub_component` slots `comp`, `row`, and `header` all accept these CSS props at `breakpoint: ALL`. Mix and match.

### Thin / compact rows

```python
patch_component_styles(component_key='tblX', sub_component='row',
  css_props={'paddingTop': '4px', 'paddingBottom': '4px', 'fontSize': '13px'})
patch_component_styles(component_key='tblX', sub_component='header',
  css_props={'paddingTop': '8px', 'paddingBottom': '8px', 'fontSize': '12px',
             'textTransform': 'uppercase', 'letterSpacing': '0.06em'})
```

Ultra-compact (data-grid feel): drop padding to `2px` and font to `12px`. Header `11px` with wider letter-spacing.

### Rounded card with shadow

```python
patch_component_styles(component_key='tblX', sub_component='comp',
  css_props={'borderRadius': '14px', 'overflow': 'hidden',
             'boxShadow': '0 10px 30px rgba(0,0,0,0.08)',
             'border': '1px solid rgba(0,0,0,0.06)'})
```

`overflow: hidden` is required so the inner header bar respects the radius.

### Outlined (scheme-colored border)

```python
patch_component_styles(component_key='tblX', sub_component='comp',
  css_props={'border': '1.5px solid #059669', 'borderRadius': '10px',
             'overflow': 'hidden'})  # match border to the colorScheme hue
```

### Minimal underline-only header

```python
patch_component_styles(component_key='tblX', sub_component='header',
  css_props={'background': 'transparent', 'color': '#475569',
             'borderBottom': '2px solid #475569'})
patch_component_styles(component_key='tblX', sub_component='row',
  css_props={'borderBottom': 'none'})
```

### Soft pill (large radius + tinted shadow)

```python
patch_component_styles(component_key='tblX', sub_component='comp',
  css_props={'borderRadius': '20px', 'overflow': 'hidden',
             'border': '1px solid rgba(59, 130, 246, 0.20)',
             'boxShadow': '0 2px 8px rgba(59, 130, 246, 0.08)'})
```

### Mode-toggle icon visibility (style doc, not per-Table)

The non-selected `_columns` / `_grid` icon is invisible by default because the platform's lazy-loaded Table CSS sets a near-transparent inline color on the inner `<svg>` via `getStyleObject`. Inline style on the SVG beats stylesheet rules unless overridden with `!important`. Put this in your app's style doc once:

```css
.comp.compTable ._modesContainer ._columns,
.comp.compTable ._modesContainer ._grid {
  opacity: 0.55 !important;
  color: #475569 !important;
  background: transparent;
  /* ...hover + selected variants per colorScheme... */
}
.comp.compTable ._modesContainer ._columns > svg,
.comp.compTable ._modesContainer ._grid > svg {
  width: 18px !important;
  height: 18px !important;
  fill: currentColor !important;
  color: currentColor !important;
  opacity: 1 !important;
}
.comp.compTable ._modesContainer ._columns > svg path,
.comp.compTable ._modesContainer ._grid > svg path,
.comp.compTable ._modesContainer ._columns > svg rect,
.comp.compTable ._modesContainer ._grid > svg rect {
  fill: currentColor !important;
}
```

Scheme-tinted selected state via parent `.comp.compTable._primary` / `._secondary` / etc.

## Gotchas

| Symptom | Root cause | Fix |
|---|---|---|
| All cells show the same literal text | Cell `Text.text.value` is a static string | Use `text: { location: { type: VALUE, value: 'Parent.<field>' } }` |
| Body is blank, header is fine | `bindingPath` resolves to undefined or a non-array | Verify the binding; in the editor, log it via an Animator's onLoad |
| Selection clicks do nothing | `selectionType: NONE` (default) | Set `selectionType: OBJECT` (or `PATH`) and bind `bindingPath2` |
| Selection clicks fire but preview pane never appears | No `TablePreviewGrid` child, OR `previewMode` excludes current displayMode | Add the child; align `previewMode` with `displayMode` |
| Mode-switcher icon row only shows one icon | Table only has one of `TableColumns`/`TableGrid` | Add the missing child; mode toggle requires both |
| Non-selected mode icon is invisible | Platform's lazy-loaded CSS sets a near-transparent color inline; your stylesheet needs `!important` to override | See the `_modesContainer ._columns / _grid` overrides in `appbuilderstyle` |
| Tree expand chevron not appearing | `treeMode: false`, or first column is hidden by personalization, or data doesn't have the `childrenKey` field populated | Verify in that order |
| Pagination dropdown lists "1, 2, 3" not your sizes | `perPageNumbers` not set, or all entries non-numeric | Set `perPageNumbers: [5, 10, 25]` (or `"5,10,25"` string) |
| Sort header click does nothing | No `onSort` event handler, or `sortKey` not set on the column | Wire `onSort`; set `sortKey` per column |
| Column personalization doesn't persist | `bindingPath7` not bound to durable storage | Bind to `Personalization.<storageKey>` |
| `TableDynamicColumn` does nothing | Not implemented in the platform | Use static `TableColumn`s |
| Empty state never appears | `TableEmptyGrid` missing as child, OR data is loading (spinner takes its place) | Add the child; verify loading isn't perpetual |
| Header label and cell text get out of sync after a column drag | Personalization keeps column order in `bindingPath7`; you re-rendered without it | Re-bind `bindingPath7` to durable storage |
| `_design1` looks like `_design2` | Many designs are subtle stripe-pattern variants; differences are most visible at 8+ rows | Increase `defaultSize` to 10+ when testing visually |

## Catalog vs reality

The CDN component catalog is often incomplete for Table properties. The `PLATFORM_SAFE_PROPS` allow-list in `app/agents/appbuilder/tools/_shared.py` softens validation for: `tableDesign`, `perPageNumbers`, `pageSize`, `defaultSize`, `selectionType`, `multiSelect`, `displayMode`, `previewMode`, `showPagination`, `showPerPage`, `showSeperators`, `showArrows`, `tableLayout`, `totalPages`, `uniqueKey`, `offlineData`, `showSpinner`, `spinnerType`, `showPageSelectionDropdown`. If a Table prop you want isn't in either the catalog OR the safe set, the tool will reject it — add to `PLATFORM_SAFE_PROPS` rather than disabling validation.

## Related skills / references

- [[build-themetest-page]] — diagnostic page for theme issues; doesn't help with Table-specific bugs
- [[clone-section]] — useful for cloning a working Table into a new tab/section
- [[reference-link-paths]] — how `linkPath` works on `TablePreviewGrid` / `TableEmptyGrid`
- [[reference-component-definition-invariants]] — full page-definition shape (componentDefinition map, children arrays, displayOrder)
- [[reference-design-system]] — colorScheme / designType / textColor vocabulary
- [[reference-editortemplates-app]] — `editortemplates` app has reference pages per table component variant
- The platform source: `nocode-ui/ui-app/client/src/components/TableComponents/` is the authoritative reference; consult when a behavior question can't be answered from this doc
