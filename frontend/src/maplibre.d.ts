// The stylesheet import is for Vite's benefit; it has no type surface.
declare module "maplibre-gl/dist/maplibre-gl.css"

// Vite's ?url suffix turns the worker file into a served-asset URL string.
declare module "maplibre-gl/dist/maplibre-gl-worker.mjs?url" {
  const url: string
  export default url
}
