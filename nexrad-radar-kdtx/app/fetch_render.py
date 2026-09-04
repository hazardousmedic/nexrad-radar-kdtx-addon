"""
Polls NOAA's public NEXRAD Level III feed (unidata-nexrad-level3 S3 bucket, no
auth required) for the latest base reflectivity (N0B) and storm-track (NST)
products for a configured radar site, renders them into a single composite
frame, and maintains a rolling animated-GIF loop + tiny auto-refreshing HTML
page under OUTPUT_DIR.

Bucket key format confirmed live 2026-09-02: "{SITE}_{PRODUCT}_%Y_%m_%d_%H_%M_%S"
at the bucket root (flat namespace, no folders), fixed-width so lexicographic
sort == chronological sort.

All location/site settings come from the add-on's Configuration options
(Supervisor writes them to /data/options.json) - see config.yaml for the
schema. This same script runs unmodified for every installer; only the
options differ per install.
"""
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

import matplotlib
matplotlib.use("Agg")
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.pyplot as plt
import numpy as np
import requests
# Aliased: this module already imports pathlib.Path for filesystem paths.
from matplotlib.path import Path as MarkerPath
from metpy.calc import azimuth_range_to_lat_lon
from metpy.io import Level3File
from metpy.plots import colortables, USCOUNTIES
from metpy.units import units
from PIL import Image
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nexrad-radar-kdtx")


def _load_options():
    """Read the add-on's configured options (Supervisor-managed /data/options.json).

    Falls back to placeholder defaults if the file is missing (e.g. running
    outside Supervisor during development) - those placeholders deliberately
    do NOT point at any real location.
    """
    defaults = {
        "radar_site": "DTX",
        "home_latitude": 0.0,
        "home_longitude": 0.0,
        "local_width_km": 220,
        "timezone": "America/New_York",
    }
    path = Path("/data/options.json")
    if not path.exists():
        log.warning("options.json not found, using placeholder defaults (%s)", defaults)
        return defaults
    with open(path) as f:
        options = json.load(f)
    return {**defaults, **options}


OPTIONS = _load_options()
SITE = OPTIONS["radar_site"].strip().upper()
HOME_LAT = float(OPTIONS["home_latitude"])
HOME_LON = float(OPTIONS["home_longitude"])
LOCAL_TZ = ZoneInfo(OPTIONS["timezone"])

if HOME_LAT == 0.0 and HOME_LON == 0.0:
    log.warning(
        "home_latitude/home_longitude are not configured (still 0.0, 0.0) - "
        "set them in the add-on's Configuration tab to your actual location. "
        "Rendering will center on the equator/prime meridian until you do."
    )

# Built once at import time, not per-render: cartopy/pyshp open shapefile
# file handles on each Feature construction, and recreating these in the
# render hot loop (every ~5 min, forever) is the same "unclosed resource in
# a hot loop" pattern that caused the Image.open() leak in rebuild_loop().
STATES_PROVINCES = cfeature.NaturalEarthFeature(
    category="cultural", name="admin_1_states_provinces_lines", scale="10m",
    facecolor="none",
)
COASTLINE = cfeature.COASTLINE.with_scale("10m")
LAKES = cfeature.LAKES.with_scale("10m")

BUCKET = "https://unidata-nexrad-level3.s3.amazonaws.com/"
OUTPUT_DIR = Path("/data/output")  # HA add-on persistent storage, survives rebuilds/updates
FRAMES_DIR = OUTPUT_DIR / "frames"
MAX_FRAMES = 12  # ~1 hour at ~5 min volume-scan cadence
FRAME_DURATION_MS = 900  # per-frame display time in the looping GIF
POLL_SECONDS = 60
S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# Matches the Echo Show dashboard's radar iframe box (1342px wide x ~604px
# tall, from the card's aspect_ratio: "45%") so the rendered map fills the
# card with no letterboxing. Adjust local_width_km in the add-on options if
# your dashboard card has a different aspect ratio.
CARD_ASPECT = 1342 / 604
LOCAL_WIDTH_KM = float(OPTIONS["local_width_km"])
LOCAL_HEIGHT_KM = LOCAL_WIDTH_KM / CARD_ASPECT
KM_PER_DEG_LAT = 111.32

