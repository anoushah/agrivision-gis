"""
mission_sim.py - Simulated drone survey + spray mission for AgriVision GIS.
Uses Shapely and PyProj via gis_utils for geometry computations.
"""
import json
import math
import streamlit.components.v1 as components
import shapely
from shapely.geometry import Point, Polygon, MultiPoint, box
from shapely.ops import transform
import pyproj

import gis_utils

SEV_COLORS = {
    "low": "#F59E0B", "moderate": "#F97316", "medium": "#F97316",
    "high": "#EF4444", "severe": "#B91C1C", "critical": "#B91C1C"
}
DEFAULT_SEV_COLOR = "#EF4444"


def zone_positions(boundary, rows=3, cols=3):
    """Return {(row, col): (lat, lon)} for grid zones within the outer boundary polygon."""
    poly = gis_utils.coords_to_polygon(boundary)
    if poly is None or poly.is_empty:
        return {}
    
    grid_cells = gis_utils.generate_inner_grid(poly, poly, rows=rows, cols=cols)
    out = {}
    for cell in grid_cells:
        out[(cell["row"], cell["col"])] = (cell["latitude"], cell["longitude"])
    return out


def zone_grid_polygons(boundary, rows=3, cols=3):
    """Return zone and sub-quadrant polygon coordinates for grid visualization."""
    poly = gis_utils.coords_to_polygon(boundary)
    if poly is None or poly.is_empty:
        return {}
    
    grid_cells = gis_utils.generate_inner_grid(poly, poly, rows=rows, cols=cols)
    out = {}
    for cell in grid_cells:
        out[(cell["row"], cell["col"])] = {
            "zone": cell["zone_coords"],
            "subs": cell["subs"]
        }
    return out


