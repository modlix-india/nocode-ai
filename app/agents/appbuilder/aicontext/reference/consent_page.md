# Designing a cookie consent page

## It is a page the platform lays OVER the current one

A consent page is an ordinary Modlix page, but it is never a destination. The
platform renders it **alongside whatever page is showing** — the visitor keeps
the page they asked for, with the consent box on top of it.

Wire it with one app property:

    properties.consentPage = "cookieConsent"

The server inlines that page into the application document as
`properties.consentPageDefinition`, the same way it inlines the shell, so it
costs no extra round trip and is there on the first paint of every page —
including marketing pages that opt out of the shell with `wrapShell: false`.
`RenderEngineContainer` then renders it next to the page:

    <Page pageDefinition={thePageTheVisitorAskedFor} />
    {consentOverlay}

It appears whenever both hold:

    analytics.enabled === true
    no decision recorded yet

Consent is unconditional: there is no application setting that turns asking off.
`analytics.consentRequired` used to be one and is now ignored wherever it still
appears in a document.

and it disappears on its own the moment a decision is recorded.

**Reference implementation: `sitezump`'s `consentPage`.** 23 components, a bar
that expands into a preferences panel. Read it before building a new one.

## Two ways to get this badly wrong

**Do NOT make it a page-routing target.** A rule like

    when COOKIE[modlix_analytics_consent] NOT_EXISTS  ->  show "cookieConsent"

*replaces* the page the visitor asked for, and it is a trap: the only way out is
for the consent page to set the very cookie the rule tests, so any mistake in
its wiring locks every visitor out of the site permanently. Seen live on
`crumbco`, where every visit to `/` served the consent box and the home page was
fetched but never rendered.

`set_page_route_rule` **refuses** this now rather than warning about it. It will
not write a rule whose target is the app's `properties.consentPage`, and it will
not write any rule with a `COOKIE … NOT_EXISTS` condition on the consent cookie,
whatever that rule points at — "has not answered the cookie question" is not a
routing condition, because routing has no way to make anyone answer.
`get_page_routing` reports the same shape on rules written by hand in the
editors.

**Do NOT navigate to it, and do NOT navigate away after saving.** The reference
accept/save events do exactly one thing:

    AcceptAllCookies:       UIEngine.SetAnalyticsConsent(granted = true)
    SaveCookiePreferences:  UIEngine.SetAnalyticsConsent(granted = true,
                                analytics = Page.analytical,
                                marketing = Page.marketing)

No `Navigate`. The overlay is gated on "no decision yet", so recording the
decision removes it. A `Navigate` there would drag the visitor off the page they
were reading.

## The wiring mistake that silently kills the page

**`onClick` takes the event KEY, not its name.** So does `properties
.onLoadEvent`. `pageDefinition.eventFunctions` is keyed by id:

    eventFunctions: { "e88c4f30681e446e96af897a8a74aa21": { name: "AcceptAllCookies", ... } }

    RIGHT   onClick: { value: "e88c4f30681e446e96af897a8a74aa21" }
    WRONG   onClick: { value: "AcceptAllCookies" }

`eventFunctions["AcceptAllCookies"]` is `undefined`, so the button renders,
clicks, and does nothing at all — no error, no console message. `crumbco`'s
consent page had all three buttons wired by name and `onLoadEvent` left null; the
visible symptom was "Accept all does nothing and the toggles show defaults".
(A step in the `_` namespace is the opposite — it takes the NAME.)

## The two KIRun functions

`UIEngine.GetAnalyticsConsent` — seed the page on load. Returns
`{status, decided, required, enabled, categories: {necessary, analytics, marketing}}`.
Before a decision exists, `categories` is what a preferences panel should SHOW
(everything on), not what has been agreed to. Always check `decided` first.

`UIEngine.SetAnalyticsConsent(granted, analytics?, marketing?)` — record it.
Writes localStorage **and** a first-party cookie (default
`modlix_analytics_consent`, overridable with `analytics.consentCookieName`),
one year, and pushes the decision into the analytics beacon.

`Store.analyticsConsent` mirrors the state for binding component visibility.

Categories: `necessary` is never a real choice — show it on and disabled.
`analytics` gates the beacon. `marketing` is recorded and exposed but drives
nothing yet, so offering the toggle promises something the platform does not
keep until an ad pixel reads it.

The reference `ConsentOnLoad` seeds a view mode plus one store key per toggle:

    state      UIEngine.GetAnalyticsConsent
    view       Page.view       = "bar"          # "bar" | "prefs"
    necessary  Page.we         = true
    analytical Page.analytical = Steps.state.output.categories.analytics
    marketing  Page.marketing  = Steps.state.output.categories.marketing

## It does NOT gate A/B tests

It used to. Consent is required on every site, and a split would not draw for a
visitor who had not agreed — so a site with no consent surface had no visitor who
could ever agree, and every split served one arm to everybody, for ever, in
silence.

Changed 2026-09-20. Page-routing splits now draw for every visitor whatever they
answered, because `modlix_page_variant` carries no identifier — its whole content
is rule key to arm key — and exists only to stop the page changing under someone
between clicks. Consent still gates **measurement**, so an unconsenting visitor
is split but never counted, which is how most A/B tools behave.

The campaign carry-forward cookie (`modlix_route_query`) is still gated, because
a campaign parameter says where someone came from.

## Alternative: embed it with SubPage

A page that wants the consent UI inline rather than floating can embed the same
page with a `SubPage` component — it shares the parent's store and runs its own
events. Use this when the consent UI belongs in the layout (a settings screen
offering "manage cookie preferences", say).

For the site-wide banner prefer `properties.consentPage`: it places itself on
every page, survives `wrapShell: false`, is inlined by the server, and hides
itself once a decision exists. A SubPage has to be placed by hand on every page
that needs it, and gated by hand too.

See also: `page_embedding.md` (SubPage vs Iframe), `personalization.md`.
