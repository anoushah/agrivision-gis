"""
GIS Utilities for AgriVision using Shapely, GeoPandas, and PyProj.
Provides geometry, CRS transformations, Outer Boundary handling,
Automatic Infected Points detection, Automatic Inner Boundary calculation,
and Automatic Grid Generation inside the infected inner boundary.
"""
import math
from typing import List, Dict, Tuple, Optional, Any
import numpy as np
import shapely
from shapely.geometry import Point, Polygon, MultiPoint, MultiPolygon, box
from shapely.ops import transform
import pyproj

# Standard WGS84 Geographic CRS
CRS_WGS84 = pyproj.CRS("EPSG:4326")


def get_utm_crs(lat: float, lon: float) -> pyproj.CRS:
    """Determine the appropriate UTM metric CRS for given lat/lon coordinates."""
    utm_zone = int((lon + 180) / 6) + 1
    hemisphere = "north" if lat >= 0 else "south"
    epsg_code = 32600 + utm_zone if hemisphere == "north" else 32700 + utm_zone
    return pyproj.CRS(f"EPSG:{epsg_code}")


def get_metric_transformers(lat: float, lon: float):
    """Return forward (WGS84 -> Metric UTM) and backward (Metric UTM -> WGS84) pyproj transformers."""
    metric_crs = get_utm_crs(lat, lon)
    to_metric = pyproj.Transformer.from_crs(CRS_WGS84, metric_crs, always_xy=True).transform
    to_wgs84 = pyproj.Transformer.from_crs(metric_crs, CRS_WGS84, always_xy=True).transform
    return to_metric, to_wgs84


def coords_to_polygon(lat_lon_list: List[Any]) -> Optional[Polygon]:
    """Convert list of {"lat": y, "lon": x} dicts or [(lat, lon)] tuples into a shapely Polygon in WGS84."""
    if not lat_lon_list or len(lat_lon_list) < 3:
        return None
    pts = []
    for item in lat_lon_list:
        if isinstance(item, dict):
            if "lat" in item and "lon" in item:
                pts.append((float(item["lon"]), float(item["lat"])))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pts.append((float(item[1]), float(item[0])))
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (isinstance(poly, Polygon) and not poly.is_empty) else None


def polygon_to_coords(poly: Polygon) -> List[Dict[str, Any]]:
    """Convert a shapely Polygon in WGS84 to a list of {"lat": y, "lon": x, "name": label} dicts."""
    if poly is None or poly.is_empty or not isinstance(poly, Polygon):
        return []
    coords = list(poly.exterior.coords)
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return [{"lat": round(y, 6), "lon": round(x, 6), "name": f"Vertex {i+1}"} for i, (x, y) in enumerate(coords)]


def get_polygon_metrics(poly: Polygon) -> Tuple[float, float]:
    """Return area in hectares and perimeter in meters for a WGS84 Shapely Polygon."""
    if poly is None or poly.is_empty or not isinstance(poly, Polygon):
        return 0.0, 0.0
    centroid = poly.centroid
    to_metric, _ = get_metric_transformers(centroid.y, centroid.x)
    metric_poly = transform(to_metric, poly)
    area_ha = metric_poly.area / 10000.0
    perimeter_m = metric_poly.length
    return round(area_ha, 4), round(perimeter_m, 2)


def point_inside_polygon(lat: float, lon: float, poly: Polygon) -> bool:
    """Check if (lat, lon) lies inside or on the boundary of a Shapely Polygon."""
    if poly is None or poly.is_empty:
        return False
    pt = Point(lon, lat)
    return poly.contains(pt) or poly.intersects(pt)


