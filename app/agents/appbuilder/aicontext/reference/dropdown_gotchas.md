# Dropdown: the three props and the wrong event

Dropdown fails in three distinctive ways, and none of them produce an error.

## Selection fires `onClick`, NOT `onChange`

From `Dropdown.tsx:130-143, 240-334`:

```js
const clickEvent = onClick ? props.pageDefinition.eventFunctions?.[onClick] : undefined;
// inside handleClick, fired on each item's onMouseDown:
setData(bindingPathPath, value, context.pageName);
callClickEvent();      // <- this is the user's selection handler
```

The component's `onChange` prop appears exactly once in the whole file, on the
search `<input onChange={handleSearch}>`, and that is the internal HTML event,
not exposed for wiring.

So "navigate after the user picks" or any post-selection event goes on
**`onClick`**. This is counter-intuitive because most form components fire
`onChange` on value change.

## Selection needs three props, not two

Miss any one and items either do not render or the wrong value is stored.

- **`data`**: the source array, as
  `{location: {type: "EXPRESSION", expression: "Page.x.array"}}`.
- **`datatype`**: `LIST_OF_OBJECTS` for arrays of objects, `LIST_OF_STRINGS` for
  `["a","b"]`. From `getRenderData.ts:35-90`, if `datatype` is unset none of the
  branches match and the function returns an empty array. **This is the number
  one cause of an empty dropdown popup despite valid data.**
- **`selectionType` + `selectionKey`**: how the picked item reduces to a stored
  value. With `selectionType: "KEY"` and `selectionKey: "appCode"` the dropdown
  stores `item.appCode` into `bindingPath`.

Valid `selectionType` for Dropdown: `KEY`, `INDEX`, `OBJECT`, `RANDOM`
(`getRenderData.ts:14`). **`PATH` is not valid for Dropdown**, that is a Table
concept.

## Search typing needs `bindingPath2`

```js
const handleSearch = (event) => {
    if (!searchBindingPath) return;    // <- bails when bindingPath2 is unset
    setData(searchBindingPath, event.target.value, context.pageName);
};
```

Without `bindingPath2` the search input is inert: `handleSearch` returns
immediately, the store listener that drives `setSearchText` never fires, and the
input's `value={searchText}` stays empty. **Set `bindingPath2` to some path
whenever `isSearchable` is true**, even if nothing else reads it.

## Minimum working config

```python
patch_component_props(component_key="myDropdown", properties={
    "placeholder": "Pick one",
    "data": {"location": {"type": "EXPRESSION", "expression": "Page.items"}},
    "datatype": "LIST_OF_OBJECTS",
    "selectionType": "KEY",
    "selectionKey": "id",
    "labelKey": "name",
    "uniqueKey": "id",
    "isSearchable": True,
    "onClick": "<event-function-key>",     # NOT onChange
})
patch_component_bindings(component_key="myDropdown", binding_paths={
    "bindingPath":  {"type": "VALUE", "value": "Page.selection"},
    "bindingPath2": {"type": "VALUE", "value": "Page.searchText"},
})
```

Note: `datatype`, `selectionType`, `selectionKey`, `uniqueKey` and `labelKey`
were all missing from the published component catalog until 2026-08-26, which is
why older notes describe them as "rejected as unknown". See
`platform_doc_read('catalog_property_drift')`. The same set was missing on
RadioButton, Tags and TextList.
