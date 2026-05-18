"""
GeoReport — 100% API-driven backend
No shapefiles. No uploads. Works anywhere.

Data sources (all free):
  • DEM + watershed delineation : OpenTopography SRTM GL3 (90m)  →  pysheds
  • Streams                      : OpenStreetMap Overpass API
  • Aquifer                      : IGRAC GGMN / WHYMAP REST API
  • Basemap tiles                : contextily (OpenTopoMap, Esri)

pip install flask flask-cors geopandas rasterio pysheds contextily
            requests numpy matplotlib shapely scipy
"""

import os, io, tempfile, base64, warnings, json, time
import requests
import numpy as np
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.backends.backend_pdf import PdfPages
import contextily as ctx
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.transform import from_bounds
from shapely.geometry import Point, shape, mapping, MultiPolygon, Polygon
from shapely.ops import unary_union
import warnings
warnings.filterwarnings("ignore")

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── API keys ──────────────────────────────────────────────────────────────────
OT_API_KEY = os.environ.get("OPENTOPO_API_KEY", "YOUR_OPENTOPOGRAPHY_KEY")

# ── Constants ─────────────────────────────────────────────────────────────────
OVERPASS_URL  = "https://overpass-api.de/api/interpreter"
DEM_PRELIM_KM = 1.5     # initial small DEM box to snap pour point (degrees ~ 0.8°)
DEM_PRELIM_DEG = 0.8
BUFFER_DEG     = 0.05   # pad around watershed for display


# ══════════════════════════════════════════════════════════════════════════════
#  1. DEM DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

def download_dem_box(south, north, west, east, path, res="SRTMGL3"):
    """Download DEM from OpenTopography for a bounding box."""
    url = (
        "https://portal.opentopography.org/API/globaldem"
        f"?demtype={res}"
        f"&south={south}&north={north}&west={west}&east={east}"
        f"&outputFormat=GTiff&API_Key={OT_API_KEY}"
    )
    r = requests.get(url, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"DEM download failed ({r.status_code}): {r.text[:300]}")
    with open(path, "wb") as f:
        f.write(r.content)
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  2. WATERSHED DELINEATION via pysheds (no shapefiles!)
# ══════════════════════════════════════════════════════════════════════════════

def delineate_watershed(lon, lat, dem_path):
    """
    Use pysheds D8 flow-direction algorithm to delineate the watershed
    upstream of (lon, lat) from the downloaded DEM.
    Returns a shapely Polygon.
    """
    from pysheds.grid import Grid

    grid = Grid.from_raster(dem_path)
    dem  = grid.read_raster(dem_path)

    # Condition DEM
    pit_filled  = grid.fill_pits(dem)
    flooded     = grid.fill_depressions(pit_filled)
    inflated    = grid.resolve_flats(flooded)

    # Flow direction (D8)
    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
    fdir   = grid.flowdir(inflated, dirmap=dirmap)

    # Flow accumulation
    acc = grid.accumulation(fdir, dirmap=dirmap)

    # Snap pour point to highest-accumulation cell within 0.05°
    x_snap, y_snap = grid.snap_to_mask(acc > 200, (lon, lat))

    # Delineate catchment
    catch = grid.catchment(
        x=x_snap, y=y_snap,
        fdir=fdir, dirmap=dirmap,
        xytype="coordinate",
    )

    # Convert raster mask → polygon
    grid.clip_to(catch)
    catch_view = grid.view(catch, dtype=np.uint8)

    shapes = list(rasterio.features.shapes(
        catch_view,
        mask=(catch_view == 1),
        transform=grid.affine,
    ))

    if not shapes:
        raise ValueError("Watershed delineation produced no polygon — try a different point.")

    polygons = [shape(s) for s, v in shapes if v == 1]
    watershed_poly = unary_union(polygons)

    # Sanity: if tiny (<5 km²), raise
    area_approx = watershed_poly.area * (111 ** 2)   # rough km²
    if area_approx < 1:
        raise ValueError("Delineated watershed is too small — point may be on a flat or ridge.")

    return watershed_poly, area_approx


# ══════════════════════════════════════════════════════════════════════════════
#  3. STREAMS from OpenStreetMap Overpass API
# ══════════════════════════════════════════════════════════════════════════════

