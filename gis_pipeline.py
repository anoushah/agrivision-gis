from __future__ import annotations
import json, re, time
from dataclasses import dataclass, asdict
from datetime import datetime
from functools import lru_cache
import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import Point, MultiPoint, Polygon, box, mapping
from shapely.ops import transform, unary_union
from shapely.prepared import prep
from shapely.validation import explain_validity, make_valid
from sklearn.cluster import DBSCAN


# ---------------- Config (step 7: yeh values apni fasal/sprayer se set karni hain) -------------
@dataclass
class Config:
    margin_m: float = 5.0            # detection error + spray drift se zyada rakho
    cell_m: float = 10.0             # sprayer ki swath width ke barabar rakho
    min_conf: float = 0.5
    min_frames: int = 3              # ek track ko itne frames mein dikhna chahiye
    dedupe_m: float = 1.0            # is distance ke andar ke points ek hi pest
    cluster_eps_m: float = 15.0      # is se door points alag cluster
    min_cluster_points: int = 1
    hull_mode: str = "concave"       # "convex" ya "concave"
    concave_ratio: float = 0.3       # chota = zyada tight shape
    boundary_setback_m: float = 2.0  # boundary se andar ki taraf gap (inner boundary)
    min_cell_fraction: float = 0.25  # cell ka kam az kam itna hissa area mein ho
    dose_l_per_ha: float = 0.0       # label ke mutabiq bharo
    max_flight_alt_m: float = 5.0    # spray altitude
    no_spray_buffer_m: float = 5.0   # no-spray area ke itne paas ke cells pe warning
    auto_fix_boundary: bool = False


# ---------------- Local metric projection (UTM, southern hemisphere handle) -------------
def utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


class Proj:
    def __init__(self, lon: float, lat: float):
        epsg = utm_epsg(lon, lat)
        self.epsg = epsg
        self.to_m = Transformer.from_crs(4326, epsg, always_xy=True)
        self.to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)

    def fwd(self, g):
        return transform(self.to_m.transform, g)

    def inv(self, g):
        return transform(self.to_ll.transform, g)


# ---------------- Step 13: boundary validation -------------
def validate_boundary(coords_lonlat, cfg: Config):
    """coords_lonlat: [(lon, lat), ...]. Returns (Polygon, warnings)."""
    warns = []
    if len(coords_lonlat) < 3:
        raise ValueError("Boundary mein kam az kam 3 points chahiye")
    lons = [c[0] for c in coords_lonlat]
    lats = [c[1] for c in coords_lonlat]
    if max(lons) - min(lons) > 180:
        raise ValueError("Antimeridian ke paas boundary supported nahi hai")
    if max(abs(v) for v in lats) > 80:
        raise ValueError("Poles ke paas boundary supported nahi hai")
    poly = Polygon(coords_lonlat)
    if not poly.is_valid:
        why = explain_validity(poly)
        if not cfg.auto_fix_boundary:
            raise ValueError(f"Boundary invalid hai ({why}). Theek karo ya auto_fix_boundary=True karo")
        fixed = make_valid(poly)
        parts = [g for g in getattr(fixed, "geoms", [fixed]) if g.geom_type == "Polygon"]
        if not parts:
            raise ValueError("Boundary theek nahi ho saki")
        biggest = max(parts, key=lambda g: g.area)
        kept = biggest.area / sum(g.area for g in parts)
        warns.append(f"Boundary auto-fix hui ({why}). Sirf sab se bada hissa rakha, {kept:.0%} area bacha. Shape check karo.")
        poly = biggest
    return poly, warns


# ---------------- Step 2: SRT parser (DJI style) -------------
@dataclass
class FramePose:
    lat: float
    lon: float
    rel_alt: float
    yaw_deg: float | None = None     # gimbal/camera heading, north se clockwise
    pitch_deg: float | None = None   # -90 = seedha neeche


_num = r"(-?\d+(?:\.\d+)?)"


def parse_srt(path: str):
    text = open(path, encoding="utf-8", errors="ignore").read()
    poses = []
    for block in re.split(r"\n\s*\n", text):
        la = re.search(r"\[latitude\s*:\s*" + _num, block)
        lo = re.search(r"\[longitude\s*:\s*" + _num, block)
        if not (la and lo):
            m = re.search(r"GPS\s*\(\s*" + _num + r"\s*,\s*" + _num, block)  # purana format: lon, lat
            if not m:
                continue
            lon, lat = float(m.group(1)), float(m.group(2))
        else:
            lat, lon = float(la.group(1)), float(lo.group(1))
        alt = re.search(r"rel_alt\s*:\s*" + _num, block)
        yaw = re.search(r"gb_yaw\s*:\s*" + _num, block)
        pit = re.search(r"gb_pitch\s*:\s*" + _num, block)
        poses.append(FramePose(lat, lon,
                               float(alt.group(1)) if alt else float("nan"),
                               float(yaw.group(1)) if yaw else None,
                               float(pit.group(1)) if pit else None))
    if not poses:
        raise ValueError("SRT mein GPS data nahi mila. Format check karo ya GCP use karo")
    return poses


