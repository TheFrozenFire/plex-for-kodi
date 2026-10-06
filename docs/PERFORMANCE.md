# Performance notes

This is a read of the current tree (`addon.xml` version `1.14.1-beta1`), not of an older release that may still be installed. No fixes here. The cache and prefetch design at the bottom is not implemented.

## Why latency dominates

Local CPU on modest HTPC hardware is not what these screens are waiting on. The Plex Media Server is remote. On a ~170 ms RTT link, a request on an already open connection is about one round trip, and a new connection is several round trips (TCP, TLS, and the request). Server time is usually small next to that. At that cost, a screen that does four **serial** requests is about a second of dead UI before any image starts, and the images are more round trips. Compressing the body, or asking for JSON instead of XML, does not change this. Fewer serial requests does.

Two rules of thumb used below:

- **Serial** means the next request is not started until this response is parsed. Wall time is about `N * RTT` plus server time.
- **Parallel** means the requests overlap. Wall time is about the slowest one, but only up to the worker cap (3; see below).

Kodi image loads are a third category: they do not go through the Python session at all, so they are invisible to the `TIMING` hook.

## How a request is made

Almost every list and metadata fetch is `PlexServer.query` (`lib/_included_packages/plexnet/plexserver.py`). It builds the URL, logs it, and calls `self.session.get` on the calling thread. The session is a `requests_cache.CachedSession` (`asyncadapter.Session`) that **does not read the cache** unless the caller passed `with_cache=True`.

`HttpRequest` (`plexnet/http.py`) is the other client: reachability, plex.tv, the playback decision, timelines. Each instance constructs a new session, so those calls do not reuse the server's keep-alive connection.

Both end in `asyncadapter.Session.request`. With timing enabled (below), that one method logs the wall time, including cache hits.

The connect path in `AsyncConnectionMixin._connect` sleeps 10 ms between non-blocking `connect_ex` polls. That only matters on a **new** TCP connection. Keep-alive on `PlexServer.session` avoids it for later `query()` calls to the same host. `HttpRequest` never gets that benefit.

Background work uses `BGThreader` (`lib/backgroundthread.py`). The `worker_count` setting goes up to 32, but the pool is `min(setting, 3)` on Kodi 21 because `ENABLE_HIGH_CONCURRENCY` is false under the GIL (`lib/kodi_util.py`). Three in-flight PMS calls is the most the hub and library code will overlap.

## What is already cached

| Data | Cached? | Where | TTL | Invalidation |
|---|---|---|---|---|
| Home and library hubs (`/hubs`, `/hubs/sections/…`, `/hubs/continueWatching`) | **No.** `PlexServer.hubs` calls `query` without `cachable`. The settings text says hubs stay live on purpose. | — | In-memory `HomeWindow.sectionHubs` until 5 minutes (`HUBS_REFRESH_INTERVAL`) or a manual refresh. Process-local. | `tick`, wake, "Refresh hubs", section reload. |
| `/library/` and `/library/sections` | **No.** `PlexServer.library` re-queries `/library/` on every attribute access, then `Library.sections()` queries `/library/sections`. | — | — | — |
| `/playlists/all` | No. | — | — | — |
| Library page (`/library/sections/{id}/all`, `firstCharacter`, filters) | Only if `cache_requests` contains `libraries`. **Default is off** (`[]`). | sqlite `special://temp/pm4k_requests_cache` | `requests_cache_expiry` hours (default 72), hard cut, no stale-while-revalidate. | `LibrarySection.clearCache`. Also wiped when any movie/episode/show/season in that section is invalidated. |
| Item metadata (`/library/metadata/{id}`, seasons, episode pages, related) | Only if `cache_requests` contains `items`. **Default off.** | same sqlite | same 72 h hard expire | `PlexObject.clearCache`: the item, its season, its show, and the **entire library section**. `videoplayer.VideoPlayerWindow.videoWindowClosed` calls `video.clearCache()` when playback ends. Preplay's `onReInit` reloads with `skip_cache=True`. |
| Persist across Kodi restarts | Only if `persist_requests_cache` (**default off**). Otherwise `PlexInterface.shutdownCache` deletes the sqlite. A crash skips `last_shutdown_successful` and the next start deletes it too. | — | — | — |
| Show genres, remembered audio track, hub "seen" timestamps | Yes, small JSON. | `data_cache.json` via `lib/data_cache.py` | ~90 days since update, ~30 days since last access | Not HTTP. |
| plex.tv resource list | Last successful XML in the settings registry (`mpaResources`). Used only when the live `/pms/resources` fails. | Kodi addon settings | Until the next success | Account change refreshes. |
| Home users | Yes if `cache_home_users` (default on). | Account registry | Not refreshed automatically once any users are stored. | Sign-out / user change. |
| Images | Kodi's texture cache only, keyed by the full URL. | `special://thumbnails` | Until Kodi evicts it, or the URL changes (size, token, blur). | Not coordinated with the response cache. |
| Playback decision (`getServerDecision`) and `/:/timeline` | No, and they must stay uncached. A decision URL can start a transcode session. | — | — | — |

