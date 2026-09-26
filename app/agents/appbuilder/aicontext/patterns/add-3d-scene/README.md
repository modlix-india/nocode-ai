---
name: add-3d-scene
description: Show a 3D model on a page with ModelViewer — a .glb the visitor can drag to turn, with clickable parts wired to page events. Use for products, devices, or any object a design shows in three dimensions.
---

# add-3d-scene

`ModelViewer` loads a glTF file and lets the visitor drag to turn it. Parts can
be clicked, and the clicked part's name goes into page data.

## When to use

- a product shown from several angles
- a device the visitor should be able to inspect
- a configurator where clicking a part selects it
- anything a source design rotates or spins in 3D

## When NOT to use

- A spinning logo. That is a CSS `transform` and needs no WebGL context.
- A pre-rendered turntable video. `Video` is cheaper and looks the same.
- More than one on a page, unless the design really needs it.

## Getting a model in

```
fetch_external_asset(url="https://example.com/chair.glb", kind="model")
```

It downloads, checks the file HEADER (not the extension — a `.glb` that is
really an HTML login page loads as nothing and leaves an empty canvas), copies
it into the app's own files, and returns the platform URL to use. It ingests
rather than hotlinks: a page pointing at someone else's URL breaks when they
move it, breaks on CORS and CSP, and bypasses the CDN.

**Licensing is not verified.** Record the source and the licence in the app's
knowledge base.

**Compressed meshes are not supported.** DRACO and KTX2 need decoder files this
build does not ship. Such a model loads as nothing, with no error the author can
see. Ask for an uncompressed export.

## Wiring it up

```
patch_component_props(
    page_name="product", component_key="viewer",
    properties={
        "modelUrl": "api/files/static/file/SYSTEM/app/global/models/chair.glb",
        "environmentPreset": "studio",
        "autoRotate": True,
        "fallbackColor": "#101322",
    },
)
```

**Scale looks after itself.** glTF carries no agreed unit, so the same chair
arrives 0.9 units tall from one exporter and 900 from another. The component
normalises every model to a common size on load, which is why `zoom` and
`cameraHeight` mean the same thing whatever file you point at it. Turn `autoFit`
off only once a scene is hand-placed.

**Lighting costs nothing.** `environmentPreset` is `studio`, `soft`, `dramatic`
or `warm`, all built from real lights. Only set `hdriUrl` when the material is
metal or glass and genuinely needs something to reflect — an `.hdr` is several
megabytes, and it replaces the rig rather than adding to it.

## Clickable parts

`onMeshClick` fires an ordinary page event function with `meshName`, `point` and
`distance`. `bindingPath` holds the selected part name in BOTH directions, so a
page can read the selection or drive it:

```
patch_component_bindings(
    component_key="viewer",
    binding_paths={"bindingPath": {"type": "VALUE", "value": "Page.selectedPart"}},
)
patch_component_props(component_key="viewer",
                      properties={"highlightColor": "#ff2d55"})
```

Setting `onMeshHover` makes every pointer move raycast the scene, which on a
loaded model is the difference between 60fps and 30. Leave it empty unless
something depends on it.

## Two things that surprise people

- **Wheel zoom is off on purpose.** A model that swallowed the wheel would trap
  a visitor who scrolled onto it on their way down the page.
- **A canvas has no height of its own.** It collapses to nothing unless the
  component is given one. The default is 420px; set a height on the COMPONENT,
  not on the canvas.

## Check it

```
render_scene_check(page_name="product")
```

Asserts the canvas is not a flat fill, not black, and that two frames differ.
It probes its own browser for WebGL first, so a failure tells you whether the
problem is your scene or the environment.