def fetch_streams_osm(minx, miny, maxx, maxy):
    """
    Fetch rivers/streams from OSM Overpass within a bounding box.
    Returns a GeoDataFrame (linestrings) or empty GDF.
    """
    query = f"""
    [out:json][timeout:60];
    (
      way["waterway"~"^(river|stream|canal|drain|brook)$"]({miny},{minx},{maxy},{maxx});
    );
    out geom;
    """
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=90)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ⚠ Overpass API error: {e}")
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    from shapely.geometry import LineString
    lines = []
    names = []
    wtypes = []

    for el in data.get("elements", []):
        if el["type"] == "way" and "geometry" in el:
            coords = [(n["lon"], n["lat"]) for n in el["geometry"]]
            if len(coords) >= 2:
                lines.append(LineString(coords))
                names.append(el.get("tags", {}).get("name", ""))
                wtypes.append(el.get("tags", {}).get("waterway", "stream"))

    if not lines:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    return gpd.GeoDataFrame(
        {"name": names, "waterway": wtypes},
        geometry=lines,
        crs="EPSG:4326",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  4. AQUIFER from IGRAC GGMN REST API  (fallback: WHYMAP bounding-box)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_aquifer(minx, miny, maxx, maxy):
    """
    Try IGRAC GGMN WFS for aquifer polygons.
    Falls back to a simplified WHYMAP query.
    Returns GeoDataFrame or empty GDF.
    """
    # IGRAC GGMN GeoServer WFS
    igrac_url = (
        "https://ggmn.un-igrac.org/geoserver/ggmn/ows"
        "?service=WFS&version=1.0.0&request=GetFeature"
        "&typeName=ggmn:whymap_wsg&outputFormat=application/json"
        f"&bbox={minx},{miny},{maxx},{maxy},EPSG:4326"
        "&maxFeatures=50"
    )
    for url in [igrac_url]:
        try:
            r = requests.get(url, timeout=45)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                gdf = gpd.read_file(io.StringIO(r.text))
                if not gdf.empty:
                    gdf = gdf.to_crs("EPSG:4326")
                    return gdf
        except Exception as e:
            print(f"  ⚠ Aquifer API attempt failed: {e}")

    # Fallback: WHYMAP simplified raster-based bounding box query
    try:
        whymap_url = (
            "https://www.whymap.org/arcgis/rest/services/WHYMAP/WHYMAP_v1/MapServer/0/query"
            "?where=1%3D1"
            f"&geometry={minx},{miny},{maxx},{maxy}"
            "&geometryType=esriGeometryEnvelope"
            "&spatialRel=esriSpatialRelIntersects"
            "&outFields=*&f=geojson"
        )
        r = requests.get(whymap_url, timeout=45)
        if r.status_code == 200:
            gdf = gpd.read_file(io.StringIO(r.text))
            if not gdf.empty:
                return gdf.to_crs("EPSG:4326")
    except Exception as e:
        print(f"  ⚠ WHYMAP fallback failed: {e}")

    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


# ══════════════════════════════════════════════════════════════════════════════
#  5. TERRAIN PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════

def compute_terrain(dem_arr, transform, nodata=None):
    dem = dem_arr.astype(float)
    if nodata is not None:
        dem[dem == nodata] = np.nan

    xres =  transform[0]
    yres = -transform[4]

    dzdx = np.gradient(dem, axis=1) / xres
    dzdy = np.gradient(dem, axis=0) / yres
    slope = np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2)))

    az  = np.radians(315)
    alt = np.radians(45)
    slope_r = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
    aspect  = np.arctan2(-dzdx, dzdy)
    hillshade = np.clip(
        np.sin(alt)*np.sin(slope_r) + np.cos(alt)*np.cos(slope_r)*np.cos(az - aspect),
        0, 1,
    )

    extent = [
        transform[2],
        transform[2] + transform[0]*dem.shape[1],
        transform[5] + transform[4]*dem.shape[0],
        transform[5],
    ]
    return dem, slope, hillshade, extent


# ══════════════════════════════════════════════════════════════════════════════
#  6. MAP HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()


def add_north_arrow(ax):
    ax.annotate("N", xy=(0.96,0.96), xytext=(0.96,0.86),
                arrowprops=dict(facecolor="black", width=3, headwidth=9),
                ha="center", fontsize=13, fontweight="bold",
                xycoords=ax.transAxes)


def ws_boundary_gdf(poly):
    return gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")


