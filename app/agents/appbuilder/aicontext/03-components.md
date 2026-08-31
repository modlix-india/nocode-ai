# Component Reference

## Component Summary

| Component | Type | Children | Pseudo-states | Key Sub-components |
|-----------|------|----------|---------------|--------------------|
| Grid | `Grid` | Unlimited | hover, focus, readonly | — |
| SectionGrid | `SectionGrid` | Unlimited | hover, focus | — |
| Text | `Text` | No | hover | text, markdownContainer, h1-h6, p, links |
| Button | `Button` | No | focus, hover, disabled | leftIcon, rightIcon, leftImage, rightImage |
| TextBox | `TextBox` | No | focus, disabled | inputBox, label, leftIcon, rightIcon, supportText, errorText, asterisk |
| TextArea | `TextArea` | No | focus, disabled | (same as TextBox) |
| Dropdown | `Dropdown` | No | hover, focus, disabled | inputBox, dropDownContainer, dropdownItem, label, searchBox, checkbox |
| CheckBox | `CheckBox` | No | hover, focus, disabled | checkbox, label, thumb |
| RadioButton | `RadioButton` | No | hover, focus, disabled | checkbox, label, thumb |
| ToggleButton | `ToggleButton` | No | hover | knob, label, icon |
| Calendar | `Calendar` | No | hover, focus, disabled | inputBox, calendarContainer, header, day |
| Image | `Image` | No | hover | image, zoomPreview, tooltip |
| Icon | `Icon` | No | None | — |
| Link | `Link` | No | hover, visited | externalIcon |
| Menu | `Menu` | No | hover, disabled, active, visited | menuItem, submenu, icon |
| Popup | `Popup` | Unlimited | None | modal, titleGrid, modalTitle, closeButton |
| Popover | `Popover` | 1 (trigger) | None | content, arrow |
| Tabs | `Tabs` | Per tab | hover | tabsContainer, tab, tabHighlighter, childContainer |
| Carousel | `Carousel` | Unlimited | None | arrowButtons, indicatorContainer, indicatorButton |
| ArrayRepeater | `ArrayRepeater` | 1 (template) | None | repeatedComp, add, remove, move |
| Form | `Form` | Unlimited | None | — |
| Table | `Table` | Columns | hover | — |
| Video | `Video` | No | None | video |
| Audio | `Audio` | No | None | playIcon, pauseIcon, progressBar |
| ProgressBar | `ProgressBar` | No | hover | track, fill, label |
| Chart | `Chart` | No | None | — |
| FileUpload | `FileUpload` | No | hover, disabled | uploadButton, fileList, removeButton |
| RangeSlider | `RangeSlider` | No | hover, readOnly | track, thumb, valueLabel |
| Stepper | `Stepper` | No | hover | listItem, doneListItem, activeListItem |
| Tags | `Tags` | No | hover, disabled | inputBox, tagsContainer, eachTag, tagCloseIcon |
| Timer | `Timer` | No | None | — |
| Otp | `Otp` | No | focus, disabled | inputBox, label, errorText |
| PhoneNumber | `PhoneNumber` | No | focus, disabled | dropdownSelect, inputBox, label, searchBox |
| ButtonBar | `ButtonBar` | No | hover, disabled, active | button |
| ColorPicker | `ColorPicker` | No | hover, disabled, focus | dropDownContainer, inputBox, label |
| Iframe | `Iframe` | No | None | iframe |
| Gallery | `Gallery` | Unlimited | None | slideImage, arrowButtons, thumbnail |
| ImageWithBrowser | `ImageWithBrowser` | No | hover | image |
| SchemaForm | `SchemaForm` | No | None | — |
| SmallCarousel | `Small Carousel` | Unlimited | None | slidesContainer, prevButton, nextButton |
| TextList | `TextList` | No | hover | listItem, listItemIcon |
| MarkdownTOC | `MarkdownTOC` | No | hover, visited | titleText, H1-H6 |
| Animator | `Animator` | 1 | None | — |
| SubPage | `SubPage` | No | None | — |

## Key Component Properties

### Grid
- `layout`: `ROWLAYOUT` | `SINGLECOLUMNLAYOUT` (default) | `ROWCOLUMNLAYOUT` | `TWOCOLUMNSLAYOUT` | `THREECOLUMNSLAYOUT`
- `containerType`: `DIV` (default) | `ARTICLE` | `SECTION` | `ASIDE` | `FOOTER` | `HEADER` | `MAIN` | `NAV`
- `onClick`, `linkPath`, `visibility`, `readOnly`, `stopPropagation`, `preventDefault`
- Events: onClick, onMouseEnter, onMouseLeave, onEnteringViewport, onLeavingViewport, onDropData

### Text
- `text`: content to display
- `textType`: `TEXT` (default) | `MD` (Markdown)
- `textContainer`: `SPAN` (default) | `H1`-`H6` | `P` | `B` | `I` | `PRE`
- `textColor`, `stringFormat`, `textLength`, `visibility`

