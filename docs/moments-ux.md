# Moments: UX and the sessionless migration

## What changed in this pass

- **Moments is home (`/`).** The app opens on your days, not on an ingest log. `/moments/:id` is a moment's page. `/moments` and unknown routes redirect to `/`, and the logo links home.
- **Nav has two groups.** Primary: *Moments, Search, People, Places*, which are about your life. Under a quiet *Recordings & tools* label: *Recordings* (was Sessions), *Voice map* and *A/B*, which are raw material and model tooling. No route changed. Only labels and order did.
- **"Session" left the vocabulary.** Users now see *Recordings* in the nav, the page title, the breadcrumb and the empty states. *Speakers* is now *People*, because that page names people. Its count still says "voices", which is what it is counting.

## The feed (home)

- **Days are the top level.** Each day has a sticky heading (*Today · Sunday, November 15*) and a one-line digest built from its moments: how many, who was there, and where. It is a digest, not a count of sessions.
- **The slot for the day's story.** `DayHead` takes a `story` prop that renders as a lead paragraph (16px, 64ch) between the digest and the moments. When the per-day roll-up lands, pass its text in. No other layout change is needed.
- **One moment is one entry, not a card.** The entry shows the time and duration in mono, then the title (16px semibold), then *with Sam and Alex · at Sam's apartment*, a summary clamped to 3 lines, and a muted tally. Up to 4 cover images sit in a fixed 208×140 mosaic on the right (1 to 4 layouts). A moment without images is text only, so empty boxes never fill the space. The covers are what make the page something you want to open. The text makes it scannable. On phones the cover drops under the text at full width.
- **Hairline rows, hover is a colour change only,** as in every other list.
- **Gaps are not shown.** Time between moments is not drawn, because the owner asked the feed not to be about recording gaps.
- **Building state.** The feed polls every 30 s, along with the newest 10 recordings. If a recording is live, Today shows *● recording now on glasses: it becomes a moment a few minutes after recording stops*. The live dot is the page's only red. While moment jobs are queued or running (`building` on `GET /v1/moments`), Today shows a spinner and *Writing up a moment…*. Today's heading appears even before its first moment exists.
- **Empty and error states.** With no recordings at all, the page says how to start. With recordings but no moments, it says when moments appear and links to Recordings. A 404 from `/v1/moments` (an older server) gives a danger banner that links to Recordings. Other failures offer a retry. If a refresh fails after a good load, the page keeps what it has and shows a banner.
- **Jump to a day.** A date input in the page head sets `?day=YYYY-MM-DD`, and the feed loads with `before` set to the next midnight. *back to today* clears it. Older pages load with *show earlier days* (`next_before`).

## The moment page: showing the web of evidence

The agent's links are the new idea, and a node-link diagram would hide them, not explain them. The page keeps the content in readable columns and lets the links act on the content.

- **Head:** a breadcrumb, the title, the date, time span and duration, the summary at 16px, then a *People / Topics / Text seen* fact row as chips. People chips use the palette dot, as they do elsewhere.
- **Three columns (≥1100px):** *Heard* (utterances and sound spans), *Seen* (keyframes and photos as a 4:3 contact sheet), and a sticky 300px *Connections* aside.
- **At rest, the web is visible as label refs.** Each line or tile carries small mono tags such as `K2` and `P1`, one for each item it links to. A dashed tag means every link to that item is below 0.5 confidence. The tags work with touch and with no hover, and clicking one selects that item and scrolls to it.
- **Focus lights a neighbourhood.** Hovering or selecting any item dims everything outside its neighbourhood to 30% opacity. Linked items stay at full strength with a relation tag, phrased from their side ("K2 depicts U3" shows *depicts* on K2 when U3 is focused, and *depicted* on U3 when K2 is). This answers "what does this frame show being said" in one glance, with no lines drawn. Hovering a connection in the aside lights just its two ends.
- **The aside reads the graph as sentences:** *[thumb] K1 depicts Sam U1*, then the quote, the model's *why*, and the confidence. The sentences are grouped by relation (*Shows what was said*, *Said about what was seen*, *Text read*, *Same scene*). Selecting an item puts it at the top of the aside, with a larger preview and *listen in recording* / *open in recording*, and filters the sentences to that item's links. Esc clears the selection.
- **Coincidental links are hidden** behind *show N coincidental overlaps* at the end of the aside.
- **Neighbours:** *‹ earlier* and *later ›* sit beside the breadcrumb (`previous_id` / `next_id`). `j` or `←` goes earlier, and `k` or `→` goes later. Stepping replaces the history entry, so the breadcrumb and Back still return to the feed.
- **People are who was heard.** Feed lines and day digests use `speakers` (from transcripts, not the LLM): named people, then a count of unnamed voices. On the moment page each voice clustered to a speaker is a chip that links to its People entry (`/speakers?speaker=`), with its colour hashed from the speaker id as everywhere else. Unnamed voices get a dashed chip. Names the LLM gives that nobody heard (`people` minus `speakers`) show as quiet *Also mentioned* chips.
- **Provenance:** *Title, summary and connections written by `model`*. An agent wrote these claims, so the page says so.
- **Recordings stay one click away and out of the way:** *Built from N recordings* is a collapsed `<details>` at the foot of the page, with one row per recording (its first time, lines and images) that opens the session view at that point.
- **Narrow screens:** below 1100px the aside drops under the two columns, and the selected item docks to the bottom edge like the player. Below 700px *Heard* and *Seen* merge into one *As it happened* stream, where runs of images become inline grids between lines.

## IA: before, now and the end state

| | Before | This pass | End state |
|---|---|---|---|
| Home | Sessions list | **Moments** feed by day | Days → story of the day → moments |
| Playback | Session view only | Session view, reached from a moment's item or its *Built from* list | A moment (or a day) plays its own media across recordings |
| Sessions | Primary nav | *Recordings* under *Recordings & tools* | Gone from nav; an admin/debug view of ingest |
| Search | Hits open sessions | Hits open their moment (`/v1/moments/at`) with the matching item selected, falling back to the recording. Audio and transcript rows have a quiet *recording* button | Same, plus frame hits get the *recording* button |
| People | *Speakers* | Renamed *People* | A person page: moments with them, their voice and photos |
| Places | Travel map, tracks by session | Unchanged | Tracks by moment or day; clicking a track opens a moment |
| Voice map, A/B | Primary nav | Under tools | Tools, or an admin area |

### Where sessions still have to exist for now

- **Full audio and transcript playback.** The session view is the only player. Moments link into it with `?t=` at the right offset.
- **Split all live,** renaming, A/B voting and voice curation all act on sessions.
- **Places and Voice map** results still open sessions. Search goes to moments first.

### What should go later, in order

1. ~~Search → moments~~ (done. Frame hits still lack the secondary *recording* button.)
2. **A moment player.** Play a moment's audio across its recordings as one timeline, with photos inline. After that, the session view is no longer how users read a transcript.
3. **A day page** (`/days/YYYY-MM-DD`) carrying the story of the day. This needs an `after=` cursor, or neighbours on `MomentDetail`.
4. **People and Places pivot to moments** (moments with Sam, moments at the Co-op).
5. **Move Recordings out of nav** into an admin/tools area, together with split, A/B and the voice map.

## Deferred and open

- A search hit opens its moment with `?item=&s=&t=`. The page selects the exact artifact if it is in the moment, otherwise the nearest line or image from the same recording, and scrolls it into view.
- Full-size image viewing on a moment page uses the selected panel's preview, not a lightbox. That can be added if it is missed.
- Media auth is moving to a session cookie, so the token plumbing on these pages goes away with that change.