def pixel_to_lonlat(px, py, pose: FramePose, img_w, img_h, hfov_deg,
                    default_yaw=0.0, default_pitch=-90.0):
    """Pixel -> ground (lon, lat). Camera ki position, height, yaw aur tilt sab use hote hain.
    Flat ground maana gaya hai. Horizon ke upar wale pixel pe None."""
    yaw = np.radians(pose.yaw_deg if pose.yaw_deg is not None else default_yaw)
    pit = np.radians(pose.pitch_deg if pose.pitch_deg is not None else default_pitch)
    f = (img_w / 2) / np.tan(np.radians(hfov_deg) / 2)
    xn, yn = (px - img_w / 2) / f, (py - img_h / 2) / f

    fwd = np.array([np.sin(yaw) * np.cos(pit), np.cos(yaw) * np.cos(pit), np.sin(pit)])  # E, N, U
    right = np.array([np.cos(yaw), -np.sin(yaw), 0.0])
    up = np.cross(right, fwd)
    ray = fwd + xn * right - yn * up
    if ray[2] >= -1e-6 or not np.isfinite(pose.rel_alt):
        return None
    t = pose.rel_alt / -ray[2]
    e, n = t * ray[0], t * ray[1]
    local = Transformer.from_crs(
        f"+proj=aeqd +lat_0={pose.lat} +lon_0={pose.lon} +datum=WGS84", 4326, always_xy=True)
    lon, lat = local.transform(e, n)
    return lon, lat


# Fallback: Ground Control Points (kam az kam 4 pixel <-> GPS jode)
class GCPGeoref:
    def __init__(self, pixel_pts, lonlat_pts):
        import cv2
        if len(pixel_pts) < 4:
            raise ValueError("Kam az kam 4 GCP chahiye")
        self.proj = Proj(*lonlat_pts[0])
        xy = np.array([self.proj.to_m.transform(lo, la) for lo, la in lonlat_pts])
        self.origin = xy[0]
        self.H, _ = cv2.findHomography(np.float32(pixel_pts), np.float32(xy - self.origin),
                                       cv2.RANSAC if len(pixel_pts) > 4 else 0)

    def __call__(self, px, py):
        import cv2
        out = cv2.perspectiveTransform(np.float32([[[px, py]]]), self.H)[0, 0] + self.origin
        return self.proj.to_ll.transform(float(out[0]), float(out[1]))


# ---------------- Step 5: detections ko filter karna -------------
@dataclass
class Detection:
    track_id: int
    conf: float
    lon: float
    lat: float
    frame: int = 0


def filter_detections(dets, cfg: Config, proj: Proj, boundary_m=None):
    tracks = {}
    for d in dets:
        if d.conf >= cfg.min_conf:
            tracks.setdefault(d.track_id, []).append(proj.to_m.transform(d.lon, d.lat))
    pts = [np.median(np.array(v), axis=0) for v in tracks.values() if len(v) >= cfg.min_frames]
    if not pts:
        return np.empty((0, 2))
    pts = np.array(pts)
    if boundary_m is not None:
        pb = prep(boundary_m)
        pts = pts[[pb.contains(Point(p)) for p in pts]]
        if len(pts) == 0:
            return np.empty((0, 2))
    labels = DBSCAN(eps=cfg.dedupe_m, min_samples=1).fit_predict(pts)  # ID badalne se bane duplicate
    return np.array([pts[labels == l].mean(axis=0) for l in np.unique(labels)])


# ---------------- Step 4: cluster + alag hull -------------
def _hull(pts, cfg: Config):
    if len(pts) == 1:
        return Point(pts[0])
    mp = MultiPoint([tuple(p) for p in pts])
    if cfg.hull_mode == "concave" and len(pts) >= 4:
        return shapely.concave_hull(mp, ratio=cfg.concave_ratio)
    return mp.convex_hull   # 2 points ya collinear mein LineString milti hai, buffer sambhal leta hai


