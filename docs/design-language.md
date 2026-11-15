# Design language

One language with three layers. The **core** holds the tokens and principles. The **app** layer is dense and functional. A future **brand** layer (the public site) is expressive and editorial. Tokens live in `frontend/src/styles/tokens.css`. Component styles use only `var(--…)` tokens. Canvas and WebGL renderers (Plotly, MapLibre) read the same tokens through `token()` in `frontend/src/styles/tokens.ts`.

## Principles

1. **Content first.** Transcripts, photos and the timeline are the interface. Chrome stays quiet and neutral.
2. **Flat.** Solid fills only: no gradients, no blur or glass, no drop shadows. Hairline borders, whitespace and a step of tone separate things, not elevation.
3. **Red means something.** The red marks the primary action, the active state, the playhead, focus and live recording. If everything is red, nothing is.
4. **Precise.** Type sizes, spacing and radii come from fixed scales, and time uses tabular figures. Alignment matters more than decoration.
5. **Still.** Motion only confirms state changes, and only briefly. Nothing moves on hover except colour.

## Colour

### Neutrals (warm, slightly paper-like)

| Token | Hex | Use |
|---|---|---|
| `--paper` / `--surface` | `#FFFFFF` | page, panels |
| `--sunken` | `#F6F5F2` | hover, wells, chips, the gate backdrop |
| `--sunken-2` | `#EEEDE9` | pressed state, strong chips, tracks of bars |
| `--line-faint` | `#EFEDE9` | lane dividers |
| `--line` | `#E3E1DC` | hairlines, list dividers |
| `--line-strong` | `#CBC8C0` | control borders, popovers |
| `--ink` | `#1A1A17` | text, data bars |
| `--ink-2` | `#3D3C38` | secondary strong text |
| `--muted` | `#5C5B56` | metadata, labels |
| `--faint` | `#8A8882` | placeholders, disabled, unassigned data only |

### Accent: red

| Token | Hex | Use |
|---|---|---|
| `--red` | `#E4002B` | primary button fill, playhead, focus ring, active marker, live dot |
| `--red-hover` | `#C80025` | primary hover |
| `--red-press` | `#A8001F` | primary pressed |
| `--red-ink` | `#C80025` | red text on a red wash (active toggles, the time under the playhead) |
| `--red-wash` | `#FDEDEF` | active toggle background, playing utterance, text selection |
| `--red-line` | `#F5B8C3` | border of an active chip |

The red is warm, saturated and confident. It was picked over `#F40009` because white text on `#E4002B` passes AA (4.85:1), and `#F40009` does not (4.33:1). Only the colour is borrowed. No third-party name, logo or lettering is used.

### Semantic

| Token | Hex | Wash |
|---|---|---|
| `--ok` | `#1C7A4A` | `--ok-wash #EAF4EE` |
| `--warn` | `#8F6400` | `--warn-wash #FBF3E0` |
| `--danger` | `#A8001F` | `--danger-wash #FBECEE`, `--danger-line #F0C2CA` |

Errors are the one other use of the red hue. They are always the deeper `--danger` text on a wash, never a filled red block, so they never read as a primary action.

### Categorical data palette

Speakers, sound classes and session tracks hash to one of eight slots (`categoryIndex()` in `speakers.ts`). The same id gets the same colour everywhere: lanes, chips, avatars, the voice map and the travel map.

| Slot | Hex | | Slot | Hex |
|---|---|---|---|---|
| `--cat-1` blue | `#2D6CB0` | | `--cat-5` umber | `#8A5E3B` |
| `--cat-2` teal | `#16807A` | | `--cat-6` slate | `#526478` |
| `--cat-3` green | `#4B8236` | | `--cat-7` plum | `#8C4A8C` |
| `--cat-4` violet | `#6A50B0` | | `--cat-8` olive | `#77751F` |

- There is no red, orange or pink in the palette, so data never competes with the accent.
- Mid-dark and moderately saturated, so the palette stays calm on white. Every slot is at least 4.5:1 on white, so a slot can also be used as text (for example, avatar initials).
- Fills use the slot at about 28% (`categoryTint()`) with a solid 1px border in the full slot colour. A block stays legible without becoming loud.
- Fixed data roles: `--data-location #2B7799` (GPS ticks and tracks), `--data-media #B67810` (photos and video), `--data-neutral` (untyped sound tags).
- Sequential ramp for density (the heat map): `--seq-0` to `--seq-5`, from transparent through `#C5D8EE` to `#17406E`.

### Contrast (WCAG AA)

| Pair | Ratio |
|---|---|
| ink on paper | 17.4 |
| muted on paper / on sunken | 6.8 / 6.2 |
| red on paper (link-size text) | 4.85 |
| white on red (button) | 4.85 |
| red-ink on red-wash | 5.3 |
| danger on danger-wash | 6.8 |
| ok on ok-wash / warn on warn-wash | 4.75 / 4.76 |