# ══════════════════════════════════════════════════════════════════════════════
#  7. MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(lon, lat):
    t0 = time.time()

    with tempfile.TemporaryDirectory() as tmp:
        dem_prelim = os.path.join(tmp, "dem_prelim.tif")
        dem_full   = os.path.join(tmp, "dem_full.tif")

        # ── Step 1: small DEM for watershed delineation ───────────────────
        print(f"[1/6] Downloading preliminary DEM for ({lat},{lon}) …")
        s = lat - DEM_PRELIM_DEG
        n = lat + DEM_PRELIM_DEG
        w = lon - DEM_PRELIM_DEG
        e = lon + DEM_PRELIM_DEG
        download_dem_box(s, n, w, e, dem_prelim, res="SRTMGL3")

        # ── Step 2: delineate watershed ───────────────────────────────────
        print("[2/6] Delineating watershed …")
        ws_poly, area_km2 = delineate_watershed(lon, lat, dem_prelim)
        minx, miny, maxx, maxy = ws_poly.bounds

        # ── Step 3: full-res DEM for the watershed extent ─────────────────
        print("[3/6] Downloading full DEM for watershed extent …")
        pad = BUFFER_DEG
        download_dem_box(miny-pad, maxy+pad, minx-pad, maxx+pad, dem_full, res="SRTMGL1")

        # clip DEM to watershed
        ws_gdf = ws_boundary_gdf(ws_poly)
        with rasterio.open(dem_full) as src:
            ws_proj = ws_gdf.to_crs(src.crs)
            dem_clip, transform = rio_mask(src, ws_proj.geometry, crop=True)
            nodata = src.nodata

        dem, slope, hillshade, extent = compute_terrain(dem_clip[0], transform, nodata)

        # ── Step 4: fetch streams (OSM) ───────────────────────────────────
        print("[4/6] Fetching streams from OpenStreetMap …")
        streams_raw = fetch_streams_osm(minx-pad, miny-pad, maxx+pad, maxy+pad)
        if not streams_raw.empty:
            streams = gpd.clip(streams_raw, ws_poly)
        else:
            streams = streams_raw

        # ── Step 5: fetch aquifer ─────────────────────────────────────────
        print("[5/6] Fetching aquifer data …")
        aquifer_raw = fetch_aquifer(minx-pad, miny-pad, maxx+pad, maxy+pad)
        if not aquifer_raw.empty:
            aquifer = gpd.clip(aquifer_raw, ws_poly)
        else:
            aquifer = aquifer_raw

        # ── Step 6: render all maps ───────────────────────────────────────
        print("[6/6] Rendering maps …")

        point_gdf = gpd.GeoDataFrame(geometry=[Point(lon, lat)], crs="EPSG:4326")
        bx, by, bx2, by2 = ws_poly.bounds
        dx = (bx2-bx)*0.05; dy = (by2-by)*0.05

        ws_web     = ws_gdf.to_crs(epsg=3857)
        pt_web     = point_gdf.to_crs(epsg=3857)
        str_web    = streams.to_crs(epsg=3857) if not streams.empty else streams
        aq_web     = aquifer.to_crs(epsg=3857) if not aquifer.empty else aquifer

        maps = {}

        # ── MAP: DEM ──────────────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        im = ax.imshow(dem, cmap="terrain", extent=extent)
        plt.colorbar(im, ax=ax, fraction=0.046, label="Elevation (m)")
        ws_gdf.boundary.plot(ax=ax, color="black", linewidth=1.5)
        point_gdf.plot(ax=ax, color="red", markersize=100, zorder=5)
        ax.set_xlim(bx-dx, bx2+dx); ax.set_ylim(by-dy, by2+dy)
        ax.set_title("Digital Elevation Model (SRTM 30m)", fontsize=13, pad=10)
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        add_north_arrow(ax)
        plt.tight_layout()
        maps["dem"] = fig_to_b64(fig)

        # ── MAP: Slope ────────────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        im = ax.imshow(slope, cmap="RdYlGn_r", extent=extent, vmin=0, vmax=45)
        plt.colorbar(im, ax=ax, fraction=0.046, label="Slope (°)")
        ws_gdf.boundary.plot(ax=ax, color="black", linewidth=1.5)
        ax.set_xlim(bx-dx, bx2+dx); ax.set_ylim(by-dy, by2+dy)
        ax.set_title("Slope Map", fontsize=13, pad=10)
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        add_north_arrow(ax)
        plt.tight_layout()
        maps["slope"] = fig_to_b64(fig)

        # ── MAP: Hillshade ────────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        ax.imshow(hillshade, cmap="gray", extent=extent)
        ws_gdf.boundary.plot(ax=ax, color="white", linewidth=1.5)
        point_gdf.plot(ax=ax, color="red", markersize=100, zorder=5)
        ax.set_xlim(bx-dx, bx2+dx); ax.set_ylim(by-dy, by2+dy)
        ax.set_title("Hillshade (Az 315°, Alt 45°)", fontsize=13, pad=10)
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        add_north_arrow(ax)
        plt.tight_layout()
        maps["hillshade"] = fig_to_b64(fig)

        # ── MAP: Watershed boundary ───────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        ws_gdf.boundary.plot(ax=ax, color="#1a73e8", linewidth=2.5, label="Watershed")
        if not streams.empty:
            streams.plot(ax=ax, color="royalblue", linewidth=0.9, label=f"Streams ({len(streams)})")
        point_gdf.plot(ax=ax, color="red", markersize=100, zorder=5, label="Site")
        ax.set_xlim(bx-dx, bx2+dx); ax.set_ylim(by-dy, by2+dy)
        ax.set_title("Watershed & Stream Network (OSM)", fontsize=13, pad=10)
        ax.legend(fontsize=9)
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        add_north_arrow(ax)
        plt.tight_layout()
        maps["watershed"] = fig_to_b64(fig)

        # ── MAP: Aquifer ──────────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        ws_gdf.boundary.plot(ax=ax, color="black", linewidth=2, label="Watershed")
        if not aquifer.empty:
            col = [c for c in aquifer.columns if c not in ("geometry",)]
            if col:
                aquifer.plot(ax=ax, column=col[0], legend=True,
                             legend_kwds={"fontsize":8,"loc":"lower left"},
                             alpha=0.55)
            else:
                aquifer.plot(ax=ax, color="orange", alpha=0.55, label="Aquifer")
        if not streams.empty:
            streams.plot(ax=ax, color="royalblue", linewidth=0.8, alpha=0.7)
        point_gdf.plot(ax=ax, color="red", markersize=100, zorder=5, label="Site")
        ax.set_xlim(bx-dx, bx2+dx); ax.set_ylim(by-dy, by2+dy)
        aq_src = "IGRAC/WHYMAP" if not aquifer.empty else "No data in region"
        ax.set_title(f"Aquifer — {aq_src}", fontsize=13, pad=10)
        ax.legend(fontsize=9)
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        add_north_arrow(ax)
        plt.tight_layout()
        maps["aquifer"] = fig_to_b64(fig)

        # ── MAP: Satellite basemap ────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        ws_web.boundary.plot(ax=ax, color="yellow", linewidth=2)
        if not str_web.empty:
            str_web.plot(ax=ax, color="cyan", linewidth=0.8)
        if not aq_web.empty:
            aq_web.plot(ax=ax, alpha=0.3, color="orange")
        pt_web.plot(ax=ax, color="red", markersize=100, zorder=5)
        try:
            ctx.add_basemap(ax, source=ctx.providers.Esri.WorldImagery)
        except Exception:
            pass
        add_north_arrow(ax)
        ax.set_title("Satellite Imagery (Esri)", fontsize=13, pad=10)
        ax.axis("off")
        plt.tight_layout()
        maps["satellite"] = fig_to_b64(fig)

        # ── MAP: Topo basemap ─────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        ws_web.boundary.plot(ax=ax, color="black", linewidth=2)
        if not str_web.empty:
            str_web.plot(ax=ax, color="blue", linewidth=0.8)
        pt_web.plot(ax=ax, color="red", markersize=100, zorder=5)
        try:
            ctx.add_basemap(ax, source=ctx.providers.OpenTopoMap)
        except Exception:
            pass
        add_north_arrow(ax)
        ax.set_title("Topographic Map (OpenTopoMap)", fontsize=13, pad=10)
        ax.axis("off")
        plt.tight_layout()
        maps["topo"] = fig_to_b64(fig)

        # ── MAP: Combined terrain ─────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(9,9))
        ax.imshow(hillshade, cmap="gray", extent=extent)
        ax.imshow(slope, cmap="viridis", extent=extent, alpha=0.32)
        ws_gdf.boundary.plot(ax=ax, color="white", linewidth=1.5)
        if not streams.empty:
            streams.plot(ax=ax, color="cyan", linewidth=1, zorder=4)
        if not aquifer.empty:
            aquifer.plot(ax=ax, color="orange", alpha=0.3, zorder=3)
        point_gdf.plot(ax=ax, color="red", markersize=100, zorder=6)
        ax.set_xlim(bx-dx, bx2+dx); ax.set_ylim(by-dy, by2+dy)
        ax.set_title("Combined Terrain Map", fontsize=13, pad=10)
        ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
        add_north_arrow(ax)
        plt.tight_layout()
        maps["combined"] = fig_to_b64(fig)

        elapsed = round(time.time() - t0, 1)
        print(f"✅ Pipeline done in {elapsed}s")

        return {
            "maps": maps,
            "watershed_info": {
                "area_km2":       round(area_km2, 1),
                "elev_min_m":     round(float(np.nanmin(dem)), 1),
                "elev_max_m":     round(float(np.nanmax(dem)), 1),
                "elev_mean_m":    round(float(np.nanmean(dem)), 1),
                "slope_mean_deg": round(float(np.nanmean(slope)), 1),
                "stream_count":   len(streams),
                "has_aquifer":    not aquifer.empty,
                "bounds": {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
                "elapsed_s":      elapsed,
                "sources": {
                    "dem":       "OpenTopography SRTM GL1 (30m)",
                    "watershed": "pysheds D8 delineation from DEM",
                    "streams":   "OpenStreetMap Overpass API",
                    "aquifer":   "IGRAC GGMN / WHYMAP",
                    "basemaps":  "Esri WorldImagery + OpenTopoMap",
                },
            },
        }


# ══════════════════════════════════════════════════════════════════════════════
#  8. ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "mode": "api-only (no shapefiles)"})


@app.route("/api/report", methods=["POST"])
def report():
    body = request.get_json(force=True)
    lat  = float(body.get("lat", 0))
    lon  = float(body.get("lon", 0))

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    try:
        result = run_pipeline(lon, lat)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/report/pdf", methods=["POST"])
def report_pdf():
    body = request.get_json(force=True)
    lat  = float(body.get("lat", 0))
    lon  = float(body.get("lon", 0))

    try:
        result = run_pipeline(lon, lat)
        buf = _build_pdf(result, lat, lon)
        return send_file(
            buf, mimetype="application/pdf",
            as_attachment=True,
            download_name=f"GeoReport_{lat}_{lon}.pdf",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _build_pdf(result, lat, lon):
    buf = io.BytesIO()
    ws  = result["watershed_info"]
    with PdfPages(buf) as pdf:

        # Cover
        fig = plt.figure(figsize=(12,12))
        fig.patch.set_facecolor("#0e1117")
        ax  = fig.add_subplot(111)
        ax.set_facecolor("#0e1117"); ax.axis("off")
        ax.text(0.5, 0.72, "GeoReport", transform=ax.transAxes,
                ha="center", fontsize=50, fontweight="bold", color="white", style="italic")
        ax.text(0.5, 0.62, "Watershed & Terrain Analysis",
                transform=ax.transAxes, ha="center", fontsize=18, color="#4fd1a5")
        ax.text(0.5, 0.52, f"📍  {lat:.4f}°N,  {lon:.4f}°E",
                transform=ax.transAxes, ha="center", fontsize=14, color="#aab0be",
                fontfamily="monospace")
        lines = [
            f"Watershed area    : {ws['area_km2']:,.1f} km²",
            f"Elevation range   : {ws['elev_min_m']:.0f} – {ws['elev_max_m']:.0f} m",
            f"Mean slope        : {ws['slope_mean_deg']:.1f}°",
            f"Stream segments   : {ws['stream_count']}",
            f"Aquifer present   : {'Yes' if ws['has_aquifer'] else 'No'}",
            f"DEM source        : {ws['sources']['dem']}",
            f"Watershed method  : {ws['sources']['watershed']}",
            f"Streams source    : {ws['sources']['streams']}",
            f"Aquifer source    : {ws['sources']['aquifer']}",
        ]
        ax.text(0.5, 0.26, "\n".join(lines), transform=ax.transAxes,
                ha="center", fontsize=11, color="#7b8494",
                fontfamily="monospace", linespacing=1.9, va="top")
        pdf.savefig(fig, facecolor="#0e1117"); plt.close()

        # Map pages
        titles = {
            "dem":       "Digital Elevation Model",
            "slope":     "Slope Map",
            "hillshade": "Hillshade",
            "watershed": "Watershed & Streams",
            "aquifer":   "Aquifer",
            "satellite": "Satellite Imagery",
            "topo":      "Topographic Map",
            "combined":  "Combined Terrain",
        }
        for key, data_url in result["maps"].items():
            import PIL.Image
            img_data = base64.b64decode(data_url.split(",")[1])
            img = PIL.Image.open(io.BytesIO(img_data))
            fig, ax = plt.subplots(figsize=(12,12))
            ax.imshow(np.array(img))
            ax.axis("off")
            ax.set_title(titles.get(key, key), fontsize=14, pad=8)
            pdf.savefig(fig, bbox_inches="tight"); plt.close()

    buf.seek(0)
    return buf


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=False)
