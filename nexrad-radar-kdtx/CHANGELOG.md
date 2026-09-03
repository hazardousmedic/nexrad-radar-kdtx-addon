# Changelog

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