# Lightning strikes, via the add-on's `homeassistant_api: true` config flag -
# Supervisor auto-injects SUPERVISOR_TOKEN and proxies HA's core REST API at
# this URL, so no manually-created long-lived access token is needed. Only
# available when running as a real Supervisor-managed add-on (unset in local
# dev), so this whole feature degrades to "no strikes drawn" gracefully.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_BASE = "http://supervisor/core/api"
STRIKE_MAX_AGE_MIN = 15  # strikes older than this are no longer drawn

# Matplotlib has no built-in lightning-bolt marker, so this is a hand-drawn
# zigzag polygon (unit-scale, origin-centered) used as a custom marker path.
_BOLT_VERTICES = [
    (0.35, 1.0), (-0.45, 0.05), (-0.05, 0.05),
    (-0.35, -1.0), (0.45, -0.05), (0.05, -0.05), (0.35, 1.0),
]
_BOLT_CODES = [MarkerPath.MOVETO] + [MarkerPath.LINETO] * 5 + [MarkerPath.CLOSEPOLY]
BOLT_MARKER = MarkerPath(_BOLT_VERTICES, _BOLT_CODES)


def list_latest_keys(product: str, count: int = 3):
    """Return up to `count` most recent object keys for SITE/product, newest last."""
    now = datetime.now(timezone.utc)
    keys = []
    for day in (now, now - timedelta(days=1)):
        prefix = f"{SITE}_{product}_{day:%Y_%m_%d}"
        resp = requests.get(BUCKET, params={"list-type": "2", "prefix": prefix, "max-keys": "1000"}, timeout=30)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        keys.extend(el.text for el in root.findall("s3:Contents/s3:Key", S3_NS))
        if keys:
            break  # today's prefix had data, no need to also fetch yesterday's
    keys.sort()
    return keys[-count:]


def fetch_key(key: str) -> bytes:
    resp = requests.get(BUCKET + key, timeout=30)
    resp.raise_for_status()
    return resp.content


def fetch_lightning_strikes():
    """Return (lon, lat, age_seconds) for recent Blitzortung strikes.

    Reads HA's `geo_location.lightning_strike_*` entities (created by the
    already-installed `mrk-its/homeassistant-blitzortung` integration) via
    Supervisor's HA API proxy. Best-effort: any failure (API unreachable,
    no SUPERVISOR_TOKEN, unexpected shape) just means no strikes are drawn
    this cycle, same philosophy as the storm-track overlay.
    """
    if not SUPERVISOR_TOKEN:
        log.warning("lightning overlay disabled: SUPERVISOR_TOKEN not set "
                    "(homeassistant_api: true missing, or not running under Supervisor)")
        return []
    try:
        resp = requests.get(
            f"{HA_API_BASE}/states",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=10,
        )
        resp.raise_for_status()
        now = datetime.now(timezone.utc)
        strikes = []
        for ent in resp.json():
            attrs = ent.get("attributes", {})
            if attrs.get("source") != "blitzortung":
                continue
            pub = attrs.get("publication_date")
            lat, lon = attrs.get("latitude"), attrs.get("longitude")
            if not pub or lat is None or lon is None:
                continue
            age = (now - datetime.fromisoformat(pub)).total_seconds()
            if age > STRIKE_MAX_AGE_MIN * 60:
                continue
            strikes.append((lon, lat, age))
        log.info("lightning: %d strikes within %d min", len(strikes), STRIKE_MAX_AGE_MIN)
        return strikes
    except Exception as exc:  # noqa: BLE001 - overlay is best-effort only
        log.warning("lightning fetch skipped: %s", exc)
        return []


