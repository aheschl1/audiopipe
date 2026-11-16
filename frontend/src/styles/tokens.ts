/** Resolves a CSS custom property from :root, for canvas/WebGL renderers that can't take var(). */
export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