### Button
- `label`, `onClick`, `linkPath`, `target`
- `leftIcon`, `rightIcon`, `leftImage`, `rightImage`
- `designType`: `_outlined` | `_text` | `_iconButton` | `_iconPrimaryButton` | `_fabButton` | `_fabButtonMini` | `_decorative` | `_bigDesign1`
- `colorScheme`, `readOnly`, `visibility`, `stopPropagation`

### TextBox
- `label`, `placeholder`, `bindingPath`, `defaultValue`
- `isPassword`, `valueType` (`text`|`number`), `numberType` (`DECIMAL`|`INTEGER`)
- `validation`, `readOnly`, `autoFocus`, `autoComplete`, `noFloat`
- `supportingText`, `leftIcon`, `rightIcon`, `maxChars`, `showMandatoryAsterisk`
- `designType`: `_outlined` | `_filled` | `_bigDesign1` | `_editOnReq`
- `emptyValue`: `UNDEFINED` | `NULL` | `ENMPTYSTRING` | `ZERO`
- `updateStoreImmediately`: update on typing (vs blur)
- Events: onChange, onBlur, onFocus, onEnter, onClear, onLeftIconClick, onRightIconClick

### Dropdown
- `label`, `bindingPath`, `placeholder`
- `data`: options array/path; `labelKey`, `uniqueKey`, `selectionKey`
- `isSearchable`, `isMultiSelect`, `noFloat`, `validation`
- `designType`: `_outlined` | `_filled` | `_bigDesign1` | `_text` | `_editOnReq`
- Events: onClick, onSearch, onScrollReachedEnd

### CheckBox / RadioButton
- `label`, `bindingPath`, `defaultValue`
- RadioButton also has: `optionValue`, `groupName`
- Events: onChange

### Image
- `src`, `alt`, `src2`-`src5` (responsive), `fallBackImg`, `imgLazyLoading`
- `enhancementType`: `none` | `zoomPreview` | `magnification` | `comparison`
- `onClick`

### Icon
- `icon`: icon class string (e.g., `"fa-solid fa-user"`)
- `onClick`

### Popup
- `showClose`, `closeOnEscape`, `closeOnOutsideClick`
- `modelTitle`, `modalPosition`: `_left_top` | `_center_center` | `_right_bottom` | etc.
- `designType`: `_design1`
- Events: eventOnOpen, eventOnClose

### Tabs
- `tabs` (array of tab names), `defaultActive`, `icon` (array)
- `tabsOrientation`: `_horizontal` | `_vertical`
- `tabsPosition`: `_start` | `_center` | `_end` | `_spaceAround` | `_spaceBetween` | `_spaceEvenly`
- Events: onTabChange

### ArrayRepeater
- `bindingPath`: path to array data
- `layout`: same as Grid
- `showAdd`, `showDelete`, `showMove`, `isItemDraggable`
- `dataType`: `array` | `object`; `defaultData`, `filterCondition`
- Events: addEvent, removeEvent, moveEvent

### Form
- `validationCheck`, `onSubmit`

### Table
Table is a family, not a single component: `Table` + `TableColumns` /
`TableColumn` / `TableGrid` / `TablePreviewGrid` / `TableRow` / `TableEmptyGrid`,
driven by **seven** binding paths. Do not guess its props.

- `bindingPath` data array (REQUIRED), `bindingPath2` selection,
  `bindingPath3` page number, `bindingPath4` rows per page, `bindingPath5` mode,
  `bindingPath6` sort, `bindingPath7` personalization
- `tableDesign` (`_design0`.. `_design9`), `colorScheme`, `tableLayout`,
  `displayMode`, `previewMode`, `offlineData`, `selectionType`, `multiSelect`,
  `uniqueKey` (REQUIRED for selection / tree / personalization), `defaultSize`,
  `perPageNumbers`, `totalPages`, `treeMode`, `childrenKey`
- Cells vary per row ONLY via `Parent.<field>`. A literal `text.value` renders
  the same string on every row, and that is the single most common Table bug.
- Events: `onSelect`, `onPagination`, `onSort`, `onExpandEvent`

Full model and recipes: `pattern_read('handle-tables')`.
Failure modes: `platform_doc_read('table_gotchas')`.

### Carousel
- `autoPlay`, `interval`, `showDots`, `showArrows`

## Validation Types

For form components (TextBox, Dropdown, etc.):

| Type | Description |
|------|-------------|
| `MANDATORY` | Field is required |
| `EMAIL` | Valid email format |
| `URL` | Valid URL format |
| `PHONE` | Valid phone number |
| `REGEX` | Custom regex pattern |
| `MIN_LENGTH` | Minimum characters |
| `MAX_LENGTH` | Maximum characters |
| `MIN` | Minimum numeric value |
| `MAX` | Maximum numeric value |

```json
{"validation": [
  {"type": "MANDATORY", "message": "Required"},
  {"type": "MIN_LENGTH", "value": 8, "message": "Min 8 chars"}
]}
```

## Common Properties (most components)

`visibility`, `readOnly`, `onClick`, `linkPath`, `designType`, `colorScheme`
