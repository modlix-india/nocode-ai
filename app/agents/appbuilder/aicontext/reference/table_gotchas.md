# Table gotchas: pagination, empty state, scrolling

The full Table mental model lives in the `handle-tables` pattern
(`pattern_read('handle-tables')`). This doc holds the four failures that pattern
does not cover, each of which looks like a broken component and is not.

## 1. Server-paginated tables need their query object initialized in onLoad

For `offlineData: false`, `bindingPath3` is the current-page path and
`bindingPath4` is the page-size path (e.g. `Page.invoiceQuery.page` and
`Page.invoiceQuery.size`), with `onPagination` firing a fetch that reads them.

**You MUST create that query object in onLoad before the table renders:**

```
SetStore(path = "Page.invoiceQuery", value = {page: 0, size: 10})   # VALUE literal
```

`Table.tsx` runs `addListenerAndCallImmediately(pageSizeBindingPath, setPageSize)`
and the same for the page number. If the path is undefined the immediate call
sets both to **undefined**, and you get two symptoms that look unrelated:

- `<select value={undefined}>` for per-page renders its FIRST `perPageNumbers`
  option as selected. The dropdown says "2" while the fetch actually used 10.
- `pageNumber` undefined makes the next arrow compute `undefined + 1 = NaN`,
  writes `page=NaN`, and the fetch returns page 0. Pagination looks dead on both
  arrows and on per-page change, even with 127 pages of data.

The initial data fetch may stay hardcoded at `page=0&size=10` as long as it
matches the initialized values. `totalPages` binds to the fetch response's
`.totalPages` (Spring Page).

## 2. Never put `visibility` on TableEmptyGrid or TableColumns

The Table shows `TableEmptyGrid` **automatically** when the bound data is empty,
and `TableColumns` when there are rows. Adding `visibility` to either overrides
that built-in behaviour: `visibility: {value: null}` reads as falsy and hides the
grid, and a stale flag expression that never becomes true keeps it hidden
forever. The empty grid needs no visibility at all.

(Some pages do gate both children with a flag, but only because they have a
three-way view switch of table / empty / kanban. Do not generalize from that.)

**If the empty grid "won't show", look upstream at onLoad, not at visibility.**
The usual cause is the onLoad chain throwing before the table settles. On
leadzump `templateConversion` a `ForEachLoop(source = Page.mappings.content)`
got null, because an empty `eager/query` response **omits the `content` field
entirely** and returns only `totalElements: 0`. The loop threw "Expected array
but found null", onLoad aborted, and the table never rendered any state.

The fix is to gate the step, not to default the expression, since the runtime
parser rejects `?? []`:

```
System.If(condition = Page.mappings.totalElements = 0)   # skip enrich on true
```

## 3. Horizontal scroll drops the trailing padding

A Table inside a Grid with `overflowX: auto` scrolls on the WRAPPER, not inside
the Table. DOM chain inner to outer: `table.comp.compTableColumns` (full column
width) → `div._tableContainer` → `div._tableWithPagination` → the Table root
`div.comp` (overflow visible, narrower, spills) → the wrapper Grid, which is the
actual scroller.

`paddingRight` on the scroll wrapper is **dropped from scrollWidth** when the
content overflows. The last column ends up flush against the right scroll end
and the padding only shows on the left.

Fix: put the trailing space inside the scrollable content. Set `marginRight:
40px` on the **TableColumns** component, the `<table class="compTableColumns">`
element that establishes scroll width. Verified on leadzump/productCampaign:
scrollWidth went 2398 to 2438.

Vertically: if the wrapper has a constrained height and `overflowY: hidden`,
rows past the fold are clipped and unscrollable. Set `overflowY: auto`. Both
axes can scroll on the same wrapper.

## 4. Scroll inside vs scroll outside is decided by WHERE overflow lives

Not by any Table property.

**Scroll inside** (sticky header, scrollbar hugs the rows): put overflow on the
Table's own sub-component style slots.

- `tableContainer`: `overflow: auto` + `maxHeight: 72vh`
- `tableWithPagination`: `overflow: auto` when paginated
- outer wrapper Grids carry NO overflow, just `flexGrow: 1` and padding

**Scroll outside** (scrollbar on the page, whole table overflows): the Table has
no overflow style slots and an ancestor Grid sets `overflowX` / `overflowY: auto`.

**Frozen header is a second, separate switch.** The `TableColumns` child has a
`fixedHeader` boolean (display name "Fix Header on Scroll", default false). True
adds the `_fixedHeader` class, and the sticky rule

```css
.comp.compTableColumns._fixedHeader thead { position: sticky; top: 0; z-index: 1; }
```

lives in the static stylesheet `ui-app/client/dist/css/Table.css`, NOT in
`TableColumnsStyle.tsx`, so grepping src will not find it. Sticky only works
because the scroll-inside container gives the thead something to stick within.
`fixedHeader: true` without scroll-inside does nothing.
