// The one place MapLibre is touched. Both map surfaces (the session panel and
// the global travel tab) speak TrackMapHandle, so swapping the tile source or
// the whole render library stays a one-file change.

import type {
  GeoJSONSource,
  LngLatBounds,
  Map as MapLibreMap,
  MapMouseEvent,
  Marker,
} from "maplibre-gl"
import { token } from "../styles/tokens"

// OpenFreeMap: free vector tiles, no key. Future: self-hosted Protomaps.
export const STYLE_URL = "https://tiles.openfreemap.org/styles/liberty"

const FIT_PADDING = 48
const FIT_MAX_ZOOM = 16

// Heat tuning. Cells are ~100 m squares; each gets weight = (dwell / busiest
// dwell) ** HEAT_GAMMA in (0, 1]. Lower gamma lifts the mid-range. Intensity
// multiplies density before the ramp; radius (px per zoom stop) controls how
// much neighbouring cells stack. Ramp stops are positions along 0..1 density.
const HEAT_CELL_DEG = 0.001
const HEAT_GAMMA = 0.4
const HEAT_INTENSITY = 1.7
const HEAT_RADIUS: [number, number][] = [
  [8, 14],
  [12, 24],
  [16, 48],
]
/** The sequential --seq-* tokens as heat-ramp stops. */
function heatRamp(): [number, string][] {
  return [
    [0, token("--seq-0")],
    [0.08, token("--seq-1")],
    [0.3, token("--seq-2")],
    [0.55, token("--seq-3")],
    [0.8, token("--seq-4")],
    [1, token("--seq-5")],
  ]
}
const HEAT_OPACITY = 0.9

export type TrackPt = { atMs: number; lat: number; lon: number }

export type Track = {
  id: string
  color: string
  label?: string
  points: TrackPt[]
  /** Raw fixes each point stands for (decimation stride); heatmap weight. */
  weight?: number
}

// lines: current default. arrows: direction of travel along each track.
// heat: density gradient — where the fixes pile up.
export type MapMode = "lines" | "arrows" | "heat"

export type Bounds = { minLat: number; maxLat: number; minLon: number; maxLon: number }

export type TrackMapHandle = {
  setTracks(tracks: Track[]): void
  fitTracks(): void
  fitBounds(bounds: Bounds): void
  setMarker(pos: { lat: number; lon: number; exact: boolean } | null): void
  setMode(mode: MapMode): void
  destroy(): void
}

// Fetch the maplibre chunk ahead of need (the browser caches the modules;
// createTrackMap's own imports then resolve instantly).
export function preloadMapLibre(): void {
  void Promise.all([
    import("maplibre-gl"),
    import("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url"),
    import("maplibre-gl/dist/maplibre-gl.css"),
  ]).catch(() => {})
}

