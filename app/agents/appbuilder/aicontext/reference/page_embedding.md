# Embedding a page: SubPage, never Iframe

## SubPage embeds another Modlix page

`SubPage.tsx` calls `getPageDefinition(pageName, appCode, clientCode)` and
renders the result as a nested React tree inside the parent. The embedded page
**shares the parent's store**, runs its own event functions, gets its own React
mount, but lives inside the parent's DOM and is governed by the parent's URL.

Props:

- `pageName` (REQUIRED): name of the page to embed
- `appCode`: defaults to the parent page's appCode
- `clientCode`: defaults to the parent page's clientCode
- `overrideThemeStyles`: re-applies theme on mount, useful when the embedded
  page comes from a different app or tenant

## Iframe cannot host a Modlix page

`Iframe.tsx`'s `secureSource()` helper fetches the URL via axios with the user's
Auth header, takes the response as a Blob, creates a Blob URL with
`URL.createObjectURL`, and points the iframe at that.

That works for **static** content. It cannot host a running Modlix SPA: the Blob
URL contains the static HTML response, and the SPA's JavaScript does not execute
in the Blob context the same way (different origin, no router context, no auth
bootstrap). The iframe loads and the embedded page is blank.

- **Use Iframe for**: authenticated file downloads (signed PDFs), exported
  reports rendered as HTML, third-party embeds needing auth headers.
- **Do NOT use Iframe for**: embedding another Modlix page. Use SubPage.

## Caveat: URL-driven pages do not embed cleanly

The embedded page sees the PARENT's URL, not its own. Pages like `editFunction`,
`editPage` and `editStorage` read their target entity id from
`Store.urlDetails.pathParts[N]`. Embed one at a parent URL like `/workspace` and
`pathParts[3]` is `"workspace"` or undefined, so the embedded page cannot tell
what to edit.

Options, in order of preference:

- Give the edit page a fallback: accept the entity id from `Page.embedTargetId`
  when the URL pathParts do not match.
- Use a wrapper page that translates parent state into the right pathParts.
- Reauthor the editor to support embedding.

For URL-independent pages (marketing pages, theme test pages, anything that does
not read pathParts) SubPage just works.

## Sizing

The flex chain through Modlix container components does not reliably propagate
full height, so give the SubPage an explicit height:

```python
patch_component_styles(component_key="workspaceEditorSubPage", sub_component="comp",
    css_props={"width": "100%", "height": "calc(100vh - 122px)", "overflow": "auto"})
```

## Related

- `wrapShell: true` on a page wraps it in the app's `shellPage`. That is a
  different mechanism from SubPage: the shell wraps the page, SubPage embeds a
  page inside a page.