`--faint` (3.5:1) is reserved for placeholders, disabled controls and non-essential marks. Borders like `--line-strong` are not relied on alone for meaning.

## Typography

**IBM Plex Sans** for the interface and **IBM Plex Mono** for timecodes, ids and scores. Both are bundled with `@fontsource` (`styles/fonts.css`), so nothing loads from a font CDN at runtime.

Why Plex:
- It is engineered and precise without being cold, which fits a tool that records your life.
- It has true tabular figures and a matching mono, which keeps clocks, rulers and scores aligned.
- It belongs to a superfamily. The brand site can add **IBM Plex Serif** for editorial headlines and stay in the same voice.

Scale (px): `--fs-2xs 11`, `xs 12`, `sm 13`, `md 14` (app body), `lg 16`, `xl 20`, `2xl 24` (page titles). The brand layer adds `--fs-display-1 32`, `display-2 44` and `display-3 64`.

- Weights: 400, 500 (labels, active nav), 600 (titles, speaker names).
- Line height: 1.55 for body text, 1.4 for compact UI, 1.25 for headings.
- Small-caps section labels: 11px, uppercase, +0.06em tracking, `--muted`.

## Space, radius, borders

- **Space:** 4px base: `--sp-0 2`, `1 4`, `2 8`, `3 12`, `4 16`, `5 24`, `6 32`, `7 48`, `8 64`.
- **Radius:** small and consistent: `--r-1 2px` (data blocks, ticks), `--r-2 4px` (chips, thumbnails), `--r-3 6px` (controls, panels). Avatars and dots use `--r-round`.
- **Borders:** `--bw 1px` hairlines. `--bw-strong 2px` is only for the playhead, the focus ring and the active-nav marker.
- **No elevation.** Popovers and tooltips are white with a `--line-strong` border, not a shadow. The lightbox uses a near-opaque white scrim, not a blur. An inset 1px rule may stand in for a border where adding a box would shift layout.

## Motion

- Transitions only on `color`, `background-color`, `border-color`, `outline-color`, `opacity` and underline colour. `--dur 140ms` (`--dur-fast 100ms`), with `--ease` set to `cubic-bezier(.2, 0, 0, 1)`.
- Never on hover: scale, lift, translate, bounce, glow, shadow or brightness filters.
- Allowed ambient motion: the live dot's slow pulse, the skeleton's breathing (opacity only), the spinner, and the 2s fade after a deep link lands on an event.
- `prefers-reduced-motion`: all durations go to 0, animations run once, and the spinner slows.

## Component patterns

- **Buttons:** default is white with a `--line-strong` border and ink text, and hovers to `--sunken`. *Primary* is a red fill with white text, and goes darker on hover and press. Use one primary per view. *Ghost* is borderless and is for toolbars and inline actions. Disabled buttons drop to 45% opacity. A disabled primary turns neutral grey, never pale pink.
- **Inputs:** white with a `--line-strong` border. Focus shows an ink border plus a 1px ink ring. Keyboard focus on everything else is a 2px red ring with a 2px offset.
- **Segmented control** (`.mode-toggle`): a hairline-bordered group. The active segment uses red-ink text on a red wash.
- **Chips:** 4px radius, `--sunken` fill. `.strong` uses ink on `--sunken-2` for emphasis. A toggle chip that is on (`.as-button.strong`) uses the red wash. Speaker chips show a palette dot and the name, and show a sunken background on hover.
- **List rows:** full-bleed rows separated by hairlines, with no cards. Hover is a `--sunken` background. Metadata is muted and 13px, and times are mono.
- **Timeline lanes:** a white canvas inside a hairline frame, with lanes divided by `--line-faint`. Blocks use a palette tint with a solid border. Unresolved voices use a dashed border. Conversations are `--sunken` bands. The playhead is a 2px red line with a red time tag. Tooltips are white with a hairline border.
- **Event list:** a 1px rail with 7px nodes: grey for speech, ink for a conversation, the media colour for photos and video, and red for the utterance under the playhead. The playing row also gets the red wash.
- **Lightboxes:** a white scrim at 97%. The image sits on the paper with a hairline-topped caption bar of ghost buttons below it.
- **Empty and loading states:** centred 13px muted text with one sentence that says what will appear. Skeletons are flat `--sunken` blocks shaped like the rows they replace. Errors use a danger wash banner.

## UX decisions

### App shell: left rail, not a top bar
The app has six destinations (Sessions, Search, Speakers, Voice map, Places, A/B). Six labels crowd a top bar and leave no room to grow. A 200px left rail lists them vertically and frees the full height for content. Account and logout sit at the bottom of the rail, out of the way. The voice map used to be a sub-tab under Speakers. It now has its own entry, because it is a different task (exploring a corpus, not naming people). Its URL (`/speakers?view=map`) is unchanged. Below 900px the rail becomes a sticky top bar with a sideways-scrolling nav, which works for one-handed thumb scrolling. A command palette was considered and deferred: with six destinations, plain links are faster to learn.

