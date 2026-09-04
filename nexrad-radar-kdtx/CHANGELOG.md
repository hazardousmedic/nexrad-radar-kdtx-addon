# Changelog

## 1.2.2

- Added diagnostic logging to `fetch_lightning_strikes()`: logs a warning
  if `SUPERVISOR_TOKEN` is missing (lightning overlay silently disabled),
  and an info line with the strike count found each cycle. Added after
  strikes weren't appearing on a live render with no errors in the logs -
  the previous silent-degrade design made it impossible to tell whether
  the token was missing or zero strikes were genuinely in range.

## 1.2.1

- Lightning strikes now render as white lightning-bolt markers instead of
  yellow `x` marks (a custom `matplotlib.path.Path` polygon, since
  matplotlib has no built-in bolt marker).

## 1.2.0

- Added live lightning strike overlay on the radar map. Reads
  `geo_location.lightning_strike_*` entities from the
  [Blitzortung](https://github.com/mrk-its/homeassistant-blitzortung)
  integration (installed separately, not bundled) via Home Assistant's own
  REST API — set `homeassistant_api: true` in this add-on's config, which
  makes Supervisor auto-inject a `SUPERVISOR_TOKEN` and proxy HA's API, so
  no manually-created long-lived access token is required. Strikes newer
  than `STRIKE_MAX_AGE_MIN` (15 min) are drawn as yellow `x` markers that
  fade out as they age; the feature is entirely optional and silently
  no-ops (no strikes drawn) if Blitzortung isn't installed or the API call
  fails.

## 1.1.2

- Hoisted the `COASTLINE`/`LAKES` map features to module-level constants
  (built once at import time) instead of recreating them via `.with_scale()`
  on every render call. Precautionary hardening against the same class of
  bug as 1.1.1, not a confirmed second leak — cartopy caches Natural Earth
  geometries internally by name/category/scale, so this mainly avoids
  needless per-render object construction and matches the pattern already
  used correctly for `STATES_PROVINCES`.

## 1.1.1

- Fixed a file-descriptor leak in `rebuild_loop()`: `Image.open()` on each
  frame was never closed, so after enough render cycles the container hit
  its open-file limit (`OSError: [Errno 24] Too many open files`) and every
  subsequent S3 poll failed, freezing the loop indefinitely. Frame images
  are now opened in a `with` block so the file handle is released after
  each conversion.

## 1.1.0

- Made location fully configurable: `radar_site`, `home_latitude`,
  `home_longitude`, `local_width_km`, and `timezone` are now add-on options
  instead of hardcoded constants, so the add-on works for any US radar site
  and any location, not just one hardcoded install.
- Renamed from a site-specific slug (`nexrad_radar_kdtx`) to a generic one
  (`nexrad_radar_loop`) to reflect that it's no longer tied to a single site.

## 1.0.0

- Initial release. Live KDTX (Detroit/Pontiac) base reflectivity loop with
  county/state/province map lines, storm-track forecast lines, and a
  scan-time timestamp imprint. Originally built for a single fixed
  home/site.
