"""
Crop Pest & Disease Detection with Precision Spray Simulation (AgriVision)
------------------------------------------------------------------------
Supports Multi-Signal Dual-Mode Analysis:
1. Ground-level YOLO Object Detection (Expanded IP102 & baseline pest/disease classes)
2. Aerial / Drone YOLO Crop Stress Pattern Detection (Agriculture-Vision 2021 anomalies)
3. HSV Vegetation Health Signal (Chlorosis yellowing & Necrosis brown spot/bare soil detection)
4. Texture Anomaly Analysis (Normalized gradient variance for leaf deformation/spots)
5. Wilting Symptom Proxy (Intra-canopy deep shadow proportion & contour fragmentation)
6. CLAHE contrast enhancement (pre-detection) for faint spots/discoloration
7. SAHI sliced inference (tiled detection) for small/distant pests -- GPU only by default

All signals feed into the SAME 3x3 zone severity system. On top of that, a proper GIS layer
(shapely + pyproj) turns the result into real map geometry:

  outer boundary (user)  ->  detection runs only inside it
  infected points        ->  every unique pest / stress spot with its real lat/lon
  inner boundary         ->  buffered convex hull of the infected points (clipped to the outer boundary)
  spray grid             ->  grid cells generated automatically inside the inner boundary

GROUND-FIXED ZONES: the 3x3 zones are laid over the ground covered by the whole flight (not over
each camera frame). Drone motion is estimated with optical flow, so a zone keeps the same number
and GPS position while the drone moves, and overlapping frames are merged (see WORLD-FIXED ZONES).

PERFORMANCE NOTES (CPU machines):
- Models are cached (lru_cache) so they load ONCE per process, not per run.
- FRAME_SKIP defaults to 5 on CPU (process every 5th frame).
- SAHI tiled inference is DISABLED automatically on CPU (it is ~12x slower per
  frame). Set FORCE_SAHI = True if you explicitly want it back.
- Frames are downscaled to PROCESS_MAX_WIDTH before inference.
- Pass progress_callback(done, total) to process_video() for a live progress bar.
"""

import os
import cv2
import random
import numpy as np
import json
import base64
import argparse
import math
from pathlib import Path
from functools import lru_cache

import torch
from ultralytics import YOLO

from shapely.geometry import Polygon, Point, MultiPoint, box
from shapely.ops import transform as shp_transform, unary_union
from shapely.prepared import prep
from pyproj import Transformer, Geod

from sahi import AutoDetectionModel
from sahi.predict import get_sliced_prediction

import subprocess
try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_EXE = None


# ---------------------- DEVICE / THREADS ----------------------
HAS_CUDA = torch.cuda.is_available()
DEVICE = "cuda:0" if HAS_CUDA else "cpu"

# CPU par PyTorch ko saare cores use karne dein (bara farq padta hai)
try:
    torch.set_num_threads(os.cpu_count() or 4)
except Exception:
    pass

# ---------------------- CONFIG ----------------------
DEFAULT_GROUND_MODEL = r"E:\Ideofuzion\AgriVision\AgriVision\runs\detect\runs\agrivision_local_v2\weights\best.pt"
LEGACY_MODEL = "best.pt"
DEFAULT_AERIAL_MODEL = "aerial_best.pt"

DEFAULT_VIDEO = "test_videos/field_footage.mp4"
DEFAULT_DRONE_VIDEO = "test_videos/crop_field_drone_30s.mp4"
OUTPUT_VIDEO = "output_annotated.mp4"
REPORT_PATH = "spray_report.json"

# --- SPEED KNOBS ---
# GPU hai to har frame; CPU hai to har 5th frame (accuracy pe bohot kam asar,
# kyunke severity waise bhi p95 aggregate se nikalti hai)
FRAME_SKIP = 1 if HAS_CUDA else 5

# Inference resolution. CPU par 480 kaafi tez hai, GPU par 640 rakh sakte hain.
IMG_SIZE = 640 if HAS_CUDA else 480

# Frame ko is width se bara ho to downscale kar dein (inference + zone analysis dono tez)
PROCESS_MAX_WIDTH = 1280 if HAS_CUDA else 960

# SAHI tiled inference: CPU par ~12x slower per frame. Default: sirf GPU par ON.
FORCE_SAHI = False
USE_SAHI = HAS_CUDA or FORCE_SAHI

CONF_THRESHOLD = 0.55

# Adaptive Box Area Filter (Mode-Aware)
MAX_BOX_FRAME_RATIO_GROUND = 0.10
MAX_BOX_FRAME_RATIO_AERIAL = 0.60
HIGH_CONF_REQUIRED_FOR_LARGE_BOX = 0.85

GRID_ROWS = 3
GRID_COLS = 3

# --- GIS (no demo coordinates: every coordinate comes from the user's outer boundary) ---
DEFAULT_INNER_MARGIN_M = 5.0     # buffer added around the infected points' convex hull
DEFAULT_GRID_CELL_M = 10.0       # spray-grid cell size inside the inner boundary
MAX_GRID_CELLS = 150             # the cell size is increased automatically above this
MIN_GRID_CELL_COVER = 0.25       # a grid cell is kept only if >= 25% of it lies inside the inner boundary
GEO_FIT_MODE = "contain"         # "contain": uniform scale (keeps shapes), "stretch": fill the boundary bbox
_GEOD = Geod(ellps="WGS84")

# --- WORLD-FIXED ZONES (drone motion compensation) ---
# The 3x3 zones are laid over the GROUND covered by the whole flight instead of over each camera
# frame, so a zone keeps describing the same piece of field while the drone moves.
MOTION_COMPENSATION = True          # False = old behaviour (zones fixed to the camera frame)
MOTION_EST_WIDTH = 320              # frames are downscaled to this width for motion estimation (fast)
MIN_CELL_VISIBLE_FRACTION = 0.04    # a zone counts in a frame only if >= 4% of the frame shows it
TRACK_MATCH_DISTANCE_WORLD_PX = 90.0  # same-class detections closer than this (ground px) = same pest
PASS1_PROGRESS_SHARE = 0.10         # share of the progress bar used by the motion-estimation pass



# ==================================================================
# WORLD-FIXED ZONES (DRONE MOTION COMPENSATION)
# ==================================================================
# PROBLEM: a 3x3 grid fixed to the CAMERA FRAME does not describe the field. When the drone
# moves, "zone 5" (image centre) shows different ground in every frame, the same ground shows
# up in different zones, and overlapping frames count the same pest twice.
#
# FIX: estimate the camera motion between frames and put every frame into ONE common world
# coordinate system (world = pixel space of the first frame). The 3x3 zones are laid over the
# whole ground area covered by the flight, so:
#   * a zone is always the same piece of ground (same number Z1..Z9, same GPS position),
#   * each frame only contributes the part of a zone that it can actually see,
#   * where frames overlap, the observations of a zone are merged (percentile over all frames),
#   * pests are tracked in world coordinates, so they are counted once, not once per frame.