def calculate_gis_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in meters between two (lat, lon) coordinates using GIS projection."""
    to_metric, _ = get_metric_transformers((lat1 + lat2) / 2.0, (lon1 + lon2) / 2.0)
    p1 = transform(to_metric, Point(lon1, lat1))
    p2 = transform(to_metric, Point(lon2, lat2))
    return float(round(p1.distance(p2), 2))


def check_geofence_gis(lat: float, lon: float, center_lat: float, center_lon: float, radius_m: float) -> Tuple[bool, float]:
    """Check if (lat, lon) is within radius_m meters of center coordinate using GIS projection."""
    dist_m = calculate_gis_distance(lat, lon, center_lat, center_lon)
    return (dist_m <= radius_m), dist_m


def fit_geofence_to_boundary_gis(poly: Polygon, safety_margin_m: float = 20.0) -> Tuple[float, float, float]:
    """Return (center_lat, center_lon, radius_m) for geofence surrounding a polygon."""
    if poly is None or poly.is_empty:
        return 0.0, 0.0, 50.0
    centroid = poly.centroid
    c_lat, c_lon = centroid.y, centroid.x
    to_metric, _ = get_metric_transformers(c_lat, c_lon)
    metric_poly = transform(to_metric, poly)
    metric_center = transform(to_metric, Point(c_lon, c_lat))
    
    coords = list(metric_poly.exterior.coords)
    farthest_m = max(metric_center.distance(Point(x, y)) for x, y in coords)
    radius_m = float(min(5000.0, max(50.0, round(farthest_m + safety_margin_m, 1))))
    return round(c_lat, 6), round(c_lon, 6), radius_m


def build_auto_inner_boundary(infected_points: List[Tuple[float, float]], outer_poly: Polygon, buffer_m: float = 3.0) -> Optional[Polygon]:
    """
    Automated Inner Boundary Generation:
    Takes automatically detected infected (lat, lon) points and an outer boundary polygon.
    Computes a buffered convex hull in metric CRS, and clips it against the outer boundary.
    """
    if not infected_points or outer_poly is None or outer_poly.is_empty:
        return None
    
    centroid = outer_poly.centroid
    to_metric, to_wgs84 = get_metric_transformers(centroid.y, centroid.x)
    
    metric_outer = transform(to_metric, outer_poly)
    metric_pts = [transform(to_metric, Point(lon, lat)) for lat, lon in infected_points]
    
    if len(metric_pts) == 1:
        metric_hull = metric_pts[0].buffer(buffer_m + 5.0)
    else:
        mp = MultiPoint(metric_pts)
        hull = mp.convex_hull
        if hull.geom_type == 'Point':
            metric_hull = hull.buffer(buffer_m + 5.0)
        elif hull.geom_type == 'LineString':
            metric_hull = hull.buffer(buffer_m + 2.0)
        else:
            metric_hull = hull.buffer(buffer_m)
            
    # Clip to outer boundary so inner boundary never exceeds outer boundary
    metric_inner = metric_hull.intersection(metric_outer)
    if metric_inner.is_empty:
        return None
    if isinstance(metric_inner, MultiPolygon):
        metric_inner = max(metric_inner.geoms, key=lambda g: g.area)
        
    wgs84_inner = transform(to_wgs84, metric_inner)
    return wgs84_inner if (isinstance(wgs84_inner, Polygon) and not wgs84_inner.is_empty) else None


def generate_inner_grid(inner_poly: Polygon, outer_poly: Polygon, rows: int = 3, cols: int = 3) -> List[Dict[str, Any]]:
    """
    Automated Grid Generation:
    Generates grid cells inside the automatically detected inner boundary polygon.
    """
    if inner_poly is None or inner_poly.is_empty or outer_poly is None or outer_poly.is_empty:
        return []
    
    centroid = outer_poly.centroid
    to_metric, to_wgs84 = get_metric_transformers(centroid.y, centroid.x)
    metric_inner = transform(to_metric, inner_poly)
    metric_outer = transform(to_metric, outer_poly)
    
    minx, miny, maxx, maxy = metric_outer.bounds
    width_m = maxx - minx
    height_m = maxy - miny
    
    step_x = max(2.0, width_m / cols)
    step_y = max(2.0, height_m / rows)
    
    grid_cells = []
    cell_id = 1
    
    for r in range(rows):
        y1 = maxy - r * step_y
        y0 = y1 - step_y
        for c in range(cols):
            x0 = minx + c * step_x
            x1 = x0 + step_x
            metric_cell_box = box(x0, y0, x1, y1)
            
            # Intersection with inner boundary
            inter_inner = metric_cell_box.intersection(metric_inner)
            is_infected_cell = not inter_inner.is_empty and inter_inner.area > 0.5
            
            # Intersection with outer boundary for geometry bounds
            inter_cell = metric_cell_box.intersection(metric_outer)
            if inter_cell.is_empty:
                continue
                
            cell_wgs84 = transform(to_wgs84, inter_cell)
            cell_centroid = transform(to_wgs84, inter_cell.centroid)
            
            # Generate sub-quadrants
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            quads_metric = {
                "NW": box(x0, cy, cx, y1),
                "NE": box(cx, cy, x1, y1),
                "SW": box(x0, y0, cx, cy),
                "SE": box(cx, y0, x1, cy)
            }
            quads_wgs84 = {}
            for qname, qbox in quads_metric.items():
                qinter = qbox.intersection(metric_outer)
                if not qinter.is_empty:
                    q_wgs = transform(to_wgs84, qinter)
                    if isinstance(q_wgs, Polygon):
                        coords = list(q_wgs.exterior.coords)
                    else:
                        coords = []
                    quads_wgs84[qname] = [[round(pt[1], 6), round(pt[0], 6)] for pt in coords]
                else:
                    quads_wgs84[qname] = []

            zone_coords = []
            if isinstance(cell_wgs84, Polygon):
                zone_coords = [[round(pt[1], 6), round(pt[0], 6)] for pt in cell_wgs84.exterior.coords]

            grid_cells.append({
                "cell_id": cell_id,
                "zone_id": cell_id,
                "row": r,
                "col": c,
                "latitude": round(cell_centroid.y, 6),
                "longitude": round(cell_centroid.x, 6),
                "is_infected": is_infected_cell,
                "polygon_wgs84": cell_wgs84,
                "zone_coords": zone_coords,
                "subs": quads_wgs84
            })
            cell_id += 1
            
    return grid_cells
