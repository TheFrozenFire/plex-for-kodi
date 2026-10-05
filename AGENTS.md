# AGENTS.md

Notes for coding agents working on this fork of [PlexMod for Kodi](https://github.com/pannal/plex-for-kodi) (`script.plexmod`). PRs land on `develop_kodi21` here, not on upstream. The tree is deployed by syncing it into Kodi's add-on directory on an HTPC and restarting Kodi there. You cannot run Kodi in this environment.

Goal of the fork: make the add-on less sluggish and smooth rough edges. Do not rewrite it. Keep diffs easy to rebase onto upstream `pannal/plex-for-kodi`.

## What runs where

Kodi 21 on modest HTPC hardware, typically a 1080p display. The Plex Media Server is remote, on a high-latency link (plan on a ~170 ms RTT). That latency, not local CPU, is the working theory for sluggishness. See `docs/PERFORMANCE.md`.

The add-on id is `script.plexmod`. Kodi loads it as four extension points declared in `addon.xml`:

| Entry | Kodi point | Role |
|---|---|---|
| `default.py` | `xbmc.python.script` | The UI. Single-instance guard, optional kiosk boot delay, then `lib.main.main()`. |
| `service.py` | `xbmc.service` | Long-running service. Kiosk auto-start and the self-updater loop (`lib/service_runner.py`, `lib/update_checker.py`). Reloads itself in-process after an update. |
| `plugin.py` | `xbmc.python.pluginsource` | Stub so "My add-ons" / favourites can launch the script. Runs `RunScript(script.plexmod,fromplugin)` unless the argument is `stub`. |
| `screensaver.py` | `xbmc.ui.screensaver` | Photo screensaver. Calls `plex.init()` and opens the slideshow window. |

`default.py` exits immediately if another instance is already running (`global property running`) or still waiting out the kiosk delay. A second launch asks the running instance to restore.

## Layout

```
addon.xml                 version, dependencies, <news> blurb the updater shows
changelog.txt             long human history, not read by the updater
default.py service.py plugin.py screensaver.py
lib/                      add-on code
  main.py                 startup, template render, account loop, shutdown
  plex.py                 PlexInterface: Kodi side of plexnet (settings, UA, cache lifecycle)
  windows/                one class per screen, plus mixins
  templating/             compiles *.xml.tpl into the skin XML Kodi actually loads
  _included_packages/     vendored plexnet, requests_cache, signalslot, tendo
resources/settings.xml    Kodi settings (version="1" schema)
resources/language/       strings.po
resources/skins/Main/     skin.xml + 1080i/templates/*.xml.tpl
                          1080i/*.xml is generated and gitignored
tests/                    offline harness (see below)
docs/                     notes that are not the in-app changelog
```

`lib/__init__.py` puts `lib/_included_packages` on `sys.path`, which is why code does `import plexnet` and `import requests_cache` rather than a package-relative import. Vendored plexnet is the Plex client. Treat it as library code you may patch, but keep the patch small: it is the rebase hotspot.

Runtime dependencies (not vendored): `requests`, `six`, `kodi-six` (`xbmc`, `xbmcgui`, `xbmcaddon`, `xbmcvfs`). Declared in `addon.xml`.

## Windows and skins

A screen is a Python class in `lib/windows/` subclassing `kodigui.BaseWindow` (a `xbmcgui.WindowXML`). The class attributes are the Kodi window constructor:

- `xmlFile` — `script-plex-*.xml` under the skin
- `path` — `util.ADDON.getAddonInfo('path')` (the add-on directory)
- `theme` — `'Main'`
- `res` — `'1080i'`

Those XML files are not in git. `lib/templating/render.py` `render_templates()` compiles `resources/skins/Main/1080i/templates/*.xml.tpl` into `1080i/*.xml` on startup when `theme_version` (`util.THEME_VERSION`), resolution, or the template/xml counts change. `main()` calls it before the window loop, inside a `GlobalProperty('rendering')` block, with a progress dialog. A broken generated XML file is detected in `XMLBase.onInit` (missing control 666) and triggers a forced recompile.

Python talks to the skin the Kodi way:

- `getControl(id)` / `setFocusId` / `setProperty` on the window. The skin binds labels and visibility to `Window.Property(...)` and `Control.IsVisible`.
- Lists are not raw `xbmcgui.ListItem`s. `kodigui.ManagedControlList` owns `ManagedListItem`s, which hold a `data_source` (a plexnet object) plus properties (`unwatched`, `progress`, `thumb.fallback`, …). Replacing a row is `replaceItems()`, which destroys the old items and writes into the existing GUI list in place. Two threads must not do that to the same control; `HomeWindow.lock` exists because a background hub callback and a GUI-thread refresh used to free list items out from under Kodi and crash.

`HomeWindow` (`lib/windows/home.py`, control ids 400+ for hub rows) is the shell. Other screens are opened on top via `lib/windows/opener.py` (`showClicked`, `seasonClicked`, `sectionClicked`, …). `windowutils.HOME` is the live home instance, used when a child window needs to signal home.

Busy spinners are `@busy.dialog()` / `@busy.busy_property()` in `lib/windows/busy.py`. A function wrapped with `@busy.dialog()` blocks the GUI thread until it returns; the spinner does not make the work asynchronous.

## Plex API calls

`lib/plex.py` builds a `PlexInterface` and installs it as plexnet's `INTERFACE` before any window opens. It maps Kodi settings onto plexnet preferences, sets timeouts, the user agent (`PM4K/<addon version>`), and the abort flag (Kodi's `Monitor.abortRequested`).