// MapLibre dwarfs the rest of the app; the dynamic import keeps it in its own
// chunk, fetched only when a map is first opened (same move as loadPlot()).
export async function createTrackMap(
  container: HTMLElement,
  opts: { onPointClick?: (trackId: string, pt: TrackPt) => void } = {},
): Promise<TrackMapHandle> {
  const [ml, worker] = await Promise.all([
    import("maplibre-gl"),
    import("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url"),
    import("maplibre-gl/dist/maplibre-gl.css"),
  ])
  // MapLibre resolves its worker relative to its own module URL, which breaks
  // once Vite pre-bundles/chunks it; hand it the asset URL Vite actually
  // serves. `?worker&url` (not plain `?url`) so Vite bundles the worker's own
  // imports — a verbatim copy would die on its relative maplibre-gl-shared
  // import in production, killing all tile and GeoJSON parsing.
  ml.setWorkerUrl(worker.default)

  const map: MapLibreMap = new ml.Map({
    container,
    style: STYLE_URL,
    attributionControl: { compact: true },
  })
  map.addControl(new ml.NavigationControl({ showCompass: false }))

  // Wait for style.load, not load: sources/layers only need the style, and
  // the full load event stalls until every initial tile and glyph fetch
  // settles — a single slow request would park the handle forever. A failed
  // style fetch fires only `error`, so reject on it (and tear the map down)
  // instead of hanging the promise and leaking the instance.
  if (!map.isStyleLoaded()) {
    try {
      await new Promise<void>((resolve, reject) => {
        map.once("style.load", () => resolve())
        map.once("error", (e) => reject(e.error ?? new Error("map style failed to load")))
      })
    } catch (err) {
      map.remove()
      throw err
    }
  }

  map.addSource("tracks", { type: "geojson", data: emptyCollection() })
  map.addSource("track-points", { type: "geojson", data: emptyCollection() })
  map.addSource("track-heat", { type: "geojson", data: emptyCollection() })
  map.addImage("track-arrow", arrowImage(), { sdf: true })
  map.addLayer({
    id: "tracks-heat",
    type: "heatmap",
    source: "track-heat",
    layout: { visibility: "none" },
    paint: {
      // Each feature is one cell with weight in (0, 1] relative to the busiest
      // cell (see heatCells), so density ≈ weight and the ramp is comparable
      // across places instead of saturating on any single visit.
      "heatmap-weight": ["get", "weight"],
      "heatmap-radius": ["interpolate", ["linear"], ["zoom"], ...HEAT_RADIUS.flat()],
      "heatmap-intensity": HEAT_INTENSITY,
      "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"], ...heatRamp().flat()],
      "heatmap-opacity": HEAT_OPACITY,
    },
  })
  map.addLayer({
    id: "tracks-line",
    type: "line",
    source: "tracks",
    paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.85 },
    layout: { "line-cap": "round", "line-join": "round" },
  })
  map.addLayer({
    id: "tracks-arrows",
    type: "symbol",
    source: "tracks",
    layout: {
      visibility: "none",
      "symbol-placement": "line",
      "symbol-spacing": 80,
      "icon-image": "track-arrow",
      "icon-size": 0.6,
      "icon-allow-overlap": true,
      "icon-ignore-placement": true,
      "icon-rotation-alignment": "map",
    },
    paint: { "icon-color": ["get", "color"] },
  })
  map.addLayer({
    id: "track-points-circle",
    type: "circle",
    source: "track-points",
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": 4,
      "circle-stroke-width": 1,
      "circle-stroke-color": token("--surface"),
    },
  })

  if (opts.onPointClick) {
    const onClick = opts.onPointClick
    map.on("click", "track-points-circle", (event: MapMouseEvent) => {
      const feature = map.queryRenderedFeatures(event.point, {
        layers: ["track-points-circle"],
      })[0]
      if (!feature) return
      const props = feature.properties as { track: string; atMs: number; lat: number; lon: number }
      onClick(props.track, { atMs: props.atMs, lat: props.lat, lon: props.lon })
    })
    map.on("mouseenter", "track-points-circle", () => {
      map.getCanvas().style.cursor = "pointer"
    })
    map.on("mouseleave", "track-points-circle", () => {
      map.getCanvas().style.cursor = ""
    })
  }

  let current: Track[] = []
  let marker: Marker | null = null

  const boundsOf = (tracks: Track[]): LngLatBounds | null => {
    const bounds = new ml.LngLatBounds()
    for (const track of tracks)
      for (const pt of track.points) bounds.extend([pt.lon, pt.lat])
    return bounds.isEmpty() ? null : bounds
  }

  return {
    setTracks(tracks) {
      current = tracks
      const lines = {
        type: "FeatureCollection" as const,
        features: tracks
          .filter((t) => t.points.length > 1)
          .map((t) => ({
            type: "Feature" as const,
            properties: { track: t.id, color: t.color },
            geometry: {
              type: "LineString" as const,
              coordinates: t.points.map((p) => [p.lon, p.lat]),
            },
          })),
      }
      const points = {
        type: "FeatureCollection" as const,
        features: tracks.flatMap((t) =>
          t.points.map((p) => ({
            type: "Feature" as const,
            properties: {
              track: t.id,
              color: t.color,
              atMs: p.atMs,
              lat: p.lat,
              lon: p.lon,
              weight: t.weight ?? 1,
            },
            geometry: { type: "Point" as const, coordinates: [p.lon, p.lat] },
          })),
        ),
      }
      ;(map.getSource("tracks") as GeoJSONSource).setData(lines)
      ;(map.getSource("track-points") as GeoJSONSource).setData(points)
      ;(map.getSource("track-heat") as GeoJSONSource).setData(heatCells(tracks))
    },
    fitTracks() {
      const bounds = boundsOf(current)
      if (bounds) map.fitBounds(bounds, { padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM })
    },
    fitBounds(b) {
      map.fitBounds(
        [
          [b.minLon, b.minLat],
          [b.maxLon, b.maxLat],
        ],
        { padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM },
      )
    },
    setMarker(pos) {
      if (!pos) {
        marker?.remove()
        marker = null
        return
      }
      let dot = marker
      if (!dot) {
        const el = document.createElement("div")
        el.className = "track-marker"
        dot = new ml.Marker({ element: el }).setLngLat([pos.lon, pos.lat]).addTo(map)
        marker = dot
      } else {
        dot.setLngLat([pos.lon, pos.lat])
      }
      dot.getElement().classList.toggle("stale", !pos.exact)
    },
    setMode(mode) {
      const show = (id: string, on: boolean) =>
        map.setLayoutProperty(id, "visibility", on ? "visible" : "none")
      show("tracks-heat", mode === "heat")
      show("tracks-arrows", mode === "arrows")
      show("track-points-circle", mode !== "heat")
      map.setPaintProperty("tracks-line", "line-opacity", mode === "heat" ? 0.25 : 0.85)
    },
    destroy() {
      marker?.remove()
      map.remove()
    },
  }
}