def build_spray_area(pts, boundary_m, no_spray_m, cfg: Config):
    inner = boundary_m.buffer(-cfg.boundary_setback_m)
    if inner.is_empty:
        raise ValueError("boundary_setback_m boundary ke muqable mein bohot bada hai")
    labels = DBSCAN(eps=cfg.cluster_eps_m, min_samples=1).fit_predict(pts)
    shapes = []
    for l in np.unique(labels):
        c = pts[labels == l]
        if len(c) >= cfg.min_cluster_points:
            shapes.append(_hull(c, cfg).buffer(cfg.margin_m))
    if not shapes:
        return Polygon()
    area = unary_union(shapes).intersection(inner)
    for ns in no_spray_m:
        area = area.difference(ns)
    return area


# ---------------- Step 9: ek hi grid, isi se dose ----------------
def make_grid(area, pts, cfg: Config):
    if area.is_empty:
        return []
    pa = prep(area)
    minx, miny, maxx, maxy = area.bounds
    cells, cs = [], cfg.cell_m
    for x in np.arange(minx, maxx, cs):
        for y in np.arange(miny, maxy, cs):
            c = box(x, y, x + cs, y + cs)
            if not pa.intersects(c):
                continue
            frac = c.intersection(area).area / c.area
            if frac < cfg.min_cell_fraction:
                continue
            n = int(((pts[:, 0] >= x) & (pts[:, 0] < x + cs) & (pts[:, 1] >= y) & (pts[:, 1] < y + cs)).sum())
            dose = cfg.dose_l_per_ha * (c.area * frac / 10000.0)
            cells.append({"id": len(cells), "geom": c, "fraction": round(frac, 3),
                          "infected_points": n, "dose_l": round(dose, 4)})
    return cells


def zone_summary(cells, boundary_m, n=3):
    """Purani 3x3 zone table ab grid se nikalti hai, to dono mein farq nahi aa sakta."""
    minx, miny, maxx, maxy = boundary_m.bounds
    out = {}
    for c in cells:
        p = c["geom"].centroid
        r = min(n - 1, int((p.y - miny) / (maxy - miny) * n))
        k = min(n - 1, int((p.x - minx) / (maxx - minx) * n))
        z = out.setdefault(f"row{r}_col{k}", {"cells": 0, "infected_points": 0, "dose_l": 0.0})
        z["cells"] += 1
        z["infected_points"] += c["infected_points"]
        z["dose_l"] = round(z["dose_l"] + c["dose_l"], 4)
    return out


# ---------------- Step 3: no-spray check ----------------
def check_no_spray(cells, no_spray_m, cfg: Config):
    bad = []
    for ns in no_spray_m:
        near = ns.buffer(cfg.no_spray_buffer_m)
        bad += [c["id"] for c in cells if c["geom"].intersects(near)]
    return sorted(set(bad))


# ---------------- Main entry ----------------
def plan_spray(boundary_ll, dets, no_spray_ll=(), cfg: Config = Config()):
    poly_ll, warns = validate_boundary(boundary_ll, cfg)
    c = poly_ll.centroid
    proj = Proj(c.x, c.y)
    boundary_m = proj.fwd(poly_ll)
    ns_m = [proj.fwd(Polygon(p)) for p in no_spray_ll]
    pts = filter_detections(dets, cfg, proj, boundary_m)
    plan = {"proj": proj, "boundary_m": boundary_m, "no_spray_m": ns_m, "points_m": pts,
            "area_m": Polygon(), "cells": [], "warnings": warns, "cfg": cfg}
    if len(pts) == 0:
        warns.append("Filter ke baad koi valid infected point nahi bacha")
        return plan
    plan["area_m"] = build_spray_area(pts, boundary_m, ns_m, cfg)
    plan["cells"] = make_grid(plan["area_m"], pts, cfg)
    near = check_no_spray(plan["cells"], ns_m, cfg)
    if near:
        warns.append(f"{len(near)} cells no-spray area ke {cfg.no_spray_buffer_m} m ke andar hain: {near[:10]}")
    if cfg.dose_l_per_ha == 0:
        warns.append("dose_l_per_ha 0 hai. Dose label se set karo")
    if not plan["cells"]:
        warns.append("Grid khali hai")
    return plan