The HTTP stack:

1. `plexnet.plexserver.PlexServer.query(path, **params)` builds `http://<active connection><path>?X-Plex-Token=…`, adds `X-Plex-Container-Start/Size` when `offset`/`limit` are passed, and performs a **synchronous** `self.session.get/put/...`. This is the call almost every list and metadata fetch uses. It logs the URL at `LOGINFO` (token redacted) **before** the request, with no duration.
2. `plexnet.http.HttpRequest` / `PlexRequest` is the older async client (`startAsync` on a thread, or `getWithTimeout` on the caller). Used for reachability tests, plex.tv (`MyPlexRequest`), the playback decision, and timeline updates. Each `HttpRequest` builds a **new** `asyncadapter.Session()`.
3. Both session objects are `asyncadapter.Session`, a `requests_cache.CachedSession` with a custom urllib3 adapter (non-blocking connect so Kodi can cancel). Cache is **off unless** the caller passes `with_cache=True`. `PlexServer.query` does that only when `cachable=True` and a `cache_ref` is set.

`plex.direct` hostnames are rewritten to the embedded IP inside this process (`plexnet.http.pgetaddrinfo`). Kodi's own image loader does **not** use that hook; image URLs need the advancedsettings host mapping (`lib/plex_hosts.py`, `lib/advancedsettings.py`) or they fail DNS-rebinding protection.

There is no generated client. Paths are string literals: `/library/sections`, `/hubs`, `/library/metadata/{ratingKey}`, `/:/timeline`, and so on. XML comes back and is parsed with `xml.etree.ElementTree` into `plexobjects.PlexObject` subclasses (`video.py`, `plexlibrary.py`, `media.py`).

Account and server discovery (`plex.init` → `plexapp.init`):

- Account state is loaded from the Kodi setting registry (`myplex.MyPlexAccount`).
- If a token exists, `GET https://plex.tv/users/account` runs async (`verifyAccount`).
- `MyPlexManager.refreshResources` does `GET https://plex.tv/pms/resources` and caches the last good body in the registry (`mpaResources`).
- Every connection is then probed with `GET /` (`PlexConnection.testReachability`), in parallel, each on its own short-lived session. Timeout is `conn_check_timeout` (default 2.5 s).

## Threading