// A right-pointing arrow as a true signed-distance field (tiny-sdf encoding:
// alpha 191 at the edge, higher inside) so MapLibre's SDF path draws crisp
// edges and icon-color tints it per track. Line placement rotates it.
function arrowImage(): ImageData {
  const size = 32
  const radius = 8
  const cutoff = 0.25
  const canvas = document.createElement("canvas")
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext("2d")!
  ctx.fillStyle = "#fff"
  ctx.beginPath()
  ctx.moveTo(6, 6)
  ctx.lineTo(26, 16)
  ctx.lineTo(6, 26)
  ctx.lineTo(11, 16)
  ctx.closePath()
  ctx.fill()
  const mask = ctx.getImageData(0, 0, size, size).data
  const inside = (x: number, y: number) => (mask[(y * size + x) * 4 + 3] ?? 0) > 127
  const out = new ImageData(size, size)
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      const me = inside(x, y)
      let best = radius * radius
      for (let yy = 0; yy < size; yy++)
        for (let xx = 0; xx < size; xx++)
          if (inside(xx, yy) !== me) best = Math.min(best, (xx - x) ** 2 + (yy - y) ** 2)
      const d = Math.sqrt(best) * (me ? -1 : 1)
      const a = Math.round(255 - 255 * (d / radius + cutoff))
      const i = (y * size + x) * 4
      out.data[i] = out.data[i + 1] = out.data[i + 2] = 255
      out.data[i + 3] = Math.max(0, Math.min(255, a))
    }
  return out
}

// Dwell per cell across all tracks (weight × fixes ≈ recording time),
// normalised so the busiest cell is 1; see the HEAT_* constants.
function heatCells(tracks: Track[]) {
  const cells = new Map<string, { lat: number; lon: number; sum: number }>()
  for (const t of tracks) {
    const w = t.weight ?? 1
    for (const p of t.points) {
      const lonDeg = HEAT_CELL_DEG / Math.max(0.2, Math.cos((p.lat * Math.PI) / 180))
      const ci = Math.floor(p.lat / HEAT_CELL_DEG)
      const cj = Math.floor(p.lon / lonDeg)
      const key = `${ci}:${cj}`
      const cell = cells.get(key) ?? {
        lat: (ci + 0.5) * HEAT_CELL_DEG,
        lon: (cj + 0.5) * lonDeg,
        sum: 0,
      }
      cell.sum += w
      cells.set(key, cell)
    }
  }
  let max = 0
  for (const c of cells.values()) max = Math.max(max, c.sum)
  return {
    type: "FeatureCollection" as const,
    features: [...cells.values()].map((c) => ({
      type: "Feature" as const,
      properties: { weight: max > 0 ? (c.sum / max) ** HEAT_GAMMA : 0 },
      geometry: { type: "Point" as const, coordinates: [c.lon, c.lat] },
    })),
  }
}

function emptyCollection() {
  return { type: "FeatureCollection" as const, features: [] }
}