Turning the existing cache on is not the design below. It is opt-in, hubs are excluded, expiry is a hard miss, invalidation deletes the whole library, and `clearCache` runs `VACUUM` on the sqlite file while the UI is waiting.

## Flow inventory

Times are round trips to the **remote PMS** unless marked plex.tv or discover. "UI" means the Kodi GUI thread (a `@busy.dialog()` wrapper is still the GUI thread). "BG" means `BGThreader`.

### 1. Startup, until home rows appear

`default.py` → `lib/main.py` `main` / `_main` → `plex.init` → `HomeWindow.onFirstInit` → `serverRefresh` → `showSections`.

| # | Call | Code | Thread | Overlaps? | Cached today |
|---|---|---|---|---|---|
| 0 | Template compile, only if `theme_version` / resolution / template count changed | `templating.render.render_templates` | UI, progress dialog | no | generated XML on disk |
| 1 | `GET https://plex.tv/users/account` | `MyPlexAccount.verifyAccount` | BG, but `plex.init` **waits** on the `init` signal (plex.tv read timeout default 2 s, up to 3 tries) | no | account JSON in the registry; the HTTP call still happens |
| 2 | `GET https://plex.tv/pms/resources` | `MyPlexManager.refreshResources` | BG | after account | registry fallback only on failure |
| 3 | `GET /` on every server connection (LAN, plex.direct, relay, …) | `PlexConnection.testReachability` | BG, one thread each, **new session per probe** | yes, with each other | no. Timeout 2.5 s each |
| 4 | `GET https://discover.provider.plex.tv/library/sections/watchlist/all?…&X-Plex-Container-Size=0` | `WatchlistSection.__init__` via `showSections`, if `use_watchlist` (default on) and the row isn't hidden | **UI**, inside `@busy.dialog` `serverRefresh` | no | no |
| 5 | `GET /playlists/all` (no container size) | `PlexServer.playlists` | **UI**, same dialog | no, after 4 | no |
| 6 | `GET /library/` | `PlexServer.library` property | **UI** | no, after 5 | no, and not memoized |
| 7 | `GET /library/sections` | `Library.sections` | **UI** | no, after 6 | no |
| 8 | `GET /hubs?includeMarkers=1&count=10` and, if "new continue watching" is on, a second `GET /hubs/continueWatching` **after** the first returns | `PlexServer.hubs` in `SectionHubsTask` | BG | home is queued first, then one task per library, **3 at a time** | no |
| 9 | Same `/hubs/sections/{id}` for every other library | `SectionHubsTask` | BG | shares the 3 workers with home | no |
| 10 | Kodi fetches `/photo/:/transcode?…` for every poster and art the skin paints | `asTranscodedImageURL` on each `ManagedListItem` | Kodi texture thread, not Python | parallel, separate pool | Kodi texture cache only |

`serverRefresh` returns after step 7. The spinner covers 4–7, which are serial. Hub rows are filled later from `sectionHubsCallback` when step 8 finishes. If the home screen has cross-section hubs configured, `sectionHubsCallback` **holds the home draw** until every library task in step 9 has finished (`_pendingLibrarySections`).

`checkPlexDirectHosts` runs on the UI thread from `onFirstInit` before `serverRefresh`. With `handle_plexdirect` not `never` it only writes `advancedsettings.xml` when the host list changed; it is not a PMS round trip, but a dialog there blocks startup.

Not on the startup path: `DiscoverHubsTask` / `_discoverHubsSync` run when the user opens Manage Hubs, and they fetch hubs for every section **again**, serially, inside one task.

### 2. Opening a library

`opener.sectionClicked` → `LibraryWindow.onFirstInit` → `fill` → `fillShows` (`@busy.dialog`).

| # | Call | Code | Thread | Notes |
|---|---|---|---|---|
| 1a | `GET /library/sections/{id}/firstCharacter` | `LibrarySection.jumpList`, when sorted by title (the usual case) | **UI** | Also reads `section.settings`, which is `GET …/prefs` the first time |
| 1b | or `GET …/all` with container size 0 | `LibrarySection.all(0, 0, …)` for non-title sorts, folders, episodes | **UI** | A count query; the body is empty-ish but the RTT is real |
| 2 | `GET …/all?X-Plex-Container-Start=0&X-Plex-Container-Size={chunk}` | `ChunkRequestTask` | BG, but the UI is already past the spinner only after step 1 | Default chunk `library_chunk_size` 240 (100 for mixed movies/shows). Further chunks wait until the user scrolls **unless** `retrieve_all_media_up_front` (default off) |
| 3 | Transcode URLs for the visible posters | `fill` list items | Kodi | Same as home images |