- The GUI thread runs window callbacks (`onInit`, `onAction`, `onClick`, `onFocus`). Anything it calls directly blocks the UI. `@busy.dialog()` makes that explicit but does not move the work off the thread.
- `util.Cron` is one thread. Interval is `1 / addonSettings.tickrate` (default 1 Hz). Home and the player register as receivers. Home's `tick` refetches hubs when the current section is older than 5 minutes. The player's `tick` sends a timeline update, which the server accepts at most every 10 seconds (`ServerTimeline` expiry).
- `backgroundthread.BGThreader` is a priority queue with `worker_count` workers. The setting allows up to 32, but the constructed count is `min(setting, 3)` unless `ENABLE_HIGH_CONCURRENCY` (Python built without the GIL, i.e. not Kodi 21's 3.11). On Kodi 21 the ceiling is **3 workers**. Tasks are `Task` subclasses (`SectionHubsTask`, `ChunkRequestTask`, `EpisodesReloadTask`, …). `reset()` abandons the current pool so a navigation change does not wait out the old queue.
- `MutablePriorityQueue._get` sorts the entire heap on every pop. Fine at this size; not a latency source next to a ~170 ms round trip.
- Home hub callbacks run on those worker threads and then take `HomeWindow.lock` before touching controls. Assume Kodi GUI calls from a worker are only safe where the code already does them, and don't add new ones.

`lib/monitor.py` `UtilityMonitor` is the `xbmc.Monitor` (abort, screensaver, sleep). `waitFor()` sleeps `wait_interval`, which is `1/ui_wait_rate` (default 10 Hz, so 100 ms). Loops that look like `while not ready: MONITOR.waitFor()` wake at that rate.

## Caching

Three different caches. None of them cover home hubs.

| Store | What | When it is used | TTL / invalidation |
|---|---|---|---|
| `requests_cache` sqlite `special://temp/pm4k_requests_cache` | Raw HTTP responses | Only if setting `cache_requests` includes `items` and/or `libraries` (default **off**, `[]`). Caller must pass `cachable` + `cache_ref`. Hubs never do. | `requests_cache_expiry` hours (default 72), hard expire, no stale-while-revalidate. Wiped on exit unless `persist_requests_cache` (default **off**). A crash (missing `last_shutdown_successful`) wipes it on next start. `clearCache()` deletes the item, its season/show, **and the whole library section**, then `VACUUM`s. Playback end calls `video.clearCache()`. Preplay's return path passes `skip_cache=True`. |
| `data_cache.json` (`lib/data_cache.py`) | Small JSON blobs (show genres, audio-selection memory, hub item "seen" timestamps) | Always, in the profile directory | Last-access eviction ~30 days, data expiry ~90 days. Not an HTTP cache. |
| Kodi texture cache | Decoded images | Every thumb/art/background URL | Keyed by the URL Kodi fetches. Transcode URLs include width, height, and usually the token. A new size or token is a miss. |

`PlexServer.library` is a property that `GET`s `/library/` on **every** access. `Library.sections()` then `GET`s `/library/sections`. Neither result is memoized.

In-memory only: `HomeWindow.sectionHubs` (last hub payload per section, refreshed after 5 minutes), `LibrarySection._settings` and `_filtersCache`, `RelatedMixin._relatedCount` on the object instance.

## Settings

`resources/settings.xml` is the Kodi settings dialog (schema version 1). Defaults live there. `lib/settings_util.py` reads them through `xbmcaddon.Addon.getSetting` and coerces types. List-valued settings are JSON stored hex-encoded.

`lib/addonsettings.py` `AddonSettings` copies a whitelist into attributes **once, at import**. `util.addonSettings.requestsTimeoutConnect` will not see a change made later in the same process. Code that must notice a live change uses `util.getSetting(...)` each time.

A few timeouts that matter on a high-latency link (defaults): connect/read 5 s (`requests_timeout_*`), plex.tv connect 1 s / read 2 s (`plextv_timeout_*`), reachability 2.5 s (`conn_check_timeout`). The plex.tv read timeout is shorter than a slow moment on a high-latency link; account verify can time out and retry (`asyncadapter.MAX_RETRIES`, default 3).

`timing_log` is a hidden boolean (default false, `<visible>false</visible>`). See Logging.

## Logging

`lib/logging.py`:

- `log` / `LOG` — always, at `LOGINFO`, prefixed `script.plexmod:`.
- `DEBUG_LOG` — only when the `debug` setting is on, or Kodi's own debug log is on. Suppressed after shutdown starts.
- `ERROR` — traceback plus optional toast.

`PlexServer.query` uses `LOG`, so **every** PMS URL is already in `kodi.log` even with debug off. It is stamped before the request and has no elapsed time.

Opt-in timing (`lib/timing.py`): `with timing.span("name")`, `timing.mark`, and `timing.observe_http`. Enabled by `PM4K_TIMING=1`, or the hidden `timing_log` setting, or a file `pm4k_timing` containing `1` in the add-on profile (`special://profile/addon_data/script.plexmod/pm4k_timing`). `PM4K_TIMING=0` forces it off. When it is off every helper returns without logging. The HTTP hook is `asyncadapter.Session.request`, so one line covers `PlexServer.query` and `HttpRequest`, including cache hit or miss, bytes, TTFB, thread, and whether the UI thread ran the call. Screen transitions log `phase=first` and `phase=full` with a span id that request lines repeat. Image URLs are timed by watching Kodi's texture database, not by proxying them. `tools/parse_timings.py` summarizes a log. `tools/crawl_feasibility.py` estimates a metadata walk and is not used at runtime. Tokens and private hosts are stripped. How to turn it on is in `docs/PERFORMANCE.md`.

## Version and changelog

Three different strings, easy to confuse:

| Place | Who reads it | What to do on this fork |
|---|---|---|
| `addon.xml` `version="…"` | Kodi, and the updater. Compared with `lib/version.py` `version_compare` (Debian ordering). | **Do not bump it** while iterating. A higher version makes this tree look newer than upstream, so the updater will not offer pannal's next release. A `+dev` suffix sorts *above* the same upstream version for the same reason. |
| `addon.xml` `<news>` | The updater, shown in the "update available" dialog (`NEWS_RE` in `lib/updater.py`). | Leave it. It describes the upstream release, not your branch. |
| `changelog.txt` | Humans. Blocks are `[-version-]`. Nothing in the add-on parses it. | Don't treat it as the version source. |

The updater (`lib/updater.py`) fetches `addon.xml` from GitHub for `pannal/plex-for-kodi` (or the configured branch; beta defaults to `develop_kodi21`) and compares versions. It downloads a GitHub archive and skips `.github`, `.gitignore`, `.gitattributes` only. Anything else in the archive is copied into the add-on directory. Kodi only *executes* the entry points above; `tests/`, `docs/`, and `AGENTS.md` are inert if they get synced, but they are not part of the product.

To tell a dev build from the upstream release with the same version string, have the deploy script write `build_rev.txt` (one line, git sha) next to `addon.xml`. That file is gitignored. On startup `lib/main.py` logs `Build rev: …` if the file is present, and logs nothing if it is absent.

`util.DEBUG_LOG` at startup already prints the add-on version (`[ STARTED: {version} ]`). That line is debug-gated. The build-rev line is not.

## Offline harness

Kodi is not installed here. `tests/kodistubs/` provides `xbmc`, `xbmcgui`, `xbmcvfs`, `xbmcaddon`, `xbmcplugin`, and `kodi_six`. `tests/__init__.py` inserts that directory on `sys.path` before `lib/` imports. `tests/kodistubs/kodienv.py` `ENV` is the steerable state (settings, infolabels, skin conditions, RPC results, captured log lines). `tests/base.py` `KodiTestCase` resets `ENV` per test.

```sh
python3 -m pytest
python3 -m unittest discover -s tests -t .
python3 -m compileall -q lib tests default.py service.py plugin.py screensaver.py
```

`pytest` is optional; the tests are `unittest.TestCase`. Needs `requests` and `six` only. No network, no Kodi. Details and the "import-time constants don't see later ENV changes" trap are in `tests/README.md`. CI is `.github/workflows/offline.yml`.

`ruff.toml` selects syntax and pyflakes rules only. CI runs it on `lib/timing.py` and `tests/test_timing.py`. The historical tree will not pass a normal lint and should not be reformatted.

## Surprises

- Importing `lib.util` or `lib.plex` reads settings and talks to the (stubbed) JSON-RPC at import time. A test that changes `ENV` after that does not change `addonSettings.*` or `util.DISPLAY_RESOLUTION`.
- `from __future__ import absolute_import` is everywhere. Keep it on new modules. Don't add type-only syntax that breaks the 3.11 Kodi runs.
- `xbmcvfs.File` is closed with `try/finally`, not `with`. The context manager is not assumed to exist on every Kodi this tree still considers. `tests/test_vfs_handles.py` enforces the close.
- Home draws hub rows from a worker thread under a lock. Don't "fix" that by moving it to the GUI thread without a plan; the lock comment in `HomeWindow.__init__` explains the crash it prevents.
- `checkFiles=1` on metadata reloads is a server-side stat of the media file, added on top of the round trip. It is on the preplay, episode, and season open paths.
- Generated skin XML is gitignored. If you change a `.xml.tpl`, you cannot see the result without the template engine (`tests/test_templates.py` drives it without writing into `1080i/`).
- Debug and timing flags are not free on the HTPC: `LOG` of every URL is already unconditional. Don't add more unconditional info logs in `query()`.
