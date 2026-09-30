# Keyboard shortcuts

How to bind a key to something on a page, and the traps.

## The spec grammar

Write `Mod` for the platform's primary modifier: it resolves to Cmd on a Mac and
Ctrl everywhere else. One authored spec covers both.

    Mod+S          Cmd+S on Mac, Ctrl+S elsewhere
    Mod+Shift+K
    Alt+ArrowDown
    Ctrl+S         literal Control, even on a Mac

`Mod` is the ONLY token that adapts. If you write `Ctrl` you get real Control on
every platform, which is almost never what an app author means. Prefer `Mod`.

Accepted modifier spellings: `Mod` / `CmdOrCtrl`, `Cmd` / `Command` / `Meta` / `Win`,
`Ctrl` / `Control`, `Alt` / `Opt` / `Option`, `Shift`. Key names are case
insensitive and `Esc`/`Escape`, `Up`/`ArrowUp`, `Return`/`Enter` are aliases.

## Three ways to bind

**1. Focus an input.** Put the shortcut on the input itself:

    shortcutKey:    "Mod+K"
    shortcutAction: "FOCUS_SELECT"     FOCUS | FOCUS_SELECT | EVENT
    shortcutScope:  "PAGE"             PAGE | GLOBAL | LOCAL
    shortcutGroup:  "Navigation"       heading in the cheat sheet

**2. Activate a control.** Put it on the Button. There is no `shortcutAction`
because the action is always "click", which routes through the existing `onClick`,
so the button's loading guard and `linkPath` navigation come along.

    shortcutKey: "Mod+S"

**3. Run a page function with no control on screen.** Use the non-visual
`Shortcut` component. It renders nothing at runtime and shows a draggable marker
in the editor.

    type:        "Shortcut"
    shortcutKey: "Mod+Enter"
    onShortcut:  "<event function key on THIS page>"
    label:       "Submit and close"

`onShortcut` obeys the same rule as `onClick`: the event function must exist in
this page's `eventFunctions`.

## Scope

- `PAGE` (default): active anywhere on this page.
- `GLOBAL`: active on every page. Put the component on the SHELL page to get a
  genuinely app-wide key.
- `LOCAL`: active only while focus is inside that component.

When two shortcuts claim the same key, the winner is decided by priority, then
scope (LOCAL beats PAGE beats GLOBAL), then nesting depth, then DOM order. An
open Popup pushes its own layer, so a dialog owns the key outright rather than
competing with the page behind it. A genuine tie pops a chooser at runtime, which
is a bug in the page, not a feature: give one of them a higher
`shortcutPriority`.

## Traps

**Never put a shortcut on a component inside a repeater.** A Table or
ArrayRepeater row is rendered N times, and a single key cannot say which row it
meant. The runtime silently refuses to register any shortcut whose component has
a non-empty `locationHistory`, so it will look configured and do nothing. Put the
shortcut on something outside the repeat.

**Keys the browser will not give you.** These never fire, no matter what you
write: `Mod+W`, `Mod+T`, `Mod+N`, `Mod+Q`, `Mod+Tab`, `F11`, `F12`,
`Mod+Shift+I/J/N/T/W`. These work but hijack a browser function, so use them
deliberately: `Mod+S`, `Mod+P`, `Mod+F`, `Mod+D`, `Mod+R`, `Mod+L`, `Mod+1`
through `Mod+9`.

`Mod+/` and `?` are reserved by the platform's own shortcut cheat sheet.

**Safe and idiomatic:** `Mod+K`, `Mod+J`, `Mod+E`, `Mod+B`, `Mod+Enter`, and most
`Mod+Shift+<letter>` pairs.

**Bare letters do not fire while typing.** A shortcut whose combo has no Ctrl or
Meta is suppressed whenever focus is in an input, textarea or contenteditable.
Combos with Ctrl or Meta still fire, which is what makes `Mod+K` work from inside
another field.

## Showing the key on screen

There is no built-in chip. Every live shortcut mirrors itself, read only, to:

    Store.shortcuts.<pageName>.<component name>

as `{ spec, display, aria, label }`. `display` is already formatted for the
viewer's OS: `⌘K` on a Mac, `Ctrl+K` on Windows. Bind a Text component to it and
place it however the design needs:

    text:       { location: { type: "EXPRESSION",
                  expression: "Store.shortcuts.myPage.searchBox.display" } }
    visibility: { location: { type: "EXPRESSION",
                  expression: "Store.shortcuts.myPage.searchBox.display != null
                               and Page.searchValue = null" } }

The visibility clause is the usual pattern: show the hint only while the field is
empty, so the chip does not collide with the input's clear button.

Two gotchas on that path:

- The key is the component's NAME, not its key. Two components on a page sharing
  a name collide.
- `<pageName>` is the RENDER context, not the file name. A page rendered inside
  the shell (anything wrapped by the app shell, including a shell-header SubPage)
  reports as `_global`. Check `globalThis.getStore().shortcuts` in the running app
  rather than assuming.

Nothing is published for a component inside a repeater, consistent with those not
registering.

## What users get for free

Every registered shortcut is discoverable without any extra authoring: holding
Cmd or Ctrl for half a second flashes a chip over every shortcut-bound control on
screen, and pressing `?` opens a cheat sheet grouped by `shortcutGroup`. Controls
also append the key to their tooltip. Only the always-on inline hint needs the
Text component above.