Step 1 blocks the spinner. Placeholders are built locally after that (`CreateDefaultItemsTask` does no I/O). Real titles and thumbs arrive after step 2. `library` responses are cacheable only when the libraries cache is switched on, and this path still pays the RTT on a cold cache.

### 3. Show → seasons

`opener.showClicked` → `ShowWindow.onFirstInit` → `setup`.

| # | Call | Code | Thread | Notes |
|---|---|---|---|---|
| 1 | `GET /library/metadata/{show}?includeExtras=1&includeExtrasCount=10&includeOnDeck=1` | `ShowWindow.setup` → `mediaItem.reload` | **UI** | Full object. Not inside `@busy.dialog`, so the window is up but empty |
| 2 | `GET /library/metadata/{show}/similar?count=36` with container size 0 | `RelatedMixin.relatedCount`, while constructing `RelatedPaginator` | **UI**, after 1 | Only to learn `totalSize`. The items are thrown away |
| 3 | `GET` the show's children (seasons) | `Show.seasons` via `fillSeasons` | BG (`batch_simple`) | Parallel with 4, subject to the 3-worker cap |
| 4 | `GET …/similar` again, container size 8 | `fillRelated` → `relatedPaginator.paginate` | BG | Second similar-items round trip. Extras and roles reuse the body from step 1 |
| 5 | `GET https://discover…/library/metadata/{guid}/userState` | `checkIsWatchlisted` | BG | Not the PMS. Skipped if watchlist is disabled |
| 6 | Season poster transcodes | `SeasonsMixin._createListItem` | Kodi | |

Steps 1 and 2 are serial on the GUI thread. Opening the show you just saw as a poster still re-fetches it; the hub payload is not reused as the full object (`isFullObject()` is false for hub leaves).

### 4. Season → episodes

`opener.seasonClicked` → `EpisodesWindow._onFirstInit` (`@busy.dialog(delay_time=2.5)`) → `_setup`.

| # | Call | Code | Thread | Notes |
|---|---|---|---|---|
| 1 | `GET /library/metadata/{season or show}?checkFiles=1&includeExtras=1&includeExtrasCount=10&includeChapters=1` | `EpisodesWindow._setup` | **UI** | `checkFiles=1` makes PMS stat the files |
| 2 | `GET` episode page, container size ~18–26 (`EpisodesPaginator.initialPageSize` 18 plus orphans) | `Season.episodes` / `Show.episodes` | **UI** via `fillEpisodes` | If a specific episode index is not in that slice, `initialPage` **loops**, doubling the window and requesting again, serially, until the episode appears or the season is exhausted |
| 3 | `GET /library/metadata/{current episode}?checkFiles=1&includeChapters=1` | `reloadItems` | **UI** | The focused row is reloaded on the GUI thread before the list is interactive |
| 4 | `GET /library/metadata/{id,id,id,…}?checkFiles=1&includeChapters=1&includeMarkers=1` | `EpisodesReloadTask` | BG | The rest of the page is one batched request. This part is already in good shape |
| 5 | Seasons list, extras, related count + related page, roles | `batch_simple` of `fillSeasons`, `fillExtras`, `fillRelated`, `fillRoles` | BG | Seasons are fetched **again** even if the show window just did it. Related is another size-0 request plus a page, same as the show |
| 6 | Episode thumbs | list items | Kodi | |

Coming from the show window does not pass the season list or the show body down. The episode screen starts cold.

### 5. Item detail (preplay)

`PrePlayWindow.setup` is `@busy.dialog`.

| # | Call | Code | Thread | Notes |
|---|---|---|---|---|
| 1 | `GET /library/metadata/{id}?checkFiles=1&includeExtras=1&includeExtrasCount=10&includeChapters=1&includeReviews=1` | `video.reload` | **UI** | Same `checkFiles` stat |
| 2 | `GET …/similar?count=36` size 0 | `relatedCount` | **UI**, after 1 | Same wasted count round trip |
| 3 | Roles, reviews, extras (local if step 1 returned them), related page, and for movies `GET /library/sections/{id}/collections` | `batch_simple` | BG | Collections is a section-wide list so the movie's collection tags can be matched by title |
| 4 | Watchlist userState | `checkIsWatchlisted` | BG | discover, not PMS |
| 5 | Clear logo, fanart, thumb transcodes | `setInfo` | Kodi | `clearLogoFrom` is another transcode URL |

Played from a hub with auto-resume (`home_inprogress_resume`), `doAutoPlay` reloads **again** (`checkFiles=1` plus `VIDEO_RELOAD_KW`) before `playVideo`.

### 6. Starting playback

`videoplayer.play` → `PLAYER.playVideo` → `_playVideo`.

| # | Call | Code | Thread | Notes |
|---|---|---|---|---|
| 0 | Local media decision | `PlexPlayer.build` | UI | No network if the item already has `Media` from the detail reload |
| 1 | `GET` the decision path (`directPlay`, stream params, …) | `PlexPlayer.getServerDecision` → `PlexRequest.getWithTimeout` | **UI** | One RTT that must stay live. Uses a **new** session, so add a TCP+TLS handshake on top of the RTT. Must not be cached |
| 2 | Kodi opens the stream URL | player | Kodi | The byte stream, not an API call. Direct play still waits on the first bytes from the remote server |
| 3 | BIF index URL (`getBifUrl`) if the part is indexed | player | UI to build the URL; Kodi fetches | Seek-preview images, more transcode-like GETs |
| 4 | `GET /:/timeline` about every 10 s | `NowPlayingManager.sendTimelineToServer` from the cron tick | BG, async `HttpRequest`, new session each time | Not on the critical path to the first frame. Do not cache |

Before step 1 the window may also wait on theme-music fade (`stopAndWait`) and up to a few 100 ms polls. That is local, and it sits in front of the decision RTT.

### 7. Returning from playback

| What | Code | Effect on the next paint |
|---|---|---|
| `video.clearCache()` | `VideoPlayerWindow.videoWindowClosed` | Deletes the item, season, show, and **library section** response cache, then `VACUUM`. If the cache was on, the next open is cold and the vacuum is on the way |
| `reload(..., skip_cache=True)` | `PrePlayWindow.onReInit` | The detail screen, which is what you land on, ignores any surviving cache and does steps 1–2 of the detail flow again on the UI thread |
| Home `onReInit` | `HomeWindow.onReInit` | If the section's hubs are older than 5 minutes, `showHubs(update=True)` refetches them. Otherwise it updates on-deck hubs (`UpdateHubTask`, one `hub.reload` each). `sectionChanged` also waits **0.5 s** on purpose, and up to 2 s if hub tasks are still running, before it will draw the section you moved to |

## Ranked suspects

Ordered by how much of the wait is `RTT * serial calls` on the paths above. Local CPU and the unconditional `LOG` of each URL are real but small next to a high-latency round trip.

1. **Home does not appear until a chain of uncached serial requests finishes, and hubs are never cached.** Steps 4–7 of startup are on the GUI thread, one after another (watchlist probe, full playlist list, `/library/`, `/library/sections`). Only then do hub requests start, three at a time, and the home `/hubs` body is fat (`includeMarkers=1`, `count=10`, every hub the server returns, not just the rows on screen). A second continue-watching call is serial behind the first. Cross-section hubs wait for every library. None of this is in the response cache, and it is repeated after 5 minutes, on wake, and whenever `sectionHubs` is dropped. On a ~170 ms RTT link this chain is on the order of a second before hub XML is even parsed. The home hub costs more than one round trip because the body is large. Then the posters start.

2. **Opening a show, a season, or a detail screen re-walks the same metadata in serial GUI-thread requests, and the item cache does not help by default.** Each of those screens starts with a full `reload` (often `checkFiles=1`, which adds a server-side stat) and then a related-items request whose only purpose is `totalSize`. The season screen can multiply the episode-page request in a doubling loop, then reload the focused episode again before the user can click. The show's season list is fetched a second time on the episode screen. The existing item cache is off, excludes nothing useful when on, is deleted for the whole library when playback ends, and is explicitly skipped when preplay returns. This is the "click a poster, wait, click a season, wait, click an episode, wait" feeling.

3. **A library view blocks on a count or A–Z index request before it asks for any posters, then blocks the first paint of real items on a second request.** `jumpList` or `all(0, 0)` is one RTT on the UI thread; the first chunk (240 items by default, or the first letter) is the next. Titles you just saw on a hub are not reused. Library caching exists but is off, and a later watch-state change throws the whole section away.

4. **Every new poster, episode thumb, fanart, and clear logo is a `/photo/:/transcode` GET from Kodi to the remote PMS, outside the Python connection pool, with no prefetch of the next row.** The URL is built in `PlexValue.asTranscodedImageURL` → `PlexServer.getImageTranscodeURL`. Kodi fetches those itself, so they do not appear in `TIMING` and they do not reuse `PlexServer.session`. The first visit to a screen pays RTT plus transcode time per visible image (Kodi overlaps them; PMS may still queue). Repeat visits are cheap only while the texture cache holds that exact URL. `poster_resolution_scale_perc` and `background_resolution_scale_perc` (default 100) scale those dimensions up and change the cache key.

