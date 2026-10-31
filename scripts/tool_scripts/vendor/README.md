# Vendored three.js (offline geometry viewer)

`render_geometry_viewer.py` inlines these into a self-contained `geometry_viewer.html`
so the geometry-confirmation gate works fully offline (file:// double-click, no CDN).

- `three.min.js` — three.js r0.137.5 UMD global build (attaches global `THREE`).
- `OrbitControls.js` — three.js r0.137.5 `examples/js/controls/OrbitControls.js`
  (global, attaches `THREE.OrbitControls`).

Source: https://unpkg.com/three@0.137.5/ · License: MIT (three.js Authors).
Pinned at r0.137.5 because it ships a UMD global build + a global OrbitControls
(later releases are ESM-only, which is harder to inline for offline file:// use).