def build_mission(boundary, zones, rows=3, cols=3, line_spacing_m=None,
                  inner_margin_m=3.0, step_m=2.0):
    """
    Build survey path, infected inner boundary, and spray trajectory using GIS libraries.
    """
    poly = gis_utils.coords_to_polygon(boundary)
    if poly is None or poly.is_empty:
        return {"error": "Draw an outer boundary (polygon) on the Field Map first."}

    centroid = poly.centroid
    c_lat, c_lon = centroid.y, centroid.x
    to_metric, to_wgs84 = gis_utils.get_metric_transformers(c_lat, c_lon)
    metric_poly = transform(to_metric, poly)
    minx, miny, maxx, maxy = metric_poly.bounds
    w, h = maxx - minx, maxy - miny

    if w < 5 or h < 5:
        return {"error": "The outer boundary is too small (under 5 m). Draw a larger area."}

    # Survey flight path generation (lawnmower pattern)
    if not line_spacing_m:
        line_spacing_m = max(4.0, round(h / 6.0, 1))

    survey_pts_metric = []
    curr_y = miny + line_spacing_m / 2.0
    left_to_right = True

    while curr_y < maxy:
        line_seg = transform(to_metric, LineString([(minx - 10, curr_y), (maxx + 10, curr_y)])) if 'LineString' in globals() else shapely.geometry.LineString([(minx - 10, curr_y), (maxx + 10, curr_y)])
        inter = line_seg.intersection(metric_poly)
        if not inter.is_empty:
            if inter.geom_type == 'LineString':
                coords = list(inter.coords)
                if len(coords) >= 2:
                    p_start, p_end = coords[0], coords[-1]
                    if not left_to_right:
                        p_start, p_end = p_end, p_start
                    survey_pts_metric.extend([p_start, p_end])
                    left_to_right = not left_to_right
            elif inter.geom_type == 'MultiLineString':
                lines = list(inter.geoms)
                if not left_to_right:
                    lines.reverse()
                for l in lines:
                    coords = list(l.coords)
                    if len(coords) >= 2:
                        p_start, p_end = coords[0], coords[-1]
                        if not left_to_right:
                            p_start, p_end = p_end, p_start
                        survey_pts_metric.extend([p_start, p_end])
                left_to_right = not left_to_right
        curr_y += line_spacing_m

    # Densify survey path
    dense_survey_metric = []
    for i in range(len(survey_pts_metric) - 1):
        p1, p2 = survey_pts_metric[i], survey_pts_metric[i+1]
        dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        steps = max(1, int(dist / step_m))
        for k in range(steps):
            dense_survey_metric.append((p1[0] + (p2[0] - p1[0]) * k / steps, p1[1] + (p2[1] - p1[1]) * k / steps))
    if survey_pts_metric:
        dense_survey_metric.append(survey_pts_metric[-1])

    survey_path_wgs84 = []
    for mx, my in dense_survey_metric:
        pt_wgs = transform(to_wgs84, Point(mx, my))
        survey_path_wgs84.append([round(pt_wgs.y, 6), round(pt_wgs.x, 6)])

    # Affected points
    z_map = {}
    if isinstance(zones, list):
        for z in zones:
            if isinstance(z, dict) and "row" in z and "col" in z:
                z_map[(z["row"], z["col"])] = z

    grid_cells = gis_utils.generate_inner_grid(poly, poly, rows=rows, cols=cols)
    affected_points = []

    for cell in grid_cells:
        r, c = cell["row"], cell["col"]
        zinfo = z_map.get((r, c))
        if zinfo and (zinfo.get("spray_needed") or zinfo.get("severity", "none") != "none"):
            sev = zinfo.get("severity", "medium")
            affected_points.append({
                "zone_id": cell["zone_id"],
                "row": r,
                "col": c,
                "severity": sev,
                "lat": cell["latitude"],
                "lon": cell["longitude"],
                "color": SEV_COLORS.get(sev, DEFAULT_SEV_COLOR)
            })

    # Sort affected points by survey detection index
    for p in affected_points:
        p_metric = transform(to_metric, Point(p["lon"], p["lat"]))
        min_idx = 0
        min_d = 1e9
        for idx, sm in enumerate(dense_survey_metric):
            d = math.hypot(p_metric.x - sm[0], p_metric.y - sm[1])
            if d < min_d:
                min_d = d
                min_idx = idx
        p["detect_idx"] = min_idx

    affected_points.sort(key=lambda item: item["detect_idx"])
    for rank, p in enumerate(affected_points, 1):
        p["rank"] = rank

    # Inner boundary computation using GIS
    inner_poly_coords = []
    if affected_points:
        pts_tuples = [(p["lat"], p["lon"]) for p in affected_points]
        inner_wgs = gis_utils.build_auto_inner_boundary(pts_tuples, poly, buffer_m=inner_margin_m)
        if inner_wgs and not inner_wgs.is_empty:
            inner_poly_coords = [[round(pt[1], 6), round(pt[0], 6)] for pt in inner_wgs.exterior.coords]

    # Spray path planning
    spray_route_wgs84 = []
    arrive_indices = []
    curr_pos = dense_survey_metric[0] if dense_survey_metric else (minx, miny)

    for p in affected_points:
        p_metric = transform(to_metric, Point(p["lon"], p["lat"]))
        target_pos = (p_metric.x, p_metric.y)
        dist = math.hypot(target_pos[0] - curr_pos[0], target_pos[1] - curr_pos[1])
        steps = max(1, int(dist / step_m))
        for k in range(steps):
            leg_pt = (curr_pos[0] + (target_pos[0] - curr_pos[0]) * k / steps, curr_pos[1] + (target_pos[1] - curr_pos[1]) * k / steps)
            leg_wgs = transform(to_wgs84, Point(leg_pt[0], leg_pt[1]))
            spray_route_wgs84.append([round(leg_wgs.y, 6), round(leg_wgs.x, 6)])
        arrive_indices.append(len(spray_route_wgs84) - 1)
        curr_pos = target_pos

    outer_coords = [[round(pt[1], 6), round(pt[0], 6)] for pt in poly.exterior.coords]
    start_pt = survey_path_wgs84[0] if survey_path_wgs84 else [round(c_lat, 6), round(c_lon, 6)]

    total_survey_m = 0.0
    for i in range(len(dense_survey_metric) - 1):
        p1, p2 = dense_survey_metric[i], dense_survey_metric[i+1]
        total_survey_m += math.hypot(p2[0] - p1[0], p2[1] - p1[1])

    return {
        "outer": outer_coords,
        "inner": inner_poly_coords,
        "survey": survey_path_wgs84,
        "spray_route": spray_route_wgs84,
        "arrive_idx": arrive_indices,
        "points": affected_points,
        "start": start_pt,
        "survey_length_m": round(total_survey_m, 1),
        "line_spacing_m": round(line_spacing_m, 1),
        "skipped_zones": [],
        "route_leaves_outer": False
    }