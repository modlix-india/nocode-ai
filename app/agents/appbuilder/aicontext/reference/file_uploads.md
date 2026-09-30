# File / asset uploads in Modlix

The platform has FOUR distinct storage spaces; pick the right one or assets
end up in the wrong place (per-tenant when you wanted app-global, or
unauth'd-public when you wanted secured). The spaces and their REST routes are:

## The four spaces

| Space | Path pattern | Use case | Security |
|---|---|---|---|
| **Static** | `/api/files/static/file/<client>/<path>` (read) — note: NO appCode in download URL | App-global (actually client-global), publicly readable assets (icons, fonts, marketing images, manifest icons) | Read = anonymous; write = auth required |
| **Client** | `/api/files/generic/client` (POST), then `/api/files/generic/file/<...>` (GET) | Files uploaded by tenant admins, scoped to a client | Read = client members; write = client admin |
| **User** | `/api/files/generic/user` (POST) | Files uploaded by an individual end-user (avatars, attachments to their own data) | Read/write = the owning user |
| **Secured** | `/api/files/secured/<client>/<app>/<path>` | Sensitive shared docs (contracts, PII) that need short-TTL access keys | Read = auth + key; write = auth |

For the App Builder app icon → **Static**. The icon is part of the
app's identity, viewed pre-auth.

## Path convention — `<appCode>/<pageName-or-"global">/...`

Modlix's static space is **client-scoped** (no appCode in the download URL),
so multiple apps under the same client share one flat namespace. To keep
assets organized + prevent cross-app collisions, every static upload should
go under `<appCode>/<pageName-or-"global">/...`:

| Scope | Path | Example |
|---|---|---|
| Page-specific | `<appCode>/<pageName>/<filename>` | `appbuilder/homeTwo/forge-rod.png` |
| Page-specific, sub-folder | `<appCode>/<pageName>/<folder>/<filename>` | `appbuilder/loginPage/icons/google.svg` |
| App-global | `<appCode>/global/<filename>` | `appbuilder/global/appicon.png` |
| App-global, sub-folder | `<appCode>/global/<folder>/<filename>` | `appbuilder/global/favicon/favicon.ico` |

Why this matters:
- **Cross-app safety** — multiple apps under SYSTEM (cxapp, sitezump, appbuilder, …) share one flat client-scoped static space. Without the appCode prefix, `cxapp` could clobber `appbuilder/icons/foo.png`.
- **Per-page cleanup** — deleting a page becomes a clean opportunity to drop `<appCode>/<pageName>/*` as well.
- **Discoverability** — agents authoring a page can look at `<appCode>/<pageName>/` to enumerate assets that page uses.

The CFA's `upload_static_asset` and `generate_image` tools default
`page_name='global'` and accept `page_name='<pageName>'`. They construct
the correct path automatically — the caller never sees the appCode-prefix
plumbing.

## Upload to static

The controllers are `AbstractResourceFileController` (parent) and
`StaticResourceFileController` (subclass at `api/files/static`). The relevant
endpoints:

```
POST /api/files/static/<client>/<app>/<path...>      # upload
POST /api/files/static/directory/<client>/<app>/<...>  # create dir
GET  /api/files/static/file/<client>/<app>/<path>    # download (note: GET path has extra `/file/` prefix)
```

Multipart fields:
- `file` (required) — the file part(s); endpoint accepts an array
- `clientCode` (query, optional) — overrides the authenticated client
- `override` (form field, optional) — "true" to overwrite existing
- `name` (form field, optional) — rename on save

The **upload URI is the destination path** — whatever subpath you POST to
becomes the storage location.

**Observed working form** (from a real appbuilder browser upload, 2026-05-18):

```
POST /<appCode>/<clientCode>/page/api/files/static/<folder>?clientCode=<cc>
Headers:
  Authorization: <JWT, no Bearer prefix>
  clientCode:    <cc>
  Content-Type:  multipart/form-data; boundary=...
Body:
  --boundary
  Content-Disposition: form-data; name="file"; filename="<actual-filename>"
  Content-Type: <mime>
  <bytes>
  --boundary--
```

Key shape facts (confirmed 2026-05-18):
- The path segment after `/api/files/static/` is the **folder**, not the
  full file path. So POSTing to `.../api/files/static/bookingsPage` puts the
  file at `bookingsPage/<multipart-filename>`. To upload to the app root,
  POST to `.../api/files/static/` (empty folder).
- **Nested folders auto-create**. POSTing to `.../api/files/static/icons/large`
  creates `icons/large/` if either folder didn't exist. So you can lay out
  deep asset trees in one POST per file — no need to pre-create directories
  via the `/directory/...` endpoint.
- The filename comes from the multipart `Content-Disposition: filename="..."`
  attribute, NOT from the URL.
- The `/<appCode>/<clientCode>/page/` PREFIX is honored by the gateway and
  sets the routing context — that's why `appCode` doesn't appear in the URL
  body. `clientCode` is duplicated as both query param AND header (defensive).
- Resulting URL pattern: `/api/files/static/file/<client>/<app>/<folder>/<filename>`

So the asymmetry is:
- Upload: `.../api/files/static/<folder>` + multipart filename
- Download: `.../api/files/static/file/<client>/<app>/<folder>/<filename>`

## Picking the right tool

The CFA's `visuals` module covers each space:

| Tool | Targets | Returns |
|---|---|---|
| `upload_static_asset` | Static (`api/files/static/<client>/<app>/<path>`) | Public path + absolute URL |
| `upload_client_file` | Client (`api/files/generic/client`) | FileDetail JSON |
| `upload_user_file` | User (`api/files/generic/user`) | FileDetail JSON |
| `build_static_asset_url` | (URL helper, no upload) | `/api/files/static/file/<client>/<app>/<path>` |
| `build_secured_asset_url` | (URL helper, no upload) | `/api/files/secured/file/<client>/<app>/<path>` |
| `resize_image_to_path` | Local-only resize before upload | Path to resized file |
| `image_to_base64` | Inline assets (≤64KB safety cap) | data:image/...;base64,... |
| `generate_secured_access_key` | Secured (read-side) | Short-lived access key |
| `download_secured_file_by_key` | Secured (read-side) | Local file |

## Common gotcha

`Image.src` in a Modlix page accepts either:
- A relative path `/api/files/static/file/.../appicon.png` (resolved against gateway origin)
- An absolute URL `https://gateway.host/api/files/static/file/.../appicon.png`
- A data URI `data:image/png;base64,...`

The data URI is fine for tiny SVGs but **don't inline a 1MB PNG** — it explodes
page JSON size, bloats the SPA bundle pre-resize, and the platform's `image_to_base64`
tool caps at 64 KB to prevent this. Resize via `resize_image_to_path` first or
upload + reference by path.

## Pitfall observed during appbuilder rebuild (2026-05-18)

I referenced `/api/files/static/file/SYSTEM/appbuilder/appicon.png` on a page
before the file had been uploaded. The Image rendered as a broken-image
placeholder with no warning at build time — the platform doesn't validate
src URLs server-side, only the browser surfaces 404 when fetching.

Lesson: when authoring pages that depend on uploaded assets, upload FIRST,
then reference. If iterating on the asset, keep the same path so the page's
src doesn't have to change.