# ---------------- Step 8: export ----------------
def to_geojson(plan):
    p = plan["proj"]
    feats = []

    def add(g, kind, props=None):
        feats.append({"type": "Feature", "geometry": mapping(p.inv(g)), "properties": {"kind": kind, **(props or {})}})

    add(plan["boundary_m"], "boundary")
    for ns in plan["no_spray_m"]:
        add(ns, "no_spray")
    if not plan["area_m"].is_empty:
        add(plan["area_m"], "spray_area")
    for pt in plan["points_m"]:
        add(Point(pt), "infected_point")
    for c in plan["cells"]:
        add(c["geom"], "cell", {k: c[k] for k in ("id", "fraction", "infected_points", "dose_l")})
    return {"type": "FeatureCollection", "features": feats}


def to_kml(plan):
    p = plan["proj"]
    out = ['<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>']
    for c in plan["cells"]:
        ring = " ".join(f"{x},{y},0" for x, y in p.inv(c["geom"]).exterior.coords)
        out.append(f'<Placemark><name>cell_{c["id"]}</name>'
                   f'<description>dose_l={c["dose_l"]}</description>'
                   f'<Polygon><outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates>'
                   f'</LinearRing></outerBoundaryIs></Polygon></Placemark>')
    out.append("</Document></kml>")
    return "".join(out)


def ordered_cells(cells, cell_m):
    """Serpentine (lawnmower) order."""
    rows = {}
    for c in cells:
        p = c["geom"].centroid
        rows.setdefault(round(p.y / cell_m), []).append(c)
    out = []
    for i, r in enumerate(sorted(rows)):
        row = sorted(rows[r], key=lambda c: c["geom"].centroid.x, reverse=bool(i % 2))
        out += row
    return out


def to_waypoints(plan, alt_m=None):
    """Mission Planner / QGroundControl 'QGC WPL 110' format (ArduPilot).
    DJI ke liye KML/Litchi alag convert karna hoga."""
    cfg = plan["cfg"]
    p = plan["proj"]
    alt = alt_m if alt_m is not None else cfg.max_flight_alt_m
    seq = ordered_cells(plan["cells"], cfg.cell_m)
    if not seq:
        raise ValueError("Export ke liye cells nahi hain")
    first = p.inv(seq[0]["geom"].centroid)
    lines = ["QGC WPL 110", f"0\t1\t0\t16\t0\t0\t0\t0\t{first.y:.8f}\t{first.x:.8f}\t0\t1"]
    for i, c in enumerate(seq, 1):
        ll = p.inv(c["geom"].centroid)
        lines.append(f"{i}\t0\t3\t16\t0\t0\t0\t0\t{ll.y:.8f}\t{ll.x:.8f}\t{alt}\t1")
    return "\n".join(lines) + "\n"


# ---------------- Step 3: approval ke liye plot ----------------
def plot_plan(plan):
    import matplotlib
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 7))

    def draw(g, **kw):
        for part in getattr(g, "geoms", [g]):
            if part.geom_type == "Polygon":
                ax.plot(*part.exterior.xy, **kw)

    draw(plan["boundary_m"], color="black", label="boundary")
    draw(plan["boundary_m"].buffer(-plan["cfg"].boundary_setback_m), color="gray", ls="--", label="inner boundary")
    for ns in plan["no_spray_m"]:
        draw(ns, color="red", label="no-spray")
    for c in plan["cells"]:
        ax.fill(*c["geom"].exterior.xy, alpha=0.35, color="orange")
    if len(plan["points_m"]):
        ax.scatter(plan["points_m"][:, 0], plan["points_m"][:, 1], c="blue", s=12, label="infected")
    ax.set_aspect("equal")
    ax.legend(loc="best")
    return fig


# ---------------- Step 10: save/load ----------------
def save_report(path, plan, meta=None):
    data = {"saved_at": datetime.now().isoformat(), "meta": meta or {},
            "config": asdict(plan["cfg"]), "warnings": plan["warnings"],
            "geojson": to_geojson(plan)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_report(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------- Step 12: timing ----------------
def timed(fn):
    def w(*a, **k):
        t = time.perf_counter()
        r = fn(*a, **k)
        print(f"[timing] {fn.__name__}: {time.perf_counter() - t:.3f}s")
        return r
    return w


# ---------------- Step 14: location search (Nominatim: max 1 request/sec, User-Agent zaroori) ----------------
_last = [0.0]


@lru_cache(maxsize=256)
def search_place(q: str, user_agent="pest-spray-app (apna email likho)"):
    import requests
    wait = 1.1 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q": q, "format": "json", "limit": 5},
                     headers={"User-Agent": user_agent}, timeout=10)
    r.raise_for_status()
    return [(x["display_name"], float(x["lon"]), float(x["lat"])) for x in r.json()]
