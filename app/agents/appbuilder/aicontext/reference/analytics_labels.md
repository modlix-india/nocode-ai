# Analytics labels

`analyticsLabel` is a common component property. It renders as
`data-analytics-label` on the element, and it is what the analytics beacon
captures clicks on.

**Without it the click is not recorded at all.** The beacon captures labelled
elements only — deliberately, because a name guessed from the DOM (tag,
position, nearby text) changes the next time the layout moves or the copy is
reworded, and a funnel built on those names breaks silently. The label is a
decision somebody made, and it survives a redesign.

## The rule

Give one to every **interactive** component you generate. Skip it on layout and
display components — most of them reject it outright.

```json
{"label": "Submit", "onClick": "handleSubmit", "analyticsLabel": "contact_submit"}
```

## Naming

`snake_case`, naming the **action and its place**, not the visible text.

| good | why |
|---|---|
| `contact_submit` | says which form |
| `plan_upgrade` | says what the click means, not that it reads "Continue" |
| `filter_by_status` | survives the control changing from tabs to a dropdown |
| `consent_accept_all` | distinguishes it from `consent_reject_all` |

| avoid | why |
|---|---|
| `submit` | three screens will collide on it |
| `Save` | not snake_case, and it is the visible text |
| `btn_1` | means nothing in a report six months later |
| `blue_button_top_right` | describes the pixels, not the action |

Two controls reading "Save" on different screens need different labels. A button
whose text is translated still needs one stable label.

Where a page also emits events with `UIEngine.TrackAnalyticsEvent`
(`checkout_started`, `cta_clicked`), match the two schemes so a reader does not
have to learn both.

## Which components accept it

33 of 77. The catalog is authoritative — `get_component_schema` tells you for a
specific type.

**Accept:** AnalyticsQuery, Button, Calendar, Carousel, CheckBox, ColorPicker,
Dropdown, FileSelector, FileUpload, Gallery, Icon, Image, ImageWithBrowser,
Link, Menu, Otp, PhoneNumber, ProductAnalyticsWidget, RadioButton, RangeSlider,
SessionReplayList, SessionReplayPlayer, Small Carousel, Stepper, Svg, Tabs,
Tags, TextArea, TextBox, ThemeSwitcher, ToggleButton, Tree, WebAnalyticsWidget.

**Reject** — passing `analyticsLabel` here fails validation: Animator,
ArrayRepeater, Audio, BlueprintEditor, ButtonBar, Chart, Form, Grid, Iframe,
MarkdownEditor, MarkdownTOC, Popover, Popup, ProgressBar, SchemaForm,
SectionGrid, Shortcut, SubPage, Table and every `Table*` component, Text,
TextEditor, TextList, Timer, Video, and the editor components.

## The gap worth knowing about

`Grid` supports `onClick` and `linkPath`, so a clickable card is a normal thing
to build — but `Grid` does not accept `analyticsLabel`. Such a card is invisible
to autocapture. Either put the label on a control inside the card, or accept the
gap; do not try to pass the property, it will be rejected.

`Table` is the same: the table itself takes no label, but a row action is a
Button, Icon or Link inside a `TableColumn`, and those do.
