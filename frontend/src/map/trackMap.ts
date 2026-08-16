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

// OpenFreeMap: free vector tiles, no key. Future: self-hosted Protomaps.
export const STYLE_URL = "https://tiles.openfreemap.org/styles/liberty"

const FIT_PADDING = 48
const FIT_MAX_ZOOM = 16

export type TrackPt = { atMs: number; lat: number; lon: number }

export type Track = {
  id: string
  color: string
  label?: string
  points: TrackPt[]
}

export type Bounds = { minLat: number; maxLat: number; minLon: number; maxLon: number }

export type TrackMapHandle = {
  setTracks(tracks: Track[]): void
  fitTracks(): void
  fitBounds(bounds: Bounds): void
  setMarker(pos: { lat: number; lon: number; exact: boolean } | null): void
  destroy(): void
}

// MapLibre dwarfs the rest of the app; the dynamic import keeps it in its own
// chunk, fetched only when a map is first opened (same move as loadPlot()).
export async function createTrackMap(
  container: HTMLElement,
  opts: { onPointClick?: (trackId: string, pt: TrackPt) => void } = {},
): Promise<TrackMapHandle> {
  const [ml, worker] = await Promise.all([
    import("maplibre-gl"),
    import("maplibre-gl/dist/maplibre-gl-worker.mjs?url"),
    import("maplibre-gl/dist/maplibre-gl.css"),
  ])
  // MapLibre resolves its worker relative to its own module URL, which breaks
  // once Vite pre-bundles/chunks it; hand it the asset URL Vite actually serves.
  ml.setWorkerUrl(worker.default)

  const map: MapLibreMap = new ml.Map({
    container,
    style: STYLE_URL,
    attributionControl: { compact: true },
  })
  map.addControl(new ml.NavigationControl({ showCompass: false }))

  // Wait for style.load, not load: sources/layers only need the style, and
  // the full load event stalls until every initial tile and glyph fetch
  // settles — a single slow request would park the handle forever.
  if (!map.isStyleLoaded()) {
    await new Promise<void>((resolve) => map.once("style.load", () => resolve()))
  }

  map.addSource("tracks", { type: "geojson", data: emptyCollection() })
  map.addSource("track-points", { type: "geojson", data: emptyCollection() })
  map.addLayer({
    id: "tracks-line",
    type: "line",
    source: "tracks",
    paint: { "line-color": ["get", "color"], "line-width": 3, "line-opacity": 0.85 },
    layout: { "line-cap": "round", "line-join": "round" },
  })
  map.addLayer({
    id: "track-points-circle",
    type: "circle",
    source: "track-points",
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": 4,
      "circle-stroke-width": 1,
      "circle-stroke-color": "#0d1117",
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
            properties: { track: t.id, color: t.color, atMs: p.atMs, lat: p.lat, lon: p.lon },
            geometry: { type: "Point" as const, coordinates: [p.lon, p.lat] },
          })),
        ),
      }
      ;(map.getSource("tracks") as GeoJSONSource).setData(lines)
      ;(map.getSource("track-points") as GeoJSONSource).setData(points)
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
    destroy() {
      marker?.remove()
      map.remove()
    },
  }
}

function emptyCollection() {
  return { type: "FeatureCollection" as const, features: [] }
}