def render_frame(n0b_bytes: bytes, nst_bytes: bytes | None, strikes: list, out_path: Path):
    f = Level3File(io.BytesIO(n0b_bytes))
    datadict = f.sym_block[0][0]
    data = f.map_data(datadict["data"])

    az = units.Quantity(np.array(datadict["start_az"] + [datadict["end_az"][-1]]), "degrees")
    rng = units.Quantity(np.linspace(0, f.max_range, data.shape[-1] + 1), "kilometers")
    cent_lon, cent_lat = f.lon, f.lat
    xlocs, ylocs = azimuth_range_to_lat_lon(az, rng, cent_lon, cent_lat)

    # NWSStormClearReflectivity (official MetPy example's choice for N0Q/N0B)
    # correctly reserves its low end for "no data"/clear air; still mask
    # anything below a light-rain noise floor so clear-air noise doesn't
    # paint the whole scan area.
    ref_norm, ref_cmap = colortables.get_with_steps("NWSStormClearReflectivity", -20, 0.5)
    ref_cmap.set_bad(color=(0, 0, 0, 0))
    masked = np.ma.masked_less(data, 5)

    crs = ccrs.LambertConformal(central_longitude=cent_lon, central_latitude=cent_lat)
    fig = plt.figure(figsize=(11.1, 11.1 / CARD_ASPECT), dpi=120)
    fig.patch.set_facecolor("#0C0F14")
    ax = fig.add_axes([0, 0, 1, 1], projection=crs)
    ax.set_facecolor("#0C0F14")
    ax.pcolormesh(xlocs, ylocs, masked, norm=ref_norm, cmap=ref_cmap, transform=ccrs.PlateCarree())

    # Map context: county lines (US only, bundled with MetPy - no download
    # needed), state/province lines (Natural Earth, covers both US and
    # Canada - cached under HOME/.local/share/cartopy after first fetch),
    # and coastlines. Sized for a zoomed-in local view.
    ax.add_feature(USCOUNTIES, linewidth=0.6, edgecolor="#3A4552")
    ax.add_feature(STATES_PROVINCES, linewidth=1.4, edgecolor="#8C99A6")
    ax.add_feature(COASTLINE, linewidth=1.0, edgecolor="#5FD8C4")
    # facecolor="none" here is deliberate: an opaque lake fill drawn after
    # the pcolormesh would blank out any real radar returns over water -
    # relevant for any site near a large lake (e.g. the Great Lakes).
    ax.add_feature(LAKES, facecolor="none", edgecolor="#5FD8C4", linewidth=1.0)

    lat_half_deg = (LOCAL_HEIGHT_KM / 2) / KM_PER_DEG_LAT
    lon_half_deg = (LOCAL_WIDTH_KM / 2) / (KM_PER_DEG_LAT * np.cos(np.deg2rad(HOME_LAT)))
    ax.set_extent(
        [HOME_LON - lon_half_deg, HOME_LON + lon_half_deg, HOME_LAT - lat_half_deg, HOME_LAT + lat_half_deg],
        crs=ccrs.PlateCarree(),
    )
    ax.set_aspect("auto")
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Storm-track (NST) overlay. Confirmed packet structure (2026-09-03, via
    # direct inspection - MetPy's own repr doesn't document this): each
    # storm cell is a repeating run of packets in sym_block[0]:
    #   {'current storm position': (x_km, y_km)}
    #   {'x':..., 'y':..., 'id': 'L1', 'type': 'Storm ID'}
    #   {'markers': [{'past storm position': (x,y)}, ...], 'track': [...]}
    #   {'markers': [{'forecast storm position': (x,y)}, ...], 'track': [...]}
    # x/y are km offsets from the radar site (same origin as cent_lon/
    # cent_lat), not lat/lon - convert before plotting on the PlateCarree
    # transform used everywhere else in this function.
    if nst_bytes:
        try:
            def _km_to_lonlat(x_km, y_km):
                lat = cent_lat + y_km / KM_PER_DEG_LAT
                lon = cent_lon + x_km / (KM_PER_DEG_LAT * np.cos(np.deg2rad(cent_lat)))
                return lon, lat

            sf = Level3File(io.BytesIO(nst_bytes))
            for block in sf.sym_block:
                for packet in block:
                    if not isinstance(packet, dict):
                        continue
                    if packet.get("type") == "Storm ID":
                        continue  # ID dot + label intentionally dropped - the track line is enough
                    elif "track" in packet and packet.get("markers"):
                        # A single-point track gives a bare dict instead of
                        # a one-item list - normalize before indexing.
                        markers = packet["markers"]
                        first_marker = markers if isinstance(markers, dict) else markers[0]
                        if "forecast storm position" not in first_marker:
                            continue  # past-track history line - not wanted, skip
                        lons, lats = zip(*(_km_to_lonlat(x, y) for x, y in packet["track"]))
                        ax.plot(lons, lats, transform=ccrs.PlateCarree(), linewidth=1.5,
                                color="white", linestyle="-")
        except Exception as exc:  # noqa: BLE001 - overlay is best-effort only
            log.warning("storm-track overlay skipped: %s", exc)

    # Lightning strikes - drawn as fading white bolt markers so recent
    # strikes stand out from ones about to age out (see STRIKE_MAX_AGE_MIN).
    # scatter() (not plot()) is used because it fills custom marker Paths
    # solid; plot() would only stroke the polygon's outline.
    for lon, lat, age_s in strikes:
        alpha = max(0.15, 1 - age_s / (STRIKE_MAX_AGE_MIN * 60))
        ax.scatter(lon, lat, marker=BOLT_MARKER, s=400, color="white",
                   linewidths=0, alpha=alpha, transform=ccrs.PlateCarree())

    # Timestamp imprint, bottom-right - uses the radar's own scan time
    # (f.metadata['prod_time'], a naive UTC datetime) rather than wall-clock
    # time, so it reflects when the data was actually collected.
    prod_time = f.metadata.get("prod_time")
    if prod_time:
        local_time = prod_time.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        ax.text(0.99, 0.02, local_time.strftime("%b %d %I:%M %p %Z"),
                transform=ax.transAxes, ha="right", va="bottom",
                color="white", fontsize=10, fontweight="bold",
                bbox=dict(facecolor="black", alpha=0.55, pad=3, edgecolor="none"))

    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)


