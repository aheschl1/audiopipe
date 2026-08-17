// The stylesheet import is for Vite's benefit; it has no type surface.
declare module "maplibre-gl/dist/maplibre-gl.css"

// Vite's ?worker&url suffix bundles the worker entry (imports and all) into
// a self-contained asset and yields its URL string.
declare module "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url" {
  const url: string
  export default url
}