class GlobalMotionEstimator:
    """
    Frame-to-frame camera motion (translation + rotation + uniform scale) for a top-down drone
    view, estimated with sparse Lucas-Kanade optical flow and a RANSAC similarity transform.

    update(frame) returns a 3x3 matrix H that maps FRAME pixel coordinates -> WORLD coordinates.
    The first frame defines the world (H = identity). If a step cannot be estimated (blur, no
    texture) the previous motion is reused ("constant velocity") and counted in `failures`.

    LIMITATION: motion is chained frame by frame, so long flights can slowly drift. Real GPS/IMU
    telemetry would remove the drift; this needs no extra hardware or files.
    """

    def __init__(self, est_width=MOTION_EST_WIDTH):
        self.est_width = est_width
        self.prev_gray = None
        self.H_world = np.eye(3, dtype=np.float64)
        self.last_step = np.eye(3, dtype=np.float64)   # last good (current -> previous) transform
        self.failures = 0
        self.frames = 0

    def update(self, frame_bgr):
        self.frames += 1
        h, w = frame_bgr.shape[:2]
        s = self.est_width / float(w) if w > self.est_width else 1.0

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if s != 1.0:
            gray = cv2.resize(gray, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)

        if self.prev_gray is None:
            self.prev_gray = gray
            return self.H_world.copy()

        step = self._estimate_step(self.prev_gray, gray, s)
        if step is None:
            self.failures += 1
            step = self.last_step
        else:
            self.last_step = step

        self.H_world = self.H_world @ step
        self.prev_gray = gray
        return self.H_world.copy()

    @staticmethod
    def _estimate_step(prev_gray, cur_gray, s):
        """Transform mapping current-frame pixels -> previous-frame pixels (full resolution), or None."""
        pts_prev = cv2.goodFeaturesToTrack(prev_gray, maxCorners=400, qualityLevel=0.01,
                                           minDistance=8, blockSize=7)
        if pts_prev is None or len(pts_prev) < 12:
            return None

        pts_cur, status, _ = cv2.calcOpticalFlowPyrLK(
            prev_gray, cur_gray, pts_prev, None, winSize=(21, 21), maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
        if pts_cur is None or status is None:
            return None

        ok = status.reshape(-1) == 1
        if int(ok.sum()) < 12:
            return None

        src = pts_cur[ok].reshape(-1, 2)     # points in the current frame
        dst = pts_prev[ok].reshape(-1, 2)    # the same points in the previous frame
        A, inliers = cv2.estimateAffinePartial2D(
            src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0, maxIters=2000, confidence=0.99)
        if A is None or inliers is None or int(inliers.sum()) < max(10, int(0.3 * int(ok.sum()))):
            return None

        scale_change = math.hypot(A[0, 0], A[1, 0])
        if not (0.8 <= scale_change <= 1.25):      # implausible zoom between two frames -> reject
            return None

        # The estimate lives in downscaled pixels; convert it back to full-resolution pixels
        S = np.diag([s, s, 1.0])
        A3 = np.vstack([A, [0.0, 0.0, 1.0]])
        return np.linalg.inv(S) @ A3 @ S


class WorldGrid:
    """
    GRID_ROWS x GRID_COLS zones laid over the WHOLE ground area covered by the flight
    (bounding box of all camera footprints, in world pixels).
    """

    def __init__(self, homographies, frame_w, frame_h, rows, cols):
        corners = np.array([[0, 0], [frame_w, 0], [frame_w, frame_h], [0, frame_h]],
                           dtype=np.float64).reshape(-1, 1, 2)
        footprints = [cv2.perspectiveTransform(corners, np.asarray(H, dtype=np.float64)).reshape(-1, 2)
                      for H in homographies]
        all_pts = np.vstack(footprints)

        self.x0, self.y0 = float(all_pts[:, 0].min()), float(all_pts[:, 1].min())
        self.x1, self.y1 = float(all_pts[:, 0].max()), float(all_pts[:, 1].max())
        self.rows, self.cols = rows, cols
        self.cell_w = max(1e-6, (self.x1 - self.x0) / cols)
        self.cell_h = max(1e-6, (self.y1 - self.y0) / rows)

    @staticmethod
    def point_to_world(H, x, y):
        """Map one frame pixel (x, y) to world coordinates using the frame's matrix H."""
        p = H @ np.array([x, y, 1.0])
        return float(p[0] / p[2]), float(p[1] / p[2])

    def world_to_cell(self, wx, wy):
        """Ground-fixed zone (row, col) that contains the world point (clamped to the grid)."""
        col = int(min(max(math.floor((wx - self.x0) / self.cell_w), 0), self.cols - 1))
        row = int(min(max(math.floor((wy - self.y0) / self.cell_h), 0), self.rows - 1))
        return row, col

    def cell_corners(self, row, col):
        xa, ya = self.x0 + col * self.cell_w, self.y0 + row * self.cell_h
        xb, yb = xa + self.cell_w, ya + self.cell_h
        return np.array([[xa, ya], [xb, ya], [xb, yb], [xa, yb]], dtype=np.float64)

    def project_cell(self, row, col, H_inv, width, height, min_visible_px):
        """
        Project one ground zone into the current camera frame (H_inv: world -> frame pixels).
        Returns None if the zone is not (sufficiently) visible, otherwise a dict with:
          x0,y0,x1,y1  bounding rectangle of the VISIBLE part (clipped to the frame)
          mask         uint8 mask (rect-sized), 255 = pixels that belong to this zone
          poly         zone outline in frame pixels (for drawing)
          visible_px   number of visible zone pixels
        """
        poly = cv2.perspectiveTransform(self.cell_corners(row, col).reshape(-1, 1, 2),
                                        np.asarray(H_inv, dtype=np.float64)).reshape(-1, 2)
        poly = np.clip(poly, -1e5, 1e5)

        xa = max(0, int(math.floor(poly[:, 0].min())))
        xb = min(width, int(math.ceil(poly[:, 0].max())))
        ya = max(0, int(math.floor(poly[:, 1].min())))
        yb = min(height, int(math.ceil(poly[:, 1].max())))
        if xb - xa < 4 or yb - ya < 4:
            return None

        mask = np.zeros((yb - ya, xb - xa), dtype=np.uint8)
        poly_int = np.round(poly).astype(np.int32)
        cv2.fillConvexPoly(mask, poly_int - np.array([xa, ya], dtype=np.int32), 255)
        visible_px = cv2.countNonZero(mask)
        if visible_px < min_visible_px:
            return None

        return {"x0": xa, "y0": ya, "x1": xb, "y1": yb,
                "mask": mask, "poly": poly_int, "visible_px": visible_px}


# ==================================================================
# GIS LAYER (shapely + pyproj)  -- the ONLY geometry implementation in the project
# ==================================================================
# All coordinates come from the user's outer boundary. Nothing falls back to a fixed location.
#   * Outer boundary  : shapely Polygon (lon, lat), built from the vertices the user drew.
#   * Geo-referencing : the flown ground area (world pixels) is fitted onto the boundary in the
#                       local UTM zone (metres), north-up. No GPS/IMU telemetry is needed, but the
#                       accuracy is only as good as the assumption "the video covers the boundary".
#   * Infected points : unique detections (mean ground position) + crop-stress centroids.
#   * Inner boundary  : convex hull of the infected points + margin, clipped to the outer boundary.
#   * Spray grid      : square cells generated inside the inner boundary.

def geo_distance_m(lat1, lon1, lat2, lon2):
    """Geodesic distance in metres on the WGS84 ellipsoid."""
    _, _, dist = _GEOD.inv(lon1, lat1, lon2, lat2)
    return float(dist)


def _make_projectors(lat, lon):
    """(to_metres, to_lonlat) shapely-geometry functions using the local UTM zone."""
    zone = max(1, min(60, int((lon + 180.0) // 6.0) + 1))
    epsg = (32600 if lat >= 0 else 32700) + zone
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return (lambda g: shp_transform(fwd.transform, g)), (lambda g: shp_transform(inv.transform, g)), fwd, inv


def _polygons_of(geom):
    """List of non-empty Polygons contained in any shapely geometry."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom] if geom.area > 0 else []
    return [p for g in getattr(geom, "geoms", []) for p in _polygons_of(g)]


def _only_polygons(geom):
    """Drop stray lines/points from an intersection result."""
    return unary_union(_polygons_of(geom))


def _rings(geom_ll):
    """Exterior rings as [[lat, lon], ...] (one list per polygon) for folium."""
    return [[[y, x] for x, y in p.exterior.coords] for p in _polygons_of(geom_ll)]


def _inner_point(geom):
    """A representative point that is guaranteed to lie inside the geometry."""
    c = geom.centroid
    return c if geom.contains(c) else geom.representative_point()


def boundary_polygon(boundary):
    """Valid shapely Polygon (lon, lat) from boundary vertices, or None.
    Accepts [{"lat":..,"lon":..}, ...] or [(lat, lon), ...]."""
    if not boundary:
        return None
    pts = []
    for p in boundary:
        if isinstance(p, dict):
            lat, lon = p.get("lat"), p.get("lon")
        else:
            lat, lon = p[0], p[1]
        if lat is None or lon is None:
            continue
        pts.append((float(lon), float(lat)))
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    polys = _polygons_of(poly)
    if not polys:
        return None
    return max(polys, key=lambda q: q.area)


def view_for_polygon(poly_ll):
    """(centre [lat, lon], zoom) that fits a lon/lat polygon on the map."""
    minx, miny, maxx, maxy = poly_ll.bounds
    clat, clon = (miny + maxy) / 2.0, (minx + maxx) / 2.0
    extent_m = max(
        geo_distance_m(miny, minx, maxy, minx),
        geo_distance_m(clat, minx, clat, maxx),
        1.0,
    )
    zoom = math.log2(156543.03 * max(math.cos(math.radians(clat)), 0.05) * 600.0 / extent_m)
    return [clat, clon], int(max(3, min(19, math.floor(zoom))))


class GeoReference:
    """
    World pixels (flown ground area) -> metres (local UTM) -> lon/lat, fitted onto the outer boundary.
    The flown area is centred on the boundary's bounding box, north-up (world y grows to the south).
    """

    def __init__(self, outer_ll, world):
        self.outer_ll = outer_ll
        ctr = outer_ll.centroid
        self.to_m, self.to_ll, self._fwd, self._inv = _make_projectors(ctr.y, ctr.x)
        self.outer_m = self.to_m(outer_ll)
        self._outer_prep = prep(self.outer_m)

        minx, miny, maxx, maxy = self.outer_m.bounds
        ww = max(world.x1 - world.x0, 1e-6)
        wh = max(world.y1 - world.y0, 1e-6)
        sx, sy = (maxx - minx) / ww, (maxy - miny) / wh
        if GEO_FIT_MODE != "stretch":
            sx = sy = min(sx, sy)
        self.sx, self.sy = max(sx, 1e-9), max(sy, 1e-9)          # metres per world pixel
        self.wcx, self.wcy = (world.x0 + world.x1) / 2.0, (world.y0 + world.y1) / 2.0
        self.mcx, self.mcy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
        self.gsd = float(0.5 * (self.sx + self.sy))

    def world_to_m(self, wx, wy):
        return self.mcx + self.sx * (wx - self.wcx), self.mcy - self.sy * (wy - self.wcy)

    def m_to_world(self, X, Y):
        return self.wcx + (X - self.mcx) / self.sx, self.wcy - (Y - self.mcy) / self.sy

    def world_to_lonlat(self, wx, wy):
        X, Y = self.world_to_m(wx, wy)
        lon, lat = self._inv.transform(X, Y)
        return float(lon), float(lat)

    def contains_world(self, wx, wy):
        """True if the ground point lies inside (or on) the user's outer boundary."""
        X, Y = self.world_to_m(wx, wy)
        return bool(self._outer_prep.covers(Point(X, Y)))

    def field_polygon_world(self):
        """Outer boundary as an (N, 2) float array of world pixels (for the per-frame field mask)."""
        pts = [self.m_to_world(x, y) for x, y in self.outer_m.exterior.coords]
        return np.array(pts, dtype=np.float64)

    def zone_polygon_m(self, world, row, col):
        corners = world.cell_corners(row, col)
        return Polygon([self.world_to_m(x, y) for x, y in corners])

    def zone_geo(self, world, rows, cols):
        """{zone_id: {lat, lon, has_area, rings}}: each analysis zone clipped to the outer boundary."""
        out = {}
        for r in range(rows):
            for c in range(cols):
                cell_m = self.zone_polygon_m(world, r, c)
                part = _only_polygons(cell_m.intersection(self.outer_m))
                has_area = not part.is_empty
                ref = _inner_point(part) if has_area else cell_m.centroid
                lon, lat = self._inv.transform(ref.x, ref.y)
                out[r * cols + c + 1] = {
                    "lat": round(float(lat), 6),
                    "lon": round(float(lon), 6),
                    "has_area": has_area,
                    "rings": _rings(self.to_ll(part)) if has_area else [],
                }
        return out


def build_spray_layers(outer_ll, infected, zone_geo, margin_m, cell_m):
    """
    Automatic GIS flow (used by the pipeline and by the dashboard when margin / cell size change):
      infected points -> inner boundary (convex hull + margin, clipped to the outer boundary)
                      -> spray grid inside the inner boundary.
    infected : list of dicts with at least lat, lon, severity (+ optional zone_id, pests, ...)
    zone_geo : {zone_id: {..., "rings": [[[lat, lon], ...]]}} used to tell which zone a cell is in
    """
    ctr = outer_ll.centroid
    to_m, to_ll, _, _ = _make_projectors(ctr.y, ctr.x)
    outer_m = to_m(outer_ll)

    zone_polys_m = {}
    for zid, zg in (zone_geo or {}).items():
        parts = [Polygon([(lon, lat) for lat, lon in ring]) for ring in zg.get("rings", []) if len(ring) >= 3]
        if parts:
            zone_polys_m[zid] = to_m(unary_union(parts))

    pts_m = [to_m(Point(float(ip["lon"]), float(ip["lat"]))) for ip in infected]
    sev_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}

    inner_rings, grid, inner_area = [], [], 0.0
    cell_used = max(1.0, float(cell_m))
    if pts_m:
        hull = MultiPoint(pts_m).convex_hull               # Point / LineString / Polygon
        buf = float(margin_m) if margin_m > 0 else (0.0 if hull.geom_type == "Polygon" else 1.0)
        inner_m = hull.buffer(buf) if buf > 0 else hull
        inner_m = _only_polygons(inner_m.intersection(outer_m))
        if not inner_m.is_empty:
            inner_area = inner_m.area
            inner_rings = _rings(to_ll(inner_m))
            ix0, iy0, ix1, iy1 = inner_m.bounds
            while ((ix1 - ix0) / cell_used + 1) * ((iy1 - iy0) / cell_used + 1) > MAX_GRID_CELLS:
                cell_used *= 1.25
            nx = max(1, int(math.ceil((ix1 - ix0) / cell_used)))
            ny = max(1, int(math.ceil((iy1 - iy0) / cell_used)))
            n = 0
            for j in range(ny):
                for i in range(nx):
                    cx0 = ix0 + i * cell_used
                    cy1 = iy1 - j * cell_used
                    gcell = box(cx0, cy1 - cell_used, cx0 + cell_used, cy1)
                    gpart = _only_polygons(gcell.intersection(inner_m))
                    if gpart.is_empty or gpart.area < MIN_GRID_CELL_COVER * gcell.area:
                        continue
                    n += 1
                    c_m = _inner_point(gpart)
                    c_ll = to_ll(c_m)

                    zone_id = None
                    for zid, zp in zone_polys_m.items():
                        if zp.covers(c_m):
                            zone_id = zid
                            break
                    if zone_id is None and zone_polys_m:
                        zone_id = min(zone_polys_m, key=lambda z: zone_polys_m[z].distance(c_m))

                    # severity / dose of a cell = that of the nearest infected point
                    k = min(range(len(pts_m)), key=lambda q: pts_m[q].distance(c_m))
                    sev = infected[k].get("severity", "low")
                    if sev == "none":
                        sev = "low"
                    grid.append({
                        "cell_id": f"C{n:03d}",
                        "zone_id": zone_id,
                        "lat": round(c_ll.y, 6),
                        "lon": round(c_ll.x, 6),
                        "severity": sev,
                        "spray_dose_percent": SPRAY_DOSE[sev],
                        "rings": _rings(to_ll(gpart)),
                    })

    ctr_lat_lon = (ctr.y, ctr.x)
    return {
        "zone_geo": zone_geo,
        "infected": infected,
        "inner_rings": inner_rings,
        "grid": grid,
        "cell_m_requested": float(cell_m),
        "cell_m_used": round(cell_used, 1),
        "inner_area_m2": round(inner_area, 1),
        "field_area_m2": round(outer_m.area, 1),
        "center": ctr_lat_lon,
    }


def estimate_flight_geometry(video_path, skip, width, height, total_src_frames, progress_callback=None):
    """
    Pass 1 (cheap): read the video once and estimate the camera pose of every frame that
    pass 2 will process (same frame_skip, same resize). Returns (matrices, estimator) where
    matrices[i] maps the i-th processed frame's pixels to world coordinates.
    """
    estimator = GlobalMotionEstimator()
    cap = cv2.VideoCapture(video_path)
    homographies = []
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        if frame_idx % skip != 0:
            continue
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        homographies.append(estimator.update(frame))

        if progress_callback is not None and total_src_frames:
            try:
                progress_callback(int(total_src_frames * PASS1_PROGRESS_SHARE * frame_idx / total_src_frames),
                                  total_src_frames)
            except Exception:
                pass
    cap.release()
    return homographies, estimator


def compute_box_iou(boxA, boxB):
    """Compute Intersection over Union (IoU) between two boxes [x1, y1, x2, y2]."""
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    interArea = max(0, xB - xA) * max(0, yB - yA)
    boxAArea = max(1e-5, (boxA[2] - boxA[0]) * (boxA[3] - boxA[1]))
    boxBArea = max(1e-5, (boxB[2] - boxB[0]) * (boxB[3] - boxB[1]))
    denom = float(boxAArea + boxBArea - interArea)
    if denom <= 0:
        return 0.0
    return interArea / denom


def suppress_overlapping_boxes(all_detected_boxes, iou_threshold=0.45):
    """
    Intra-Frame Non-Maximum Suppression (NMS):
    Deduplicates overlapping predictions across ground YOLO, aerial YOLO,
    and SAHI sliced tiles within the SAME frame.
    """
    if not all_detected_boxes:
        return []

    items = []
    for box, name_map, color_box in all_detected_boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        conf_val = float(box.conf[0])
        class_id = int(box.cls[0])
        class_label = name_map.get(class_id, f"class_{class_id}")
        items.append({
            "box": box,
            "name_map": name_map,
            "color_box": color_box,
            "conf": conf_val,
            "bbox": [x1, y1, x2, y2],
            "class_label": class_label,
        })

    items.sort(key=lambda x: x["conf"], reverse=True)
    keep = []

    while items:
        best = items.pop(0)
        keep.append((best["box"], best["name_map"], best["color_box"]))

        filtered_items = []
        for item in items:
            same_class = (item["class_label"] == best["class_label"])
            iou = compute_box_iou(best["bbox"], item["bbox"])
            if iou > iou_threshold and (same_class or iou > 0.65):
                continue
            filtered_items.append(item)
        items = filtered_items

    return keep


class SpatialTemporalTracker:
    """
    Inter-Frame Spatial-Temporal Tracking & Deduplication:
    Tracks pests across consecutive frames as the drone flies over the crop field.
    Prevents double-counting the exact same pest visible in multiple overlapping frames.
    """
    def __init__(self, max_disappeared_frames=10, distance_threshold_px=90.0):
        self.next_track_id = 1
        self.tracks = {}  # track_id -> {"centroid": (cx, cy), "label": str, "zone": (r, c), "disappeared": 0}
        self.max_disappeared = max_disappeared_frames
        self.distance_threshold = distance_threshold_px
        self.unique_zone_pest_counts = {(r, c): {} for r in range(GRID_ROWS) for c in range(GRID_COLS)}
        self.total_unique_pests = {}

    def update(self, current_frame_detections):
        """
        current_frame_detections: list of dicts with {"cx": float, "cy": float, "label": str, "row": int, "col": int}
        Optional "wx"/"wy" = world (ground) coordinates. When present they are used for matching
        instead of the frame pixels, which makes tracking independent of the drone motion.
        """
        for tid in list(self.tracks.keys()):
            self.tracks[tid]["disappeared"] += 1

        annotated_dets = []

        for det in current_frame_detections:
            # Prefer world (ground) coordinates so matching keeps working while the drone moves
            cx, cy = det.get("wx", det["cx"]), det.get("wy", det["cy"])
            label = det["label"]
            r, c = det["row"], det["col"]

            best_tid = None
            best_dist = float("inf")

            for tid, track in self.tracks.items():
                if track["disappeared"] > self.max_disappeared:
                    continue
                if track["label"] == label:
                    tcx, tcy = track["centroid"]
                    dist = math.hypot(cx - tcx, cy - tcy)
                    if dist < self.distance_threshold and dist < best_dist:
                        best_dist = dist
                        best_tid = tid

            is_new = False
            if best_tid is not None:
                self.tracks[best_tid]["centroid"] = (cx, cy)
                self.tracks[best_tid]["disappeared"] = 0
                self.tracks[best_tid]["zone"] = (r, c)
                track_id = best_tid
            else:
                track_id = self.next_track_id
                self.next_track_id += 1
                self.tracks[track_id] = {
                    "centroid": (cx, cy),
                    "label": label,
                    "zone": (r, c),
                    "disappeared": 0,
                }
                is_new = True

                self.total_unique_pests[label] = self.total_unique_pests.get(label, 0) + 1
                self.unique_zone_pest_counts[(r, c)][label] = self.unique_zone_pest_counts[(r, c)].get(label, 0) + 1

            det_copy = dict(det)
            det_copy["track_id"] = track_id
            det_copy["is_new"] = is_new
            annotated_dets.append(det_copy)

        stale_tids = [tid for tid, track in self.tracks.items() if track["disappeared"] > self.max_disappeared]
        for tid in stale_tids:
            del self.tracks[tid]

        return annotated_dets

SEVERITY_THRESHOLDS = {
    "low": 0.10,     # 10% zone coverage/discoloration -> low
    "medium": 0.25,  # 25% -> medium
    # >=45% -> high
}

SPRAY_DOSE = {
    "none": 0,
    "low": 30,      # % of full dose
    "medium": 60,
    "high": 100,
}

COLORS = {
    "none": (200, 200, 200),
    "low": (0, 255, 255),      # yellow
    "medium": (0, 165, 255),   # orange
    "high": (0, 0, 255),       # red
}

# Legacy 14 Ground Class Names Map
LEGACY_CLASS_NAME_MAP = {
    0: "Powdery Mildew",
    1: "Bacterial Leaf Blight",
    2: "Spotted Stink Bug",
    3: "Stalk Rust",
    4: "Locust/Grasshopper",
    5: "Thrips",
    6: "Cotton Bollworm",
    7: "Anthracnose",
    8: "Sheath Blight",
    9: "Snail",
    10: "Aphid",
    11: "Northern Corn Leaf Blight",
    12: "Corn Borer",
    13: "Corn Rust",
}

# IP102 102 Ground Pest/Disease Names
IP102_CLASS_NAMES = [
    'rice leaf roller', 'rice leaf caterpillar', 'paddy stem maggot', 'asiatic rice borer', 'yellow rice borer',
    'rice gall midge', 'Rice Stemfly', 'brown plant hopper', 'white backed plant hopper', 'small brown plant hopper',
    'rice water weevil', 'rice leafhopper', 'grain spreader thrips', 'rice shell pest', 'grub', 'mole cricket', 'wireworm',
    'white margined moth', 'black cutworm', 'large cutworm', 'yellow cutworm', 'red spider', 'corn borer', 'army worm', 'aphids',
    'Potosiabre vitarsis', 'peach borer', 'english grain aphid', 'green bug', 'bird cherry-oataphid', 'wheat blossom midge',
    'penthaleus major', 'longlegged spider mite', 'wheat phloeothrips', 'wheat sawfly', 'cerodonta denticornis', 'beet fly',
    'flea beetle', 'cabbage army worm', 'beet army worm', 'Beet spot flies', 'meadow moth', 'beet weevil', 'sericaorient alismots chulsky',
    'alfalfa weevil', 'flax budworm', 'alfalfa plant bug', 'tarnished plant bug', 'Locustoidea', 'lytta polita', 'legume blister beetle',
    'blister beetle', 'therioaphis maculata Buckton', 'odontothrips loti', 'Thrips', 'alfalfa seed chalcid', 'Pieris canidia',
    'Apolygus lucorum', 'Limacodidae', 'Viteus vitifoliae', 'Colomerus vitis', 'Brevipoalpus lewisi McGregor', 'oides decempunctata',
    'Polyphagotars onemus latus', 'Pseudococcus comstocki Kuwana', 'parathrene regalis', 'Ampelophaga', 'Lycorma delicatula', 'Xylotrechus',
    'Cicadella viridis', 'Miridae', 'Trialeurodes vaporariorum', 'Erythroneura apicalis', 'Papilio xuthus', 'Panonchus citri McGregor',
    'Phyllocoptes oleiverus ashmead', 'Icerya purchasi Maskell', 'Unaspis yanonensis', 'Ceroplastes rubens', 'Chrysomphalus aonidum',
    'Parlatoria zizyphus Lucus', 'Nipaecoccus vastalor', 'Aleurocanthus spiniferus', 'Tetradacus c Bactrocera minax ', 'Dacus dorsalis(Hendel)',
    'Bactrocera tsuneonis', 'Prodenia litura', 'Adristyrannus', 'Phyllocnistis citrella Stainton', 'Toxoptera citricidus', 'Toxoptera aurantii',
    'Aphis citricola Vander Goot', 'Scirtothrips dorsalis Hood', 'Dasineura sp', 'Lawana imitata Melichar', 'Salurnis marginella Guerr',
    'Deporaus marginatus Pascoe', 'Chlumetia transversa', 'Mango flat beak leafhopper', 'Rhytidodera bowrinii white', 'Sternochetus frigidus',
    'Cicadellidae'
]

# Agriculture-Vision Aerial Anomaly Names
AERIAL_CLASS_NAMES = [
    "weed_cluster", "nutrient_deficiency", "drydown", "water",
    "storm_damage", "planter_skip", "double_plant", "endrow", "waterway"
]


def apply_clahe(frame):
    """Contrast enhancement so faint spots/discoloration become more detectable
    before the frame is handed to the detector."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    enhanced = cv2.merge((l, a, b))
    return cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)


class _XYXY:
    """Wraps a plain list so it supports .tolist() like ultralytics tensors do."""
    def __init__(self, vals):
        self.vals = vals

    def tolist(self):
        return self.vals


class PseudoBox:
    """Makes a SAHI prediction look like an ultralytics box, so the rest of the
    script (which expects box.xyxy[0], box.conf[0], box.cls[0]) works unchanged."""
    def __init__(self, x1, y1, x2, y2, conf, cls_id):
        self.xyxy = [_XYXY([x1, y1, x2, y2])]
        self.conf = [conf]
        self.cls = [cls_id]


def resolve_video_path(requested_path):
    """Robust resolution for video path across root and test_videos subfolder."""
    p = Path(requested_path)
    if p.exists():
        return str(p)
    alt1 = Path("test_videos") / p.name
    if alt1.exists():
        return str(alt1)
    alt2 = Path(p.name)
    if alt2.exists():
        return str(alt2)
    return str(requested_path)


def get_severity_from_ratio(ratio, det_cnt=0, mode="combined"):
    """Map a combined severity ratio (0.0 to 1.0) and detection count to severity label."""
    if mode == "ground" and det_cnt == 0 and ratio < 0.10:
        return "none"
    if ratio < 0.10:
        return "none"
    elif ratio < 0.25:
        return "low"
    elif ratio < 0.45:
        return "medium"
    else:
        return "high"


def analyze_zone_color(zone_bgr, mask=None):
    """
    HSV color space analysis for vegetation health cross-check.
    Classifies pixels into Healthy Green Canopy, Chlorosis (Yellowing), and Necrosis (Brown spot/bare soil).
    mask: optional uint8 mask (same height/width as zone_bgr). Only pixels with mask > 0 are analysed
          (used when a ground zone is only partly inside the camera view).
    """
    hsv = cv2.cvtColor(zone_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    total_pixels = max(1, zone_bgr.shape[0] * zone_bgr.shape[1])

    healthy = (h >= 26) & (h <= 85) & (s >= 35) & (v >= 35)
    chlorosis = (h >= 14) & (h <= 25) & (s >= 40) & (v >= 50)
    necrosis = (((h < 14) | (h >= 165))) & (s >= 30) & (v >= 30) & (v <= 220)

    if mask is not None:
        valid = mask > 0
        total_pixels = max(1, int(np.count_nonzero(valid)))
        healthy, chlorosis, necrosis = healthy & valid, chlorosis & valid, necrosis & valid

    healthy_count = int(np.sum(healthy))
    chlorosis_count = int(np.sum(chlorosis))
    necrosis_count = int(np.sum(necrosis))

    unhealthy_count = chlorosis_count + necrosis_count
    canopy_count = healthy_count + unhealthy_count

    # Centre of the unhealthy pixels (crop coordinates) -> real position of the stress spot
    unhealthy_cx = unhealthy_cy = None
    if unhealthy_count > 0:
        m = cv2.moments((chlorosis | necrosis).astype(np.uint8), binaryImage=True)
        if m["m00"] > 0:
            unhealthy_cx, unhealthy_cy = m["m10"] / m["m00"], m["m01"] / m["m00"]

    unhealthy_ratio = float(unhealthy_count / total_pixels)
    chlorosis_ratio = float(chlorosis_count / total_pixels)
    necrosis_ratio = float(necrosis_count / total_pixels)
    canopy_ratio = float(canopy_count / total_pixels)

    return {
        "unhealthy_ratio": unhealthy_ratio,
        "chlorosis_ratio": chlorosis_ratio,
        "necrosis_ratio": necrosis_ratio,
        "canopy_ratio": canopy_ratio,
        "unhealthy_cx": unhealthy_cx,
        "unhealthy_cy": unhealthy_cy,
        "unhealthy_px": unhealthy_count,
    }


def analyze_zone_texture(zone_bgr, mask=None):
    """
    Computes spatial texture roughness anomaly using normalized intensity variance.
    Smooth healthy leaves have consistent texture, while spotty or deformed
    leaves produce elevated intensity variations.
    """
    gray = cv2.cvtColor(zone_bgr, cv2.COLOR_BGR2GRAY)
    values = gray[mask > 0] if mask is not None else gray
    if values.size == 0:
        values = gray
    mean_val = float(np.mean(values))
    std_val = float(np.std(values))
    norm_var = std_val / (mean_val + 1e-5)

    texture_anomaly = max(0.0, min(1.0, (norm_var - 0.55) / 0.30))
    return {
        "norm_var": round(norm_var, 3),
        "texture_anomaly": float(texture_anomaly)
    }


def analyze_zone_wilting(zone_bgr, mask=None):
    """
    Heuristic for wilting / drooping leaves:
    Combines intra-canopy deep shadow ratio (V < 15, S < 20) and edge density.
    LIMITATION NOTE: 2D RGB top-down video lacks 3D depth / thermal IR data.
    Drooping is estimated via shadow proportion and irregular edge density proxies.
    """
    hsv = cv2.cvtColor(zone_bgr, cv2.COLOR_BGR2HSV)
    s_c, v_c = hsv[:, :, 1], hsv[:, :, 2]
    total_pixels = max(1, zone_bgr.shape[0] * zone_bgr.shape[1])

    shadow = (v_c < 15) & (s_c < 20)
    gray = cv2.cvtColor(zone_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 100, 250) > 0
    if mask is not None:
        valid = mask > 0
        total_pixels = max(1, int(np.count_nonzero(valid)))
        shadow, edges = shadow & valid, edges & valid

    shadow_count = int(np.sum(shadow))
    shadow_ratio = float(shadow_count / total_pixels)
    edge_density = float(np.sum(edges) / total_pixels)

    wilt_score = max(0.0, min(1.0, max(0.0, shadow_ratio - 0.005) * 40.0))
    return {
        "shadow_ratio": shadow_ratio,
        "edge_density": edge_density,
        "wilt_score": wilt_score
    }


# Offline, general-agriculture-practice prevention knowledge base.
# Matched by KEYWORD against the detected class name (case-insensitive substring match).
# This is NOT learned by the YOLO model -- it is a static lookup the dashboard
# consults after detection. Classes that match nothing return None, and the
# dashboard must say "not available" rather than inventing advice.
PREVENTION_KB = {
    "aphid": "Encourage natural predators (ladybugs, lacewings); use insecticidal soap or neem oil spray; avoid excess nitrogen fertilizer which attracts aphids.",
    "borer": "Remove and destroy infested stalks/stems after harvest; use pheromone traps to monitor adult moth activity; rotate crops to break the pest's life cycle.",
    "cutworm": "Use collars around young seedlings; till soil before planting to expose larvae; apply targeted biological control (Bt) if infestation is confirmed.",
    "armyworm": "Scout fields regularly during larval stage; encourage natural predators; apply Bt-based biological insecticide on young larvae.",
    "weevil": "Practice crop rotation; remove plant debris where larvae overwinter; use pheromone or sticky traps for early detection.",
    "beetle": "Hand-pick if infestation is light; use row covers on young plants; apply neem-based repellents.",
    "mite": "Increase humidity around plants (mites thrive in dry conditions); introduce predatory mites; avoid excess dust which favors mite populations.",
    "moth": "Use pheromone traps to monitor and disrupt mating; remove weeds that host larvae; apply targeted biological control during larval stage.",
    "thrip": "Use reflective mulches to deter adults; introduce predatory insects (minute pirate bugs); avoid over-fertilizing with nitrogen.",
    "hopper": "Remove weeds and alternate hosts near fields; use yellow sticky traps for monitoring; encourage natural predators like spiders.",
    "midge": "Remove and destroy infested plant parts; rotate crops; monitor with pheromone traps for timely intervention.",
    "blight": "Improve field drainage and air circulation; avoid overhead irrigation; remove and destroy infected plant debris; consider resistant varieties next season.",
    "rust": "Ensure good air circulation between plants; avoid wetting foliage when watering; remove infected leaves promptly; apply approved fungicide if severe.",
    "mildew": "Improve air circulation and reduce humidity around plants; avoid overhead watering; remove and dispose of infected leaves.",
    "mold": "Improve drainage and airflow; avoid overcrowding plants; remove affected fruit/leaves promptly to stop spread.",
    "spot": "Remove and destroy affected leaves; avoid overhead watering; rotate crops to reduce soil-borne buildup.",
    "virus": "Remove and destroy infected plants to prevent spread; control insect vectors (aphids, whiteflies) that transmit the virus; use certified virus-free seed/stock next season.",
    "wilt": "Improve soil drainage; avoid overwatering; rotate with non-host crops; remove and destroy infected plants.",
    "scab": "Prune for better air circulation; remove fallen infected leaves in autumn; consider resistant varieties.",
    "canker": "Prune out and destroy infected branches during dry weather; disinfect pruning tools between cuts; avoid wounding bark.",
}


def get_prevention_advice(class_label):
    """Return prevention advice by keyword match, or None if nothing matches
    (the caller must show 'not available' rather than fabricating advice)."""
    label_lower = class_label.lower()
    for keyword, advice in PREVENTION_KB.items():
        if keyword in label_lower:
            return advice
    return None


@lru_cache(maxsize=1)
def load_models(use_sahi=USE_SAHI):
    """Load ground and aerial models with appropriate fallbacks.

    CACHED: yeh function poore process mein sirf ek baar chalta hai. Streamlit
    har button click pe script dobara chalata hai, lekin lru_cache ki wajah se
    weights sirf pehli dafa disk se load hote hain -- baad mein memory se.
    """
    ground_model = None
    aerial_model = None
    sahi_ground_model = None
    ground_class_map = LEGACY_CLASS_NAME_MAP
    warnings = []

    if Path(DEFAULT_GROUND_MODEL).exists():
        print(f"Loading upgraded ground model: {DEFAULT_GROUND_MODEL}  [device={DEVICE}]")
        ground_model = YOLO(DEFAULT_GROUND_MODEL)
        ground_class_map = ground_model.names

        if use_sahi:
            print("Loading SAHI-wrapped ground model for sliced inference...")
            try:
                sahi_ground_model = AutoDetectionModel.from_pretrained(
                    model_type='ultralytics',
                    model_path=DEFAULT_GROUND_MODEL,
                    confidence_threshold=CONF_THRESHOLD,
                    device=DEVICE
                )
            except Exception as e:
                msg = f"SAHI model load failed ({e}). Falling back to standard full-frame YOLO inference."
                print(msg)
                warnings.append(msg)
                sahi_ground_model = None
        else:
            msg = ("SAHI tiled inference is disabled on CPU for speed (it runs ~12 separate "
                   "inferences per frame). Using standard full-frame YOLO instead. "
                   "Set FORCE_SAHI = True in pest_detection_severity_spray.py to re-enable.")
            print(msg)
            warnings.append(msg)

    elif Path(LEGACY_MODEL).exists():
        print(f"Loading legacy ground model: {LEGACY_MODEL}  [device={DEVICE}]")
        ground_model = YOLO(LEGACY_MODEL)
        ground_class_map = LEGACY_CLASS_NAME_MAP
    else:
        msg = (f"No ground model weights found (checked '{DEFAULT_GROUND_MODEL}' and "
               f"'{LEGACY_MODEL}'). Ground detections will be empty until the path is fixed.")
        print(f"Warning: {msg}")
        warnings.append(msg)

    if Path(DEFAULT_AERIAL_MODEL).exists():
        print(f"Loading aerial crop stress model: {DEFAULT_AERIAL_MODEL}")
        aerial_model = YOLO(DEFAULT_AERIAL_MODEL)
    else:
        msg = (f"No aerial model weights found ('{DEFAULT_AERIAL_MODEL}'). Aerial pattern "
               f"signals will rely on visual HSV/Texture heuristics only.")
        print(f"Warning: {msg}")
        warnings.append(msg)

    return ground_model, aerial_model, ground_class_map, sahi_ground_model, warnings


def process_video(video_path=DEFAULT_VIDEO, output_video=OUTPUT_VIDEO, report_path=REPORT_PATH,
                  mode="combined", frame_skip=None, progress_callback=None,
                  motion_compensation=None, boundary=None,
                  inner_margin_m=DEFAULT_INNER_MARGIN_M, grid_cell_m=DEFAULT_GRID_CELL_M):
    """
    progress_callback: optional fn(done_frames, total_frames) -- Streamlit progress bar
                       ke liye. None ho to kuch nahi hota (CLI ke liye safe).
    frame_skip: None ho to global FRAME_SKIP use hoti hai.
    motion_compensation: True/False to force drone-motion compensation (ground-fixed zones)
                       on/off. None -> use the MOTION_COMPENSATION config flag.
    boundary: the user's OUTER (flight) boundary as [(lat, lon), ...] or [{"lat":..,"lon":..}, ...].
              Detection / analysis runs only inside it and every coordinate is derived from it.
              None -> the video is still analysed, but no coordinates / GIS layers are produced.
    inner_margin_m / grid_cell_m: margin around the infected points' hull and spray-grid cell size.
    """
    skip = int(frame_skip) if frame_skip else FRAME_SKIP
    skip = max(1, skip)

    resolved_path = resolve_video_path(video_path)
    print(f"Starting AgriVision processing in '{mode.upper()}' mode on video: {resolved_path}...")
    print(f"Device: {DEVICE} | frame_skip: {skip} | imgsz: {IMG_SIZE} | SAHI: {USE_SAHI}")

    ground_model, aerial_model, ground_class_map, sahi_ground_model, model_warnings = load_models()

    cap = cv2.VideoCapture(resolved_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"Could not open video file: {resolved_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 20
    src_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_src_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    # --- Downscale for speed ---
    scale = 1.0
    if src_width > PROCESS_MAX_WIDTH:
        scale = PROCESS_MAX_WIDTH / float(src_width)
    width = int(src_width * scale)
    height = int(src_height * scale)
    frame_pixel_area = max(1, width * height)

    temp_raw_video = str(Path(output_video).with_suffix(".raw.mp4"))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(temp_raw_video, fourcc, max(1.0, fps / skip), (width, height))

    # --- Pass 1: estimate the drone's motion and build the GROUND-FIXED zone grid ---
    # (see the "WORLD-FIXED ZONES" section above for the reasoning)
    use_motion = MOTION_COMPENSATION if motion_compensation is None else bool(motion_compensation)
    motion_failures = 0
    homographies = []
    if use_motion:
        print("Pass 1/2: estimating drone motion to build ground-fixed zones...")
        homographies, motion_estimator = estimate_flight_geometry(
            resolved_path, skip, width, height, total_src_frames, progress_callback)
        motion_failures = motion_estimator.failures
    if not homographies:
        homographies = [np.eye(3, dtype=np.float64)]   # static camera / tracking disabled

    world = WorldGrid(homographies, width, height, GRID_ROWS, GRID_COLS)

    # --- GIS: fit the flown ground area onto the user's outer boundary (no demo coordinates) ---
    outer_ll = boundary_polygon(boundary)
    if boundary and outer_ll is None:
        raise ValueError("The field boundary needs at least 3 valid vertices.")
    geo = GeoReference(outer_ll, world) if outer_ll is not None else None
    zone_geo = geo.zone_geo(world, GRID_ROWS, GRID_COLS) if geo is not None else {}
    zone_gps = {}
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            zg = zone_geo.get(r * GRID_COLS + c + 1)
            zone_gps[(r, c)] = (zg["lat"], zg["lon"]) if zg else (None, None)
    field_poly_world = geo.field_polygon_world() if geo is not None else None
    if geo is None:
        print("No outer boundary given: coordinates and GIS layers will NOT be produced.")
    min_visible_px = max(1, int(MIN_CELL_VISIBLE_FRACTION * frame_pixel_area))
    print(f"World extent: {world.x1 - world.x0:.0f} x {world.y1 - world.y0:.0f} px "
          f"(camera frame: {width} x {height} px) | motion-tracking failures: {motion_failures}")

    # Per-zone histories only receive a value in frames where that zone is visible
    zone_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_coverage_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_det_count_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}

    zone_discoloration_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_chlorosis_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_necrosis_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_texture_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_wilting_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_combined_ratio_history = {(r, c): [] for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_class_tally = {(r, c): {} for r in range(GRID_ROWS) for c in range(GRID_COLS)}
    zone_thumbnails = {(r, c): None for r in range(GRID_ROWS) for c in range(GRID_COLS)}

    # Tracks live in WORLD coordinates, so a pest keeps its identity while the drone moves and
    # when it re-enters the view later (overlapping flight lines). Tracks never expire.
    tracker = SpatialTemporalTracker(max_disappeared_frames=10 ** 9,
                                     distance_threshold_px=TRACK_MATCH_DISTANCE_WORLD_PX)

    per_frame_report = []

    # Real positions of the infected points (all in world / ground coordinates)
    track_acc = {}      # track_id -> running mean ground position of one unique pest
    stress_acc = {}     # (row, col) -> weighted sum of the crop-stress pixel centroids

    frame_idx = 0
    processed_frames = 0
    total_valid_boxes = 0
    total_rejected_oversized_boxes = 0
    total_rejected_outside_boundary = 0

    class_tally = {}

    max_box_ratio = MAX_BOX_FRAME_RATIO_AERIAL if mode == "aerial" else MAX_BOX_FRAME_RATIO_GROUND

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1

        if frame_idx % skip != 0:
            continue

        if scale != 1.0:
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)

        processed_frames += 1

        if progress_callback is not None and total_src_frames:
            try:
                p1 = PASS1_PROGRESS_SHARE if use_motion else 0.0
                progress_callback(int(total_src_frames * (p1 + (1.0 - p1) * frame_idx / total_src_frames)),
                                  total_src_frames)
            except Exception:
                pass

        frame = apply_clahe(frame)
        analysis_frame = frame.copy()   # clean copy (no drawings) used for colour/texture analysis

        # Camera pose of this frame: frame pixels -> world (ground) coordinates
        H_cur = homographies[min(processed_frames - 1, len(homographies) - 1)]
        H_inv = np.linalg.inv(H_cur)

        zone_counts = {k: 0 for k in zone_history}
        box_mask = np.zeros((height, width), dtype=np.uint8)

        # Outer boundary projected into this frame: pixels outside it are never analysed
        field_mask = None
        if field_poly_world is not None:
            fp = cv2.perspectiveTransform(field_poly_world.reshape(-1, 1, 2),
                                          np.asarray(H_inv, dtype=np.float64)).reshape(-1, 2)
            fp = np.clip(fp, -1e5, 1e5)
            field_mask = np.zeros((height, width), dtype=np.uint8)
            cv2.fillPoly(field_mask, [np.round(fp).astype(np.int32)], 255)

        ground_boxes = []
        aerial_boxes = []

        if sahi_ground_model is not None and mode in ["ground", "combined"]:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            sliced_result = get_sliced_prediction(
                frame_rgb,
                sahi_ground_model,
                slice_height=IMG_SIZE,
                slice_width=IMG_SIZE,
                overlap_height_ratio=0.2,
                overlap_width_ratio=0.2,
                verbose=0
            )
            for pred in sliced_result.object_prediction_list:
                bbox = pred.bbox
                pseudo_box = PseudoBox(bbox.minx, bbox.miny, bbox.maxx, bbox.maxy,
                                        pred.score.value, pred.category.id)
                ground_boxes.append((pseudo_box, ground_class_map, (255, 100, 0)))
        elif ground_model is not None and mode in ["ground", "combined"]:
            res_g = ground_model.predict(frame, imgsz=IMG_SIZE, conf=CONF_THRESHOLD,
                                         device=DEVICE, verbose=False)[0]
            for b in res_g.boxes:
                ground_boxes.append((b, ground_class_map, (255, 100, 0)))

        if aerial_model is not None and mode in ["aerial", "combined"]:
            res_a = aerial_model.predict(frame, imgsz=IMG_SIZE, conf=CONF_THRESHOLD,
                                         device=DEVICE, verbose=False)[0]
            for b in res_a.boxes:
                aerial_boxes.append((b, aerial_model.names, (0, 200, 255)))

        all_detected_boxes = ground_boxes + aerial_boxes
        # --- Intra-Frame Multi-Model NMS Deduplication ---
        all_detected_boxes = suppress_overlapping_boxes(all_detected_boxes, iou_threshold=0.45)

        current_frame_dets = []

        for box, name_map, color_box in all_detected_boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            bw, bh = x2 - x1, y2 - y1
            box_area = bw * bh
            area_ratio = box_area / frame_pixel_area
            conf_val = float(box.conf[0])

            if area_ratio > max_box_ratio and conf_val < HIGH_CONF_REQUIRED_FOR_LARGE_BOX:
                total_rejected_oversized_boxes += 1
                continue

            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            class_id = int(box.cls[0])
            class_label = name_map.get(class_id, f"class_{class_id}")

            # Position of this detection on the GROUND (world coordinates), and the
            # ground-fixed zone it belongs to. The zone no longer depends on where the
            # camera happens to point.
            wx, wy = world.point_to_world(H_cur, cx, cy)

            # Detection runs ONLY inside the user's outer boundary
            if geo is not None and not geo.contains_world(wx, wy):
                total_rejected_outside_boundary += 1
                continue

            total_valid_boxes += 1
            row, col = world.world_to_cell(wx, wy)

            current_frame_dets.append({
                "cx": cx, "cy": cy, "wx": wx, "wy": wy,
                "label": class_label, "row": row, "col": col,
                "conf": conf_val, "box": box, "name_map": name_map, "color_box": color_box
            })

        # --- Inter-Frame Spatial-Temporal Tracking & Object Deduplication ---
        tracked_dets = tracker.update(current_frame_dets)

        for det in tracked_dets:
            class_label = det["label"]
            conf_val = det["conf"]
            row, col = det["row"], det["col"]
            box, color_box = det["box"], det["color_box"]
            x1, y1, x2, y2 = box.xyxy[0].tolist()

            acc = track_acc.setdefault(det["track_id"], {"label": class_label, "sx": 0.0, "sy": 0.0,
                                                         "n": 0, "conf_max": 0.0})
            acc["sx"] += det["wx"]
            acc["sy"] += det["wy"]
            acc["n"] += 1
            acc["conf_max"] = max(acc["conf_max"], conf_val)

            entry = class_tally.setdefault(class_label, {"count": 0, "conf_sum": 0.0, "conf_n": 0,
                                                         "conf_min": 1.0, "conf_max": 0.0})
            if det["is_new"]:
                entry["count"] += 1     # unique physical pests
            entry["conf_sum"] += conf_val
            entry["conf_n"] += 1        # number of observations (frames) behind conf_sum
            entry["conf_min"] = min(entry["conf_min"], conf_val)
            entry["conf_max"] = max(entry["conf_max"], conf_val)

            zone_counts[(row, col)] += 1
            if det["is_new"]:
                # Count every physical pest ONCE per zone, not once per frame it appears in.
                # (Overlapping frames show the same pest several times.)
                zone_class_tally[(row, col)][class_label] = zone_class_tally[(row, col)].get(class_label, 0) + 1

            # Union of all boxes of this frame; used for the per-zone coverage ratio
            cv2.rectangle(box_mask, (int(x1), int(y1)), (int(x2), int(y2)), 255, -1)

            cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), color_box, 3)
            label_text = f"{class_label} #{det['track_id']} {conf_val:.2f}"
            label_size, _ = cv2.getTextSize(label_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            label_y = max(int(y1) - 10, label_size[1] + 10)
            cv2.rectangle(frame, (int(x1), label_y - label_size[1] - 8),
                         (int(x1) + label_size[0] + 8, label_y + 4), color_box, -1)
            cv2.putText(frame, label_text, (int(x1) + 4, label_y - 4),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        frame_zone_report = {}
        for (row, col) in zone_history:
            # Project this GROUND-FIXED zone into the current frame. Returns None when the
            # zone is (almost) outside the camera view -> no observation for this frame.
            view = world.project_cell(row, col, H_inv, width, height, min_visible_px)
            if view is None:
                continue

            x_start, y_start, x_end, y_end = view["x0"], view["y0"], view["x1"], view["y1"]
            zw, zh = x_end - x_start, y_end - y_start          # size of the visible part
            cell_mask = view["mask"]                            # visible pixels of THIS zone only
            visible_px = view["visible_px"]
            if field_mask is not None:
                # keep only the part of the zone that lies inside the outer boundary
                cell_mask = cv2.bitwise_and(cell_mask, field_mask[y_start:y_end, x_start:x_end])
                visible_px = cv2.countNonZero(cell_mask)
                if visible_px < min_visible_px:
                    continue

            det_cnt = zone_counts[(row, col)]
            zone_crop = analysis_frame[y_start:y_end, x_start:x_end]   # clean pixels (no drawings)
            annotated_crop = frame[y_start:y_end, x_start:x_end]
            if annotated_crop.size > 0 and (zone_thumbnails[(row, col)] is None or (det_cnt > 0 and processed_frames % 5 == 0)):
                try:
                    thumb_small = cv2.resize(annotated_crop, (240, 180), interpolation=cv2.INTER_AREA)
                    _, jpg_buf = cv2.imencode('.jpg', thumb_small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if jpg_buf is not None:
                        b64_str = base64.b64encode(jpg_buf).decode('utf-8')
                        zone_thumbnails[(row, col)] = f"data:image/jpeg;base64,{b64_str}"
                except Exception:
                    pass

            # Share of the VISIBLE part of the zone that is covered by detection boxes
            # (union of boxes, so overlapping boxes are not double counted)
            box_in_zone = cv2.bitwise_and(box_mask[y_start:y_end, x_start:x_end], cell_mask)
            coverage_ratio = cv2.countNonZero(box_in_zone) / float(visible_px)

            color_res = analyze_zone_color(zone_crop, cell_mask)
            tex_res = analyze_zone_texture(zone_crop, cell_mask)
            wilt_res = analyze_zone_wilting(zone_crop, cell_mask)

            unhealthy_ratio = color_res["unhealthy_ratio"]
            chlorosis_ratio = color_res["chlorosis_ratio"]
            necrosis_ratio = color_res["necrosis_ratio"]
            tex_anomaly = tex_res["texture_anomaly"]
            wilt_score = wilt_res["wilt_score"]

            if mode == "ground":
                combined_ratio = max(coverage_ratio, 0.5 * unhealthy_ratio, 0.4 * tex_anomaly)
            elif mode == "aerial":
                combined_ratio = max(coverage_ratio, unhealthy_ratio, 0.3 * tex_anomaly, 0.3 * wilt_score)
            else:  # "combined"
                combined_ratio = max(coverage_ratio, unhealthy_ratio, 0.3 * tex_anomaly, 0.3 * wilt_score)

            severity = get_severity_from_ratio(combined_ratio, det_cnt, mode=mode)

            # Where is the crop stress in this zone? (ground position of the unhealthy pixels)
            if (geo is not None and color_res["unhealthy_cx"] is not None
                    and unhealthy_ratio >= SEVERITY_THRESHOLDS["low"]):
                swx, swy = world.point_to_world(H_cur, x_start + color_res["unhealthy_cx"],
                                                y_start + color_res["unhealthy_cy"])
                w_px = float(color_res["unhealthy_px"])
                sa = stress_acc.setdefault((row, col), {"sx": 0.0, "sy": 0.0, "w": 0.0})
                sa["sx"] += swx * w_px
                sa["sy"] += swy * w_px
                sa["w"] += w_px

            zone_history[(row, col)].append(severity)
            zone_coverage_history[(row, col)].append(coverage_ratio)
            zone_det_count_history[(row, col)].append(det_cnt)

            zone_discoloration_history[(row, col)].append(unhealthy_ratio)
            zone_chlorosis_history[(row, col)].append(chlorosis_ratio)
            zone_necrosis_history[(row, col)].append(necrosis_ratio)
            zone_texture_history[(row, col)].append(tex_anomaly)
            zone_wilting_history[(row, col)].append(wilt_score)
            zone_combined_ratio_history[(row, col)].append(combined_ratio)

            color = COLORS[severity]
            zone_lat, zone_lon = zone_gps[(row, col)]

            # Ground-fixed zone outline: it follows the ground (not the camera) as the drone moves
            cv2.polylines(frame, [view["poly"]], True, (255, 255, 0), 1)
            cv2.polylines(frame, [view["poly"]], True, color, 2)

            # Zone number label: the same ground area always keeps the same number
            zone_label = f"Z{row * GRID_COLS + col + 1}"
            cv2.putText(frame, zone_label, (x_start + 5, y_start + zh - 8),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

            if severity != "none":
                dose = SPRAY_DOSE[severity]
                label = f"{severity.upper()} - Spray {dose}%"
                sub_label = f"Discolor:{unhealthy_ratio*100:.1f}% Tex:{tex_anomaly:.2f}"

                label_size, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
                cv2.rectangle(frame, (x_start + 5, y_start + 5),
                             (x_start + 15 + max(label_size[0], 180), y_start + 45), color, -1)
                cv2.putText(frame, label, (x_start + 10, y_start + 23),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                cv2.putText(frame, sub_label, (x_start + 10, y_start + 40),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

                # GPS shown in all modes (only when a real boundary / georeference exists)
                if zone_lat is not None and zone_lon is not None:
                    gps_label = f"GPS: {zone_lat}, {zone_lon}"
                    gps_size, _ = cv2.getTextSize(gps_label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
                    gps_y = y_start + zh - 30
                    cv2.rectangle(frame, (x_start + 5, gps_y - 18),
                                 (x_start + 15 + gps_size[0], gps_y + 6), (0, 0, 0), -1)
                    cv2.putText(frame, gps_label, (x_start + 10, gps_y),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

                # --- Simulated spray action (visual demo, no hardware) ---
                random.seed(frame_idx * 100 + row * 3 + col)
                num_droplets = int(dose / 10)
                for _ in range(num_droplets):
                    dx = random.randint(x_start + 10, max(x_start + 11, x_start + zw - 10))
                    dy = random.randint(y_start + 55, max(y_start + 56, y_start + zh - 10))
                    cv2.circle(frame, (dx, dy), 4, (255, 200, 0), -1)
                    cv2.circle(frame, (dx, dy), 6, (255, 200, 0), 1)

                cv2.putText(frame, "SPRAYING...",
                           (x_start + 10, y_start + zh - 15),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 200, 0), 2)

            frame_zone_report[f"zone_{row}_{col}"] = {
                "severity": severity,
                "spray_dose_percent": SPRAY_DOSE[severity],
                "combined_ratio": round(combined_ratio, 4),
                "discoloration_ratio": round(unhealthy_ratio, 4),
                "texture_anomaly": round(tex_anomaly, 4),
                "wilt_score": round(wilt_score, 4),
                "yolo_coverage_ratio": round(coverage_ratio, 4),
                "latitude": zone_lat,
                "longitude": zone_lon,
            }

        out.write(frame)

        per_frame_report.append({
            "frame": processed_frames,
            "frame_idx": frame_idx,
            "timestamp_sec": round(frame_idx / fps, 2),
            "zones": frame_zone_report,
        })

    cap.release()
    out.release()

    # --- Re-encode to browser-compatible H.264 (libx264, yuv420p) ---
    if FFMPEG_EXE and os.path.exists(temp_raw_video):
        try:
            print(f"Re-encoding annotated video to H.264 via FFmpeg ({output_video})...")
            cmd = [
                FFMPEG_EXE, "-y", "-i", temp_raw_video,
                "-c:v", "libx264", "-pix_fmt", "yuv420p", output_video
            ]
            ret = subprocess.run(cmd, capture_output=True, text=True)
            if ret.returncode == 0 and os.path.exists(output_video):
                os.remove(temp_raw_video)
                print("H.264 video conversion successful!")
            else:
                print(f"FFmpeg conversion warning (code {ret.returncode}): {ret.stderr}. Using raw video.")
                os.replace(temp_raw_video, output_video)
        except Exception as e:
            print(f"FFmpeg conversion exception: {e}. Using raw video.")
            if os.path.exists(temp_raw_video):
                os.replace(temp_raw_video, output_video)
    elif os.path.exists(temp_raw_video):
        os.replace(temp_raw_video, output_video)

    if progress_callback is not None and total_src_frames:
        try:
            progress_callback(total_src_frames, total_src_frames)
        except Exception:
            pass

    severity_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    report = {}

    for (row, col), history in zone_history.items():
        if not history:
            continue

        peak_severity = max(history, key=lambda s: severity_rank[s])
        covs = np.array(zone_coverage_history[(row, col)])
        dets = np.array(zone_det_count_history[(row, col)])
        comb_ratios = np.array(zone_combined_ratio_history[(row, col)])

        discolors = np.array(zone_discoloration_history[(row, col)])
        chloroses = np.array(zone_chlorosis_history[(row, col)])
        necroses = np.array(zone_necrosis_history[(row, col)])
        textures = np.array(zone_texture_history[(row, col)])
        wiltings = np.array(zone_wilting_history[(row, col)])

        p95_coverage = float(np.percentile(covs, 95))
        p95_combined = float(np.percentile(comb_ratios, 95))
        p95_discoloration = float(np.percentile(discolors, 95))
        p95_chlorosis = float(np.percentile(chloroses, 95))
        p95_necrosis = float(np.percentile(necroses, 95))
        p95_texture = float(np.percentile(textures, 95))
        p95_wilting = float(np.percentile(wiltings, 95))

        if mode == "ground":
            observed_frames = len(history)   # frames in which this ground zone was actually visible
            sustained_presence = int(np.sum(dets > 0)) >= max(1, int(observed_frames * 0.01))
            sustained_severity = get_severity_from_ratio(p95_combined if sustained_presence else 0.0, det_cnt=1 if sustained_presence else 0, mode=mode)
        else:
            sustained_severity = get_severity_from_ratio(p95_combined, mode=mode)

        recommended_dose = SPRAY_DOSE[sustained_severity]

        zone_lat, zone_lon = zone_gps[(row, col)]

        # Dominant pest/disease class detected in this zone (if any)
        zone_classes = zone_class_tally[(row, col)]
        dominant_class = None
        dominant_class_count = 0
        if zone_classes:
            candidate = max(zone_classes, key=zone_classes.get)
            if candidate and str(candidate).strip().lower() not in ["none", "null", ""]:
                dominant_class = candidate
                dominant_class_count = zone_classes[dominant_class]
        prevention_advice = get_prevention_advice(dominant_class) if dominant_class else None
        total_zone_detections = sum(zone_classes.values()) if zone_classes else 0

        report[f"zone_{row}_{col}"] = {
            "latitude": zone_lat,
            "longitude": zone_lon,
            "observed_frames": len(history),
            "thumbnail_b64": zone_thumbnails.get((row, col)),
            "recommended_spray_dose_percent": recommended_dose,
            "sprayed": recommended_dose > 0,
            "sustained_severity": sustained_severity,
            "peak_instantaneous_severity": peak_severity,
            "p95_combined_severity_ratio": round(p95_combined, 4),
            "p95_discoloration_ratio": round(p95_discoloration, 4),
            "p95_chlorosis_ratio": round(p95_chlorosis, 4),
            "p95_necrosis_ratio": round(p95_necrosis, 4),
            "p95_texture_anomaly": round(p95_texture, 4),
            "p95_wilting_score": round(p95_wilting, 4),
            "p95_yolo_coverage_ratio": round(p95_coverage, 4),
            "total_yolo_detection_frames": int(np.sum(dets > 0)),
            "total_yolo_detection_count": total_zone_detections,
            "dominant_pest_class": dominant_class,
            "dominant_pest_detection_count": dominant_class_count,
            "zone_class_breakdown": zone_classes,
            "prevention_advice": prevention_advice,
        }

    # --- Infected points -> inner boundary -> spray grid (all automatic, all GIS) ---
    infected_points = []
    gis_result = None
    if geo is not None:
        def _zone_sev(rc):
            return report.get(f"zone_{rc[0]}_{rc[1]}", {}).get("sustained_severity", "none")

        zones_with_detection = set()
        for tid, acc in sorted(track_acc.items()):
            wx, wy = acc["sx"] / acc["n"], acc["sy"] / acc["n"]
            row, col = world.world_to_cell(wx, wy)
            zones_with_detection.add((row, col))
            sev = _zone_sev((row, col))
            if severity_rank[sev] < severity_rank["low"]:
                sev = "low"                     # a confirmed detection is never "healthy"
            lon, lat = geo.world_to_lonlat(wx, wy)
            infected_points.append({
                "point_id": len(infected_points) + 1, "source": "detection",
                "zone_id": row * GRID_COLS + col + 1, "severity": sev,
                "pests": acc["label"], "conf": round(acc["conf_max"], 3),
                "observations": acc["n"],
                "lat": round(lat, 6), "lon": round(lon, 6),
            })

        for (row, col), sa in sorted(stress_acc.items()):
            sev = _zone_sev((row, col))
            if sev == "none" or (row, col) in zones_with_detection or sa["w"] <= 0:
                continue
            wx, wy = sa["sx"] / sa["w"], sa["sy"] / sa["w"]
            if not geo.contains_world(wx, wy):
                continue
            lon, lat = geo.world_to_lonlat(wx, wy)
            infected_points.append({
                "point_id": len(infected_points) + 1, "source": "crop_stress",
                "zone_id": row * GRID_COLS + col + 1, "severity": sev,
                "pests": "Crop stress (discoloration)", "conf": None,
                "observations": len(zone_history[(row, col)]),
                "lat": round(lat, 6), "lon": round(lon, 6),
            })

        gis_result = build_spray_layers(outer_ll, infected_points, zone_geo,
                                        float(inner_margin_m), float(grid_cell_m))
        print(f"GIS: {len(infected_points)} infected points | inner boundary "
              f"{gis_result['inner_area_m2']:.0f} m2 | spray grid cells: {len(gis_result['grid'])}")

    # Non-zone metadata block (dashboards that only read "zone_r_c" keys can ignore it)
    report["_meta"] = {
        "ground_fixed_zones": bool(use_motion),
        "frames_analyzed": processed_frames,
        "source_frames": total_src_frames,
        "output_fps": round(max(1.0, fps / skip), 2),
        "motion_tracking_failures": motion_failures,
        "world_extent_px": [round(world.x1 - world.x0, 1), round(world.y1 - world.y0, 1)],
        "gsd_m_per_px": round(geo.gsd, 5) if geo is not None else None,
        "boundary_defined": geo is not None,
        "detections_rejected_outside_boundary": total_rejected_outside_boundary,
    }
    report["_gis"] = gis_result

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    per_frame_path = str(Path(report_path).with_name(
        Path(report_path).stem + "_per_frame.json"))
    with open(per_frame_path, "w") as f:
        json.dump(per_frame_report, f, indent=2)

    class_tally_path = str(Path(report_path).with_name("class_tally.json"))
    with open(class_tally_path, "w") as f:
        json.dump(class_tally, f, indent=2)

    print(f"\n================ PIPELINE EXECUTION SUMMARY ================")
    print(f"Pipeline Mode: {mode.upper()}  |  Device: {DEVICE}  |  frame_skip: {skip}")
    print(f"Processed frames: {processed_frames} (of {total_src_frames} source frames)")
    print(f"Valid YOLO detections: {total_valid_boxes}")
    print(f"Rejected oversized boxes: {total_rejected_oversized_boxes}")
    print(f"Rejected detections outside the field boundary: {total_rejected_outside_boundary}")
    print(f"Annotated video saved to: {output_video}")
    print(f"Spray report saved to: {report_path}")
    print(f"Per-frame severity report saved to: {per_frame_path}\n")
    print("--- Final Zone Spray Recommendations ---")
    print(json.dumps(report, indent=2))

    print("\n--- Detection Tally by Class ---")
    if not class_tally:
        print("  No valid YOLO detections above CONF_THRESHOLD.")
    else:
        for label, stats in sorted(class_tally.items(), key=lambda kv: -kv[1]["count"]):
            avg_conf = stats["conf_sum"] / max(1, stats.get("conf_n", stats["count"]))
            print(f"  {label}: {stats['count']} detections | "
                  f"conf min={stats['conf_min']:.2f} avg={avg_conf:.2f} max={stats['conf_max']:.2f}")

    return report, class_tally, output_video, model_warnings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgriVision Crop Pest/Disease & Precision Spray Pipeline")
    parser.add_argument("--video", type=str, default=DEFAULT_VIDEO, help="Path to input video file")
    parser.add_argument("--output", type=str, default=OUTPUT_VIDEO, help="Path to output annotated video file")
    parser.add_argument("--report", type=str, default=REPORT_PATH, help="Path to output JSON spray report")
    parser.add_argument("--mode", type=str, choices=["combined", "aerial", "ground"], default="combined",
                        help="Pipeline mode: 'combined' (Ground+Aerial+HSV), 'aerial' (Aerial+HSV+Tex+Wilt), 'ground' (Ground YOLO)")
    parser.add_argument("--frame-skip", type=int, default=None,
                        help="Process every Nth frame (higher = faster). Default: 1 on GPU, 5 on CPU.")

    parser.add_argument("--boundary", type=str, default=None,
                        help="Outer flight boundary: a GeoJSON/JSON file, or 'lat,lon;lat,lon;lat,lon;...' (>= 3 vertices)")
    parser.add_argument("--inner-margin", type=float, default=DEFAULT_INNER_MARGIN_M,
                        help="Metres added around the infected points' hull to form the inner boundary")
    parser.add_argument("--grid-cell", type=float, default=DEFAULT_GRID_CELL_M,
                        help="Spray-grid cell size in metres")

    args = parser.parse_args()

    boundary_arg = None
    if args.boundary:
        if os.path.isfile(args.boundary):
            with open(args.boundary, "r", encoding="utf-8") as bf:
                data = json.load(bf)
            if isinstance(data, dict) and "coordinates" in data:            # GeoJSON Polygon
                boundary_arg = [(pt[1], pt[0]) for pt in data["coordinates"][0]]
            elif isinstance(data, dict) and "features" in data:             # GeoJSON FeatureCollection
                boundary_arg = [(pt[1], pt[0]) for pt in data["features"][0]["geometry"]["coordinates"][0]]
            else:                                                           # [{"lat":..,"lon":..}, ...]
                boundary_arg = data
        else:
            boundary_arg = [tuple(float(v) for v in pair.split(",")) for pair in args.boundary.split(";") if pair.strip()]

    process_video(video_path=args.video, output_video=args.output, report_path=args.report,
                  mode=args.mode, frame_skip=args.frame_skip, boundary=boundary_arg,
                  inner_margin_m=args.inner_margin, grid_cell_m=args.grid_cell)