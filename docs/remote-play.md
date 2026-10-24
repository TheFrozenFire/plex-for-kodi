# Play a Plex item from outside Kodi

PlexMod can start playback of one library item in the add-on that is already
running. The item is chosen by its Plex `ratingKey`. Playback goes through the
normal play path (resume handling, watch progress, sticky subtitles, and
playback prep).

The add-on has to be running already (home, any screen on top of it, or the
screensaver). A second launch does not open another copy of the UI. It hands
the request to the running instance and exits. Restart Kodi, or quit PlexMod
and start it again, after installing this build so the running instance is
the one that listens.

Kodi's JSON-RPC is the only interface. The examples below post to
`http://127.0.0.1:8080/jsonrpc`.

## Play

`ratingKey` is required. `server` is an optional server id or name; omit it
to use the server selected in PlexMod. `resume` defaults to resuming when the
item has progress, otherwise starting at the beginning, with no prompt.
`resume` `false` always starts at the beginning. `resume` `"ask"` is the
on-screen play button, including its resume prompt. `force` `true` stops
whatever video is already playing first. Without `force`, an in-progress
video is left alone.

A show or season key plays the next episode: for a show, the server's On Deck
item, otherwise the first unwatched episode; for a season, an in-progress
episode, otherwise the first unwatched one.

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"JSONRPC.NotifyAll","id":1,"params":{"sender":"script.plexmod","message":"PM4K_PLAY","data":{"ratingKey":"12345"}}}' \
  http://127.0.0.1:8080/jsonrpc
```

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"JSONRPC.NotifyAll","id":1,"params":{"sender":"script.plexmod","message":"PM4K_PLAY","data":{"ratingKey":"12345","server":"SERVER_ID","resume":false,"force":false}}}' \
  http://127.0.0.1:8080/jsonrpc
```

The same request can be a second script launch. It forwards to the running
instance:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"Addons.ExecuteAddon","id":1,"params":{"addonid":"script.plexmod","params":["play","12345"]}}' \
  http://127.0.0.1:8080/jsonrpc
```

Optional arguments after the ratingKey are server id or name, resume
(`1`, `0`, or `ask`), and force (`1` or `0`):

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"Addons.ExecuteAddon","id":1,"params":{"addonid":"script.plexmod","params":["play","12345","SERVER_ID","0","1"]}}' \
  http://127.0.0.1:8080/jsonrpc
```

## Result

The latest result is a Kodi window property, `script.plex.remoteplay`.
`state` is `pending` while the request is in progress, then `ok` or `error`.
Wait until it is not `pending`. A Kodi notification is also shown, and a
`Remote play:` line is written to the log.

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"XBMC.GetInfoLabels","id":1,"params":{"labels":["Window(10000).Property(script.plex.remoteplay)"]}}' \
  http://127.0.0.1:8080/jsonrpc
```

The label value is JSON:

```json
{"id":"abc","ok":true,"ratingKey":"12345","reason":"started","requested":"12345","state":"ok","type":"episode"}
```

```json
{"id":"abc","ok":false,"reason":"playing","requested":"12345","state":"error"}
```

`reason` on failure is one of `bad_request`, `not_running`, `not_ready`,
`playing`, `busy`, `no_server`, `ambiguous_server`, `not_found`,
`unsupported`, `nothing_to_play`, `unavailable`, `error`. `playing` means a
video was already playing and `force` was not set. An empty property means
the running instance did not receive the request.

The same JSON is also stored on `script.plex.remoteplay.<id>` when the
request has an id.

## Finding a ratingKey

Ask the Plex server. `TOKEN` is that server's access token. `SECTION` is a
library section id from the first call. `type=1` is movies, `type=2` shows,
`type=3` seasons, `type=4` episodes. The `ratingKey` attribute on each
`Video` is the value to pass above.

```bash
curl -s "http://SERVER:32400/library/sections?X-Plex-Token=TOKEN"
curl -s "http://SERVER:32400/library/sections/SECTION/all?type=4&X-Plex-Token=TOKEN"
```

A title search returns the same attribute:

```bash
curl -s --get "http://SERVER:32400/search" --data-urlencode "query=TITLE" --data-urlencode "X-Plex-Token=TOKEN"
```

## Navigate

The same `JSONRPC.NotifyAll` call can move the running add-on. The sender
is `script.plexmod` and the message is `PM4K_NAV`. Kodi delivers it as
`Other.PM4K_NAV`. `data` is one of:

- `{"to":"home"}` — close PlexMod windows on top of Home, select the Home
  section, and focus the section list
- `{"to":"section","type":"show"}` — open the first TV Shows library, as if
  it were chosen on the home screen
- `{"to":"section","type":"movie"}` — open the first Movies library the same
  way
- `{"to":"section","title":"TV Shows"}` — open the library with that title
- `{"to":"section","type":"movie","title":"Movies"}` — the title and the type
  both have to match
- `{"to":"skip_marker"}` — press the playback screen's Skip button while an
  intro or credits marker is active
- `{"to":"auto_skip"}` — turn on intro and credits auto-skip for later
  episodes in this Kodi session. `{"to":"auto_skip","on":false}` turns it off

`title` matches the library title exactly, ignoring case and surrounding
whitespace. If a title is set and no library matches, nothing opens. The
add-on does not fall back to the first library of that type.

```json
{"sender":"script.plexmod","message":"PM4K_NAV","data":{"to":"section","title":"TV Shows"}}
```

Home and library navigation do nothing while a video is playing. Any other
payload is ignored. Each result is a `Remote nav:` line in the log.

## Skip the active marker

`skip_marker` runs the playback screen's own Skip button (control 791). It
does not depend on which control has focus, or on the button still being on
screen. An intro or credits marker still counts after the button has hidden
itself and the marker is OSD-only (`show.markerSkip_OSDOnly`). A final
credits marker takes the same path as the button (post-play, or the next
episode). If no marker is active, nothing happens and the log says
`Remote nav: ignored reason=no_marker`.

```json
{"jsonrpc":"2.0","method":"JSONRPC.NotifyAll","id":1,"params":{"sender":"script.plexmod","message":"PM4K_NAV","data":{"to":"skip_marker"}}}
```

A skip is `Remote nav: ok to=skip_marker type=intro` or `type=credits`. A
final credits marker adds `final=1`.

## Session auto-skip

`auto_skip` turns on intro and credits auto-skip for episode playback that
starts after the command. The episode already playing is unchanged. The
choice lasts for this Kodi session only. It is not written to a show's
playback settings. Sending it again with `"on":false` turns it off.
`on` may be omitted (on), or `true` / `false`.

```json
{"jsonrpc":"2.0","method":"JSONRPC.NotifyAll","id":1,"params":{"sender":"script.plexmod","message":"PM4K_NAV","data":{"to":"auto_skip"}}}
```

```json
{"jsonrpc":"2.0","method":"JSONRPC.NotifyAll","id":1,"params":{"sender":"script.plexmod","message":"PM4K_NAV","data":{"to":"auto_skip","on":false}}}
```

The log line is `Remote nav: ok to=auto_skip on=1` or `on=0`.
