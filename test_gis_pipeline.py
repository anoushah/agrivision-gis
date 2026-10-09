import numpy as np
import pytest
from pyproj import Geod
from shapely.geometry import Polygon, box
from gis_pipeline import *


def make(lon, lat, side=200):
    p = Proj(lon, lat)
    x0, y0 = p.to_m.transform(lon, lat)
    return p, x0, y0, side


def dets_at(p, xy_list, frames=3):
    out, tid = [], 0
    for x, y in xy_list:
        lon, lat = p.to_ll.transform(x, y)
        tid += 1
        out += [Detection(tid, 0.9, lon, lat, f) for f in range(frames)]
    return out


def boundary_ll(p, poly_m):
    return list(p.inv(poly_m).exterior.coords)


CFG = Config(dose_l_per_ha=10)


@pytest.mark.parametrize("lon,lat", [(73.05, 33.7), (151.2, -33.9)])  # Islamabad aur Sydney (southern)
def test_single_point(lon, lat):
    p, x0, y0, s = make(lon, lat)
    b = box(x0 - s / 2, y0 - s / 2, x0 + s / 2, y0 + s / 2)
    plan = plan_spray(boundary_ll(p, b), dets_at(p, [(x0, y0)]), cfg=CFG)
    assert plan["cells"] and plan["area_m"].area == pytest.approx(np.pi * CFG.margin_m ** 2, rel=0.05)


def test_southern_epsg():
    assert utm_epsg(151.2, -33.9) == 32756


def test_two_points_and_collinear():
    p, x0, y0, s = make(73.05, 33.7)
    b = box(x0 - 100, y0 - 100, x0 + 100, y0 + 100)
    for pts in ([(x0, y0), (x0 + 20, y0)], [(x0, y0), (x0 + 10, y0), (x0 + 20, y0)]):
        plan = plan_spray(boundary_ll(p, b), dets_at(p, pts), cfg=CFG)
        assert plan["cells"] and plan["area_m"].area > 0


def test_non_convex_boundary_not_covering_notch():
    p, x0, y0, _ = make(73.05, 33.7)
    L = Polygon([(x0, y0), (x0 + 200, y0), (x0 + 200, y0 + 40), (x0 + 40, y0 + 40),
                 (x0 + 40, y0 + 200), (x0, y0 + 200)])
    cfg = Config(dose_l_per_ha=10, cluster_eps_m=1000)   # ek hi cluster, taa-ke notch ka test ho
    plan = plan_spray(boundary_ll(p, L), dets_at(p, [(x0 + 190, y0 + 20), (x0 + 20, y0 + 190), (x0 + 20, y0 + 20)]), cfg=cfg)
    notch = box(x0 + 80, y0 + 80, x0 + 180, y0 + 180)
    assert not plan["area_m"].intersects(notch)


def test_outlier_makes_separate_cluster():
    p, x0, y0, _ = make(73.05, 33.7)
    b = box(x0 - 500, y0 - 500, x0 + 500, y0 + 500)
    plan = plan_spray(boundary_ll(p, b), dets_at(p, [(x0, y0), (x0 + 5, y0), (x0 + 400, y0 + 400)]), cfg=CFG)
    assert plan["area_m"].area < 1000   # ek bada hull nahi bana


def test_filters_low_conf_and_few_frames():
    p, x0, y0, _ = make(73.05, 33.7)
    b = box(x0 - 100, y0 - 100, x0 + 100, y0 + 100)
    d = dets_at(p, [(x0, y0)], frames=1)   # sirf 1 frame
    assert plan_spray(boundary_ll(p, b), d, cfg=CFG)["cells"] == []


def test_self_intersecting_rejected():
    with pytest.raises(ValueError):
        validate_boundary([(0, 0), (1, 1), (1, 0), (0, 1)], Config())


def test_antimeridian_rejected():
    with pytest.raises(ValueError):
        validate_boundary([(179.9, 0), (-179.9, 0), (-179.9, 1), (179.9, 1)], Config())


def test_georef_nadir_center_and_edge():
    pose = FramePose(lat=33.7, lon=73.05, rel_alt=100, yaw_deg=0, pitch_deg=-90)
    lon, lat = pixel_to_lonlat(960, 540, pose, 1920, 1080, hfov_deg=90)
    assert (lon, lat) == pytest.approx((73.05, 33.7), abs=1e-7)
    lon2, lat2 = pixel_to_lonlat(1920, 540, pose, 1920, 1080, hfov_deg=90)     # right edge: 100 m east
    # asli (geodesic) doori aur disha: UTM grid north asli north se ~1.08 degree ghuma hota hai
    az, _, dist = Geod(ellps="WGS84").inv(73.05, 33.7, lon2, lat2)
    assert dist == pytest.approx(100, abs=0.5)
    assert az % 360 == pytest.approx(90, abs=0.2)      # east


def test_georef_yaw_east_right_edge_goes_south():
    pose = FramePose(33.7, 73.05, 100, yaw_deg=90, pitch_deg=-90)
    lon2, lat2 = pixel_to_lonlat(1920, 540, pose, 1920, 1080, 90)
    az, _, dist = Geod(ellps="WGS84").inv(73.05, 33.7, lon2, lat2)
    assert dist == pytest.approx(100, abs=0.5)
    assert az % 360 == pytest.approx(180, abs=0.2)     # south


def test_waypoints_and_geojson():
    p, x0, y0, _ = make(73.05, 33.7)
    b = box(x0 - 100, y0 - 100, x0 + 100, y0 + 100)
    plan = plan_spray(boundary_ll(p, b), dets_at(p, [(x0, y0), (x0 + 15, y0 + 15)]), cfg=CFG)
    assert to_waypoints(plan).startswith("QGC WPL 110")
    assert to_geojson(plan)["features"]
    assert zone_summary(plan["cells"], plan["boundary_m"])