# Content skip rules

The add-on can skip a time range, or a whole item, using a rules document it downloads. The TV does not accept inbound connections. Nothing is skipped until **Content skip rules** is turned on. That switch is off by default.

A failed download never stops playback. The last document that parsed is kept and used.

## Settings

| Setting | Default | Role |
| --- | --- | --- |
| `skip_rules_enabled` | off | Master switch. |
| `skip_rules_url` | empty | Where to download the document. Empty uses only the saved copy. |
| `skip_rules_token` | empty | Sent as `Authorization: Bearer` when the address is set. |
| `skip_lead_in_ms` | 1000 | How early, in milliseconds, a range starts to count. |
| `skip_rules_toast` | on | Short notice when something is skipped. |

The token is not written into the log or into the status property.

## When the document is fetched

- Once at startup, on the existing timer, when the switch is on.
- Again every 10 minutes.
- Again immediately before each playback start.
- When Kodi is sent `NotifyAll` from `script.plexmod` with message `PM4K_SKIPS_RELOAD`, and the switch is on.

Requests send `If-None-Match` when a previous response included an `ETag`. A `304` keeps the saved copy. The saved copy lives in the add-on profile as `skip_rules.json`. Each attempt times out after a few seconds. A timeout, a bad status, or a document that is not version 1 leaves the saved copy in place.

## Document

Unknown fields are ignored. A rule that is missing a matcher, or that has a bad type or a bad range, is dropped and logged by id. The rest of the document is kept.

```json
{
  "version": 1,
  "updated_at": "2026-01-01T00:00:00Z",
  "rules": [
    {
      "id": "intro-1",
      "type": "skip_range",
      "plex_rating_key": "12345",
      "server": null,
      "show_title": null,
      "season": null,
      "episode": null,
      "movie_title": null,
      "year": null,
      "imdb": null,
      "tvdb": null,
      "start_ms": 0,
      "end_ms": 90000,
      "reason": "intro",
      "source": "manual",
      "created_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

`type` is `skip_range` or `skip_item`.

`server` is a Plex machine identifier or a server name. It is checked only when the rule sets it.

`start_ms` and `end_ms` are milliseconds. Omit `end_ms` (or set it to the end of the file) when the range runs to the end. `end_ms` must be greater than `start_ms` when it is set.

A rule must match by one of:

- `plex_rating_key`
- `imdb` (`tt…`) and/or `tvdb` (digits)
- `show_title` plus `season` plus `episode`
- `movie_title` plus `year`

## Which rules apply

For the item that is about to play:

1. Rules whose rating key matches (and whose server matches, when the rule has one).
2. Otherwise rules whose `imdb` or `tvdb` value is on **this** item. An episode uses the episode's own guids, not the show's.
3. Otherwise a case-insensitive title match: show title, season, and episode, or movie title and year.

A rule that sets a rating key does not also match by title when the key does not match. If any rating-key rule hits, title and external-id rules are not used for that item. If any external-id rule hits, title rules are not used.

Overlapping or touching `skip_range` rules become one range. The jump goes to the end of that merged range.

## Playback

`skip_item` is decided before the file starts.

- An episode advances to the next item in the play queue. If there is no next item, playback stops.
- A movie stops, even when something else is queued.
- The notice is `Skipped: <reason>`, or `Skipped` when the rule has no reason.

`skip_range` is watched from the player monitor, which already reads the playhead, about every 250 ms. The monitor does not seek or stop itself. It queues the action and wakes the window. The window event loop, which is the script thread, performs the seek or the end-of-file handling. That path does not touch list items from a worker.

The playhead is inside a range when it is within `[start_ms - lead_in, end_ms)`. Entering that window seeks to `end_ms` and shows `Skipped scene`. One entry produces one seek. Seeking or resuming into the window counts as an entry. After the playhead leaves the window, a later entry can skip again. Sitting inside the window does not seek again.

A range that reaches the end of the file uses the same path as playback ending: the next queued item, including the usual end-of-episode screen when that is enabled, or stop when nothing follows.

## Status

Before playback, the matched rules are published as a window property `script.plex.skiprules` on window `10000`. JSON-RPC can read it with `XBMC.GetInfoLabels` and `Window(10000).Property(script.plex.skiprules)`.

The value is JSON:

```json
{"matched":[{"id":"intro-1","type":"skip_range","reason":"intro","start_ms":0,"end_ms":90000}],"rating_key":"12345","type":"episode"}
```

## Reload

From a machine that can reach Kodi's JSON-RPC port:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"JSONRPC.NotifyAll","params":{"sender":"script.plexmod","message":"PM4K_SKIPS_RELOAD","data":""},"id":1}' \
  http://127.0.0.1:8080/jsonrpc
```