def rebuild_loop():
    frames = sorted(FRAMES_DIR.glob("*.png"))[-MAX_FRAMES:]
    if not frames:
        return
    images = []
    for p in frames:
        with Image.open(p) as img:
            images.append(img.convert("RGB"))
    images[0].save(
        OUTPUT_DIR / "loop.gif",
        save_all=True,
        append_images=images[1:],
        duration=FRAME_DURATION_MS,
        loop=0,
    )
    images[-1].save(OUTPUT_DIR / "latest.png")


def write_index_html():
    html = """<!doctype html><html><head>
<meta http-equiv="refresh" content="300">
<style>html,body{margin:0;background:#0C0F14;height:100%}
img{width:100%;height:100%;object-fit:contain;display:block}</style>
</head><body><img src="loop.gif?t=TIMESTAMP"></body></html>""".replace("TIMESTAMP", str(int(time.time())))
    (OUTPUT_DIR / "index.html").write_text(html)


def main():
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    last_key = None
    while True:
        try:
            n0b_keys = list_latest_keys("N0B", 1)
            if not n0b_keys:
                log.warning("no N0B keys found for %s", SITE)
                time.sleep(POLL_SECONDS)
                continue
            latest = n0b_keys[-1]
            if latest != last_key:
                nst_keys = list_latest_keys("NST", 1)
                n0b_bytes = fetch_key(latest)
                nst_bytes = fetch_key(nst_keys[-1]) if nst_keys else None
                strikes = fetch_lightning_strikes()
                frame_path = FRAMES_DIR / f"{latest}.png"
                render_frame(n0b_bytes, nst_bytes, strikes, frame_path)

                existing = sorted(FRAMES_DIR.glob("*.png"))
                for stale in existing[:-MAX_FRAMES]:
                    stale.unlink(missing_ok=True)

                rebuild_loop()
                write_index_html()
                last_key = latest
                log.info("rendered frame %s", latest)
        except Exception:
            log.exception("render cycle failed, will retry")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