5. **Calls that already have to hit the server still pay a fresh handshake, and a few local waits sit in front of them.** The playback decision, every timeline post, every reachability probe, and every plex.tv call construct a new `HttpRequest` session (`http.HttpRequest.__init__`). The first byte is RTT plus TLS, not RTT. `AsyncConnectionMixin._connect` also sleeps 10 ms per poll while that handshake is in progress. Separately, `HomeWindow.sectionChanged` always waits 0.5 s (and up to 2 s if hub tasks are outstanding) before it will switch sections, even when `sectionHubs` already has the data. Theme-music stop-and-wait sits in front of the decision request. These do not multiply like suspect 1, but they add a fixed tax to every play and every section change.

Not the main event, listed so they are not rediscovered:

- `util.LOG` of every URL in `query` and `HttpRequest.logRequest` is unconditional. It is cheap next to a round trip, but it is noisy while reading a timing log. Don't add more unconditional logs there.
- `clearCache` → sqlite `VACUUM` will matter if the cache is turned on naively and then invalidated on every playback end.
- `DiscoverHubsTask` refetches every section serially when Manage Hubs is opened.
- Worker cap of 3. Overlapping a few hub requests helps. Raising the pool much further barely shortens the same set of requests. The GIL is released inside the socket read, and that is not the lever. Delete serial calls instead.
- Fetching an original-size poster instead of the transcoded cell. The original is far larger and slower, especially when the server has not already scaled that image.

## Collecting a session

Link RTT is measured separately. Timing is off unless you turn it on, and every hook returns immediately when it is off.

**Turn it on** for one session, then off. Any of:

- start Kodi with `PM4K_TIMING=1` (`PM4K_TIMING=0` forces it off even if the setting is on)
- set the hidden addon setting `timing_log` to `true` (it is not shown in the UI; restart the add-on afterwards)
- write `1` into `pm4k_timing` in the add-on profile (`special://profile/addon_data/script.plexmod/pm4k_timing`)

Then walk the slow paths once: cold start until the home rows are up, open a show, open a season, open a movie or episode detail, start playback and wait for the picture, stop, go back to the previous screen, open a library and scroll until the next page of posters loads. Quit Kodi (or turn timing off) so the art watcher flushes.

Grep the Kodi log for `TIMING`. Hosts other than `*.plex.tv` are logged as `{server}`. Numeric ids, hex catalog ids, and token query values are redacted in every field, including `thread` (a request thread can be named with the full URL). A hook that fails is logged and swallowed so it cannot break drawing or playback. `playback.start` covers both a single item and a playlist (a season with more than one episode). It stays the current span for requests on other threads until the first frame. Summarize the file from a checkout of this tree:

```sh
python3 tools/parse_timings.py /path/to/kodi.log
```

Lines:

| Prefix | Meaning |
|---|---|
| `TIMING SPAN ... phase=begin` | A transition started. `id` is the span. Requests logged while it is current carry the same `span`. |
| `phase=first` | Time from the start of that transition to the first usable paint (section list, title and poster, episode list, placeholders, or the playing state). |
| `phase=full` | Time until that transition's load finished. For a show, season, or detail screen this waits until the background fill tasks return, not merely until they are queued. |
| `TIMING REQ` | One HTTP call from `asyncadapter.Session.request` (both `PlexServer.query` and `HttpRequest`). `ttfb_ms` is until headers, `total_ms` until the body is in memory, `cache` is `hit` or `miss`, `ui_blocked=1` means the call ran on the UI thread. |
| `TIMING ART` | An image URL was handed to a list item. `cache=hit` means Kodi's texture database already had it. `cache=miss` is the wait until it shows up. `cache=timeout` means it was still absent after 20 s (often an off-screen image Kodi never requested). |
| `TIMING PLAY` | Playback phases on the `playback.start` span: `decision` (`mode=direct` or `mode=transcode`), `markers`, `stream_open` (just before `Player.play`), `playing`, `subtitles` (the handler's subtitle and now-playing update), `first_frame` (`onAVStarted`). `ms` is cumulative from the play action. |

Span names: `home.load`, `home.hubs`, `open.show`, `open.season`, `open.detail`, `library.open`, `library.page`, `playback.start`, `return.home`, `return.season`, `return.detail`.

`return.*` logs `first` and `full` together: the screen was already up, and both numbers are how long the reinit reload took. `library.open` logs `first` when the placeholder grid is focused and `full` when the first real chunk has been painted. Later chunks are `library.page`.

Artwork is not proxied. A local proxy would change the URL Kodi caches, so the measurement would not match a normal visit, and it would be a new thing that can fail. Instead, when timing is on, setting an `http` image URL records the time, and a daemon thread checks Kodi's `Textures13.db` (read-only, short timeout) and, if needed, `xbmc.getCacheThumbName`. When timing is off that thread is not started and the database is not opened.

Do not leave `PM4K_TIMING` on day to day. `PlexServer.query` still logs the URL at info before the call; those lines have no duration. Prefer `TIMING`.

### Is a full mirror feasible?

`tools/crawl_feasibility.py` walks a server without going through Kodi. It is serial, waits so it stays under `--rate` requests per second (default 2), and stops at `--max-requests` (default 2000). The token is read from the environment and sent as a header. The report prints counts and byte totals, not names.

```sh
PLEX_URL=http://SERVER:32400 PLEX_TOKEN=... python3 tools/crawl_feasibility.py --scope dashboard
PLEX_URL=http://SERVER:32400 PLEX_TOKEN=... python3 tools/crawl_feasibility.py --scope full --rate 2 --max-requests 2000
PLEX_URL=http://SERVER:32400 PLEX_TOKEN=... python3 tools/crawl_feasibility.py --scope dashboard --fetch-art
```

`--scope dashboard` is the first page of each library, then one page of seasons and one page of episodes for the shows on that page. That estimates "prefetch what the home rows can reach", not a literal copy of the hub payload. `--scope full` pages through every item and its children. `--fetch-art` also GETs the transcode URLs at 244x361 and 532x299 (the home poster and 16:9 cells at the default scale) and adds those bytes. Without it, `art bytes` stays 0 and `art urls` is only a count.

Keeping a mirror current later, without another full walk:

- Items carry `updatedAt` (unix seconds). `GET /library/sections/<id>/all?updatedAt>>=<unix>` is the Plex "greater than or equal" filter and returns what changed.
- `GET /library/recentlyAdded` is a short list, not a delta of the whole server.
- `/:/websockets/notifications` pushes `library.new`, `library.on.deck`, `timeline`, and `activity`. Treat those as hints to refetch the rating keys they name. Use `updatedAt` after the socket drops.

## Cache and prefetch design

Not implemented. The aim is to make a repeat visit, and the *next* click, cost zero PMS round trips, without serving a stale watch-state forever and without caching anything that starts playback.

### Incremental roadmap

Build this in order. Stop after each step and compare a timing log. The order is the one that moves the waits on a ~170 ms RTT link. `cache_requests` is empty by default, so today every one of those round trips is paid again.

What that link feels like, with the connection already open: home is a few seconds before posters. Opening a show or a movie is about a second of serial metadata before the screen can fill, and a show is several seconds once episode thumbnails are included. A season is at least that, because its detail, the count-only similar call, the episode page, and the batch reload run one after another. A library grid's metadata is under a second; the posters on a first visit add a couple of seconds. Starting playback is several seconds.

1. **Persistent response cache, on for navigation, hubs, lists, and items.** This is the repeat-visit win. Use the stale-while-revalidate policy below, include hubs, and keep the file in the profile directory so it survives restart. Do not cache decisions, timelines, or transcode URLs. The current `cache_requests` checkboxes stay as a kill switch. The library page the user just opened is the same cache: leaving and coming back should not repeat `library.open`.
2. **Drop the count-only similar request.** Done, behind `defer_related_count` (default on). Show, season, and detail screens no longer call `relatedCount` before they paint. The related hub learns `totalSize` from the page `fillRelated` already fetches. Turn the setting off to restore the count-only request.
3. **Collapse serial reloads.** Done for the season screen, behind `fast_season_paint` (default on). A show already in hand is not reloaded, the season is not reloaded with `checkFiles` before the list draws, and the focused episode is not reloaded on the UI thread. The episode page is the read that paints. `checkFiles` stays on playback. Turn the setting off to restore the serial reloads.
4. **Prefetch detail and children for what is already on screen.** When the home rows are painted, store the metadata those posters need (the show or movie, its seasons, and the first episode page) in the cache from step 1. Also prefetch the focused item's next click, and cancel it when focus moves. The click then paints from cache. Art for those URLs is warmed by setting the image URL early enough that Kodi fetches it before the click. Do not build a second image store.
5. **Keep the PMS connection warm.** After idle, the next click should not pay a fresh handshake. One shared session per origin, and a cheap touch of the open connection before the user acts. `HttpRequest` must stop building a new session for the decision and for timelines, or those calls pay for a new connection every time.
6. **Prefetch posters for the next chunk.** When a library or hub page is shown, set the transcode URLs for the next off-screen page so the texture cache is warm before the scroll. Ask for the cell size the UI uses, not the original file.
7. **Skip connections that do not answer.** Done, behind `skip_dead_connections` (default on). An address already marked unreachable is not probed again, so it does not wait out the reachability timeout. A new connection object (a changed server list) starts unknown and is tested. Turn the setting off to probe every address every time.
8. **Prefetch the next posters.** Done, behind `prefetch_art` (default on). A few background threads download the displayed-size transcode URL for the rest of the current hub row, the season posters past the first screen, and the focused show's first episode page. List items use the file when it is already on disk. Kodi still fetches an image itself the first time it is on screen.

An artwork `timeout` means that after 20s the URL was not in `Textures13.db` and not under `special://thumbnails`. Two things produce that. Kodi does not fetch art for a list item it has not rendered, so off-screen thumbs sit until the cap. The file check also used to look in the process working directory instead of `special://thumbnails`, so a cached image could be reported as a timeout. The lookup now uses the thumbnails directory.

9. **Prepare playback for the focused episode.** Done, behind `fast_playback_start` (default on). While an episode is focused, one background thread reloads its metadata (chapters and markers) and asks the server for the playback decision. The click reuses that decision when it is still for the same item and offset, and skips the second metadata reload. A seek does not reuse it. If a video is already playing, the decision request runs while that playback stops. An idle player has nothing to stop, so the request stays on the caller. The list item is given the container type (`setMimeType` and `setContentLookup(False)`) so Kodi does not probe the remote file. Markers are already on the episode after that reload; the `markers` playback phase is the local intro check and player stop, which need the decision's metadata, so they are not a second request to run beside the decision.

The wait from `playing` to `first_frame` is Kodi opening the stream. The add-on does not control Kodi's cache. For a large remote file, `advancedsettings.xml` (a Kodi file, not an add-on setting) is the lever:

```xml
<advancedsettings>
  <cache>
    <buffermode>1</buffermode>
    <memorysize>139460608</memorysize>
    <readfactor>20</readfactor>
  </cache>
</advancedsettings>
```

`buffermode` 1 caches network files. `memorysize` is bytes of buffer (the example is about 133 MB, enough for a few seconds of a high-bitrate file). `readfactor` is how much faster than realtime Kodi fills that buffer. Raise `memorysize` for a high-bitrate direct play; a buffer smaller than a couple of seconds of the file is what makes the first frame wait. This file is edited by hand in Kodi's userdata folder. The add-on does not write it.
10. **A wider mirror only if the crawl says so.** If `tools/crawl_feasibility.py --scope full` is a modest number of requests and bytes, extend step 4 from visible rows to the libraries those rows came from, using `updatedAt` and the notifications socket. If the crawl is large, stop at the steps above.

More workers are not a step. Overlapping the few hub requests that already run together helps. Adding many more in-flight calls barely shortens that same set. Delete serial calls first.

The notes below are the policy for that cache. They are not a reason to build all of it at once.

### Response cache

Keep the sqlite file (it is already there) but stop using it as a hard expire that is off by default and blind to hubs. Replace the policy:

**Stale-while-revalidate, per class.** On a hit younger than the soft TTL, return the body and do not call the server. Between soft and hard TTL, return the body immediately and refresh on a worker; if the new body differs, update the list in place. Past the hard TTL, or on a miss, block (today's behaviour). A failed revalidation keeps the stale body.

| Class | Examples | Soft | Hard | May be shown stale? |
|---|---|---|---|---|
| `nav` | `/library/`, `/library/sections`, section prefs | 1 hour | 7 days | yes |
| `hub` | `/hubs`, `/hubs/sections/{id}`, `/hubs/continueWatching` | 45 s | 24 hours | yes, for the first paint; refresh behind the rows |
| `list` | `…/all`, `firstCharacter`, `collections`, episode page, seasons | 2 min | 7 days | yes |
| `item` | `/library/metadata/{id}` and `…/children` | 10 min | 7 days | yes, except `viewOffset` / `viewCount` / `updatedAt` which revalidate sooner (soft 0, so the next open refreshes, but still paints from cache) |
| `related` | `…/similar`, `…/related` | 1 day | 7 days | yes |

Never store: `POST`/`PUT`/`DELETE`, `/:/timeline`, `/:/scrobble`, `/:/progress`, decision and transcode URLs, `GET /` reachability, plex.tv account and resources (they already have their own fallback).

Key by method + path + query **without** `X-Plex-Token`, scoped by server uuid and account id (the same pair `getRCBaseKey` already builds). Store the raw XML bytes; parsing can stay on the caller.

Invalidation, narrower than today:

- Watch, unwatch, or a timeline that changed `viewOffset`: drop that item's `item` entry and patch the row. Do **not** drop the library list or the hub body; patch `viewCount` / `viewOffset` on the cached XML or mark just that rating key dirty.
- Playback end: mark the item dirty. Do not `clearCache()` the section, and do not `VACUUM` on the UI thread. Vacuum on a timer if at all.
- Manual "clear cache" and server-changed `updatedAt` (the hub code already compares `addedAt`/`updatedAt` in `HomeWindow._showHub`) stay as the escape hatches.
- Default this **on** for `nav`, `hub`, `list`, and `item`. The current `cache_requests` checkboxes can stay as a kill switch.

`persist_requests_cache` semantics: the new cache lives in the profile directory, not `special://temp`, and survives restarts. The "last shutdown was clean" wipe goes away; hard TTL covers a crashed exit.

### What to stop requesting

- Memoize `PlexServer.library` and `Library.sections()` for the `nav` TTL. `showSections` should be one sections request, not `/library/` plus `/library/sections`.
- The size-0 `similar` request is no longer on the paint path (`defer_related_count`, default on). The related hub uses `totalSize` from the page it fetches.
- `EpisodesPaginator.initialPage`: one request with `X-Plex-Container-Start` set from the episode index, not a doubling loop of ever-larger pages.
- `checkFiles=1` only when starting playback or when a path-mapping check needs the file, not on every detail open.
- `includeMarkers=1` only on the continue-watching hub, not on every `/hubs` response.
- `excludeFields=summary` on hub and poster lists (the watchlist code in `myplexserver.py` already does this). Add `excludeElements` for streams and markers on list calls that only paint a poster and a title. Keep the full body for the item that is actually focused.
- `/playlists/all` and the watchlist size-0 probe should not be on the critical path in front of `/hubs`. Run them on a worker after the home hub task is queued, or cache them as `nav`.

### Prefetch

When an item **receives focus**, queue (do not wait for) the request the next click would make, into the same cache:

| Focus | Prefetch |
|---|---|
| Hub or library poster that is a show | `GET /library/metadata/{id}?includeExtras=1&includeExtrasCount=10&includeOnDeck=1` and the seasons key |
| Season poster | first episode page (`Container-Size` 26) and that season's metadata **without** `checkFiles` |
| Episode row | that episode's metadata with chapters, the URL preplay's `reload` uses |
| Movie poster | the preplay `reload` URL, minus `checkFiles` |
| Library list within ~20 items of a chunk boundary | the next `ChunkRequestTask` (today this waits for the boundary) |
| Home, once the visible hub rows are drawn | the continue-watching metadata for the focused item only, plus transcode URLs for the **next** off-screen hub page |

Cancel the prefetch when focus moves (`Task.cancel` already exists). Cap prefetches at the same 3–4 in-flight requests so they do not queue behind the thing the user just clicked; `addTasksToFront` is the wrong call for a prefetch.

### Parallelism

- `showSections` steps 4–7 become one worker batch. The home `SectionHubsTask` is queued **first**, before the function waits, so `/hubs` overlaps `/library/sections` instead of following it.
- Inside `PlexServer.hubs`, `/hubs` and `/hubs/continueWatching` run together, not sequentially.
- `ShowWindow.setup` and `PrePlayWindow.setup` no longer call `relatedCount` on the GUI thread (`defer_related_count`, default on). The season screen's extra reloads are also off that path (`fast_season_paint`, default on).
- The worker cap of 3 is about the GIL, but these tasks block on sockets. Overlapping the requests that are already in flight is enough. Raising the pool much further does not pay for itself on a ~170 ms RTT link; the serial chains above dominate. Do not raise it by flipping `ENABLE_HIGH_CONCURRENCY` globally; that comment is about CPU-bound work.

### Connections

- One `Session` per origin (PMS active connection, plex.tv, discover), shared by `PlexServer.query` and `HttpRequest`. The decision and the timeline then ride the connection `query` already warmed. After the connection has gone idle, touch it before the next click so that click does not pay for a new handshake.
- An address that has already failed reachability is not probed again (`skip_dead_connections`, default on). A new connection object, from a changed server list, starts unknown and is tested. Turn the setting off to probe every address every time.
- Replace the 10 ms `sleep` in `_connect` with `select`/`poll` on the socket. Correctness stays (the connect is still non-blocking and cancellable); new connections lose the extra tick.
- Keep the transcode host stable (same IP the python client uses, not a plex.direct name Kodi cannot resolve) so Kodi's own curl pool can reuse connections too. The advancedsettings mapping already exists for this; image URLs should prefer that mapped address.

### Images

Do not proxy images through the response cache; Kodi's texture cache is the right store. Do:

- Prefetch the next page's transcode URLs by assigning them to list items early (Kodi starts the texture job when the URL is set, which already happens for built items; the missing part is building the next page's items before the user scrolls). On a first visit those images, not the metadata, are what make the grid feel slow.
- Keep dimensions stable. `poster_resolution_scale_perc` above 100 multiplies bytes and busts the texture cache for no gain on a 1080p display. Do not fall back to the original file; it is much larger and slower than the transcoded cell, and slower still when the server has to scale it on the spot.
- `excludeFields` does not shrink JPEGs. The transcode `width`/`height` does. The poster cells already request a specific size via `scaleResolution`; leave that, and avoid a second URL for the same image at a different size (background vs thumb) when the screen does not need both immediately.

### What we would not do

- A rewrite of plexnet, or a second HTTP stack.
- Caching the decision response or the timeline.
- Bumping `addon.xml` version. The persistent cache in step 1 is the piece to default on, and it keeps a kill switch. Confirm that default with a timing log before it is what a daily session runs. Do not default the wider mirror on.
- Raising the worker pool, or changing the response format, as a way to make screens faster.