### Page head
Every page opens with the same head: a title, a one-line summary or count, and page-level actions on the right. This gives every screen the same entry point and a predictable home for actions like *split all live* and *clustering settings*, which used to float in ad-hoc toolbars.

### Sessions
A life log is browsed by day, so sessions are grouped under day headings (*Today*, *Yesterday*, *Fri, Nov 13*). Each row leads with its start time in mono, followed by the name and then duration and device. The date no longer repeats on every row. *Split all live* moved from the top of the list into the page actions, where it is visible but cannot be mistaken for a list row. A live session shows a red dot, the one red element on the page.

### Session view
Reviewing a session means four things at once: listening, reading, seeing what was there, and fixing who said what. The layout gives each a fixed place:
- **The player docks to the bottom edge** (as in a music app). It stays reachable while you read a long transcript and never pushes content down. It shows the session name on wide screens.
- **The header** shows a breadcrumb back to Sessions, the title, and quiet ghost actions (rename, split, A/B) on the right.
- **The timeline strip spans the full width** under the header. It is the session's overview and seek bar, and a wider time axis is the best use of a wide screen. Lane heads keep the speaker chips, so you can label a voice from the overview.
- **The transcript is the main column.** The video card stack stays in the transcript's left gutter, aligned to when each clip happened, and photo thumbnails stay inline at their moment.
- **A sticky right aside (320px) holds context:** the location map (opened from the strip's *map* chip), the session's sound tags, and a contact sheet of every photo that opens the lightbox. You can glance at where you were or what you saw without losing your place in the transcript.
- **Below 1100px** the aside folds into the column above the transcript (map, then sounds). The contact sheet is hidden there because the photos are already inline. On phones the strip, transcript and docked player stack, and the dock drops its title.

### Search
The page head explains the page in one line. The three modes are a segmented control directly above the query field, because they change what the field means. The field and the button stay sticky while you scroll results. Matches in snippets are bold with a red underline, not a highlighter block, so results stay readable.

### Speakers
The page head shows the voice count, and *clustering settings* is its action. The settings panel opens inline as a sunken well, not a floating card. Each voice is a hairline row: an avatar tinted with the voice's palette colour, the name, counts, then rename and merge. Expanding a row shows transcripts and voice-prints, switched by a segmented control, in a lighter nested list.

### Voice map
Gets the wide layout and its own nav entry. Projection views and filters sit in one toolbar above the plot. The inspector sits to the right of the plot on wide screens and below it on phones. Plotly is themed from tokens: a white plot, hairline grid, Plex labels and palette markers. Unassigned points are faint grey.

### Places
The travel map gets the wide layout, with a session legend in a right column that drops below the map on phones. Tracks use the palette colours and density uses the blue sequential ramp. The base map is no longer dimmed, because the light basemap now suits the theme.

### A/B
A page head explains the purpose. The wins table uses uppercase column labels and ink share bars. The voting view keeps its sticky toolbar. The focused card gets a red ring and the winning candidate a green wash, so the keyboard flow reads at a glance.

## Logo

The mark is a red glass orb on a white rounded tile (`frontend/public/logo.svg`). The lower half sags and leans slightly left, as if the glass has started to melt, and a soft red light gathers on the tile beneath it. It is the one deliberate exception to flatness: the mark carries depth so that the UI around it doesn't have to.

- The orb's base colour is the accent `--red` (`#E4002B`), deepening to `#7D0016` at the rim. Its highlights are restrained: a blurred top sheen at 32% white and a faint inner glow at the bottom.
- The wordmark is lower-case **context** in Plex Sans semibold, set beside the mark. The sidebar uses a 24 px mark and the login page a 32 px one. The favicon is the SVG itself.
- Never recolour the orb, flatten it into a plain dot, or place it on red.

## Brand layer (future site)

The public site imports the same `tokens.css` and adds a brand sheet on top of it. It does not fork the tokens.
- **Type:** headlines in IBM Plex Serif (`--font-serif`, installed only on the site) at `--fs-display-*`, with Plex Sans for body text at `--fs-lg` and line height 1.55, set to 60 to 70 characters per line.
- **Colour:** still mostly paper and ink. The site may use red more boldly than the app, for example a full-bleed red band or a large red numeral, but at most once per screen. Palette colours appear only in product imagery and diagrams.
- **Space:** the `--sp-6` to `--sp-8` steps become section rhythm, and the grid is 12 columns up to 1200px.
- **Imagery:** real product screenshots, framed by a hairline, never floating with a shadow.
- **Motion:** the same rules. A one-time fade-in for content is acceptable, but no parallax or scroll-jacking.
- **The mark:** the red dot before the wordmark carries over to the site. The site may enlarge it, but it is never redrawn as a logo with effects.
