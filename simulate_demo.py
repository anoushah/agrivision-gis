"""SIMULATION / DEMO: nakli detections, asli GIS pipeline.

Is mein drone ki koi asli video ya SRT istemal nahi hoti. Boundary ke andar kuch
nakli "infected" points banaye jate hain, phir wahi plan_spray / plot_plan / save_report
chalte hain jo asli run mein chalte hain. Isse approval screen aur GeoJSON/KML/waypoints
export tak ka flow dikhaya ja sakta hai. Ye georeferencing ka test NAHI hai.

Use:
    python simulate_demo.py                    # boundary.json (na ho to sample ban jata hai)
    python simulate_demo.py myfield.json 3     # apni boundary file, 3 infected clusters
boundary.json format: [[lon, lat], [lon, lat], ...]  (lon pehle, lat baad mein)
"""
import csv
import json
import os
import sys

import numpy as np
from shapely.geometry import Point, Polygon

from gis_pipeline import *

BOUNDARY_JSON = sys.argv[1] if len(sys.argv) > 1 else "boundary.json"
N_CLUSTERS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
SEED = 7                 # same number = same nakli points har baar
FRAMES_PER_TRACK = 5     # har point kitne frames mein "dikha"
MIN_CLUSTER_GAP_M = 60   # clusters ek dusre se kam az kam itna door
EDGE_MARGIN_M = 15       # clusters boundary ke kinare se itna andar

# SIRF placeholder: asli khet ke corners (lon, lat) se badlo
SAMPLE_BOUNDARY = [[73.0501, 33.7001], [73.0520, 33.7001], [73.0520, 33.7018], [73.0501, 33.7018]]

if not os.path.exists(BOUNDARY_JSON):
    with open(BOUNDARY_JSON, "w") as f:
        json.dump(SAMPLE_BOUNDARY, f)
    print(f"NOTE: {BOUNDARY_JSON} nahi mili, sample boundary bana di. Asli khet ke corners se badal dena.")

with open(BOUNDARY_JSON) as f:
    boundary = json.load(f)

lon0 = float(np.mean([c[0] for c in boundary]))
lat0 = float(np.mean([c[1] for c in boundary]))
p = Proj(lon0, lat0)
poly = Polygon([p.to_m.transform(lon, lat) for lon, lat in boundary])
if not poly.is_valid or poly.is_empty:
    sys.exit("ERROR: boundary valid polygon nahi hai (corners ka order ya self-intersection check karo).")

rng = np.random.default_rng(SEED)
inner = poly.buffer(-EDGE_MARGIN_M)
if inner.is_empty:
    inner = poly
minx, miny, maxx, maxy = inner.bounds


def random_point_in(shape, tries=5000):
    for _ in range(tries):
        x = rng.uniform(minx, maxx)
        y = rng.uniform(miny, maxy)
        if shape.contains(Point(x, y)):
            return x, y
    return None


# 1) clusters ke centres
centres = []
for _ in range(N_CLUSTERS):
    for _ in range(2000):
        c = random_point_in(inner)
        if c is None:
            break
        if all(np.hypot(c[0] - o[0], c[1] - o[1]) >= MIN_CLUSTER_GAP_M for o in centres):
            centres.append(c)
            break
if len(centres) < N_CLUSTERS:
    print(f"NOTE: khet chhota hai, sirf {len(centres)} cluster ban sake.")

# 2) har cluster ke gird nakli pest points
dets, tid = [], 0
for cx, cy in centres:
    for _ in range(int(rng.integers(6, 12))):
        x, y = cx + rng.normal(0, 4), cy + rng.normal(0, 4)
        if not poly.contains(Point(x, y)):
            continue
        lon, lat = p.to_ll.transform(x, y)
        tid += 1
        conf = float(rng.uniform(0.6, 0.95))
        dets += [Detection(tid, conf, lon, lat, f) for f in range(FRAMES_PER_TRACK)]

# 3) ek nakli galat detection (1 frame, kam confidence): filter dikhane ke liye
fp = random_point_in(poly)
if fp is not None:
    lon, lat = p.to_ll.transform(*fp)
    tid += 1
    dets.append(Detection(tid, 0.35, lon, lat, 0))

# detections CSV (app_approval.py ke liye)
with open("dets.csv", "w", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["track_id", "conf", "lon", "lat", "frame"])
    for d in dets:
        wr.writerow([d.track_id, d.conf, d.lon, d.lat, d.frame])

# asli pipeline
cfg = Config(dose_l_per_ha=10)   # demo value; asli dose pesticide ke label se
plan = plan_spray(boundary, dets, cfg=cfg)
print("SIMULATION | detections:", len(dets), "| points:", len(plan["points_m"]), "| cells:", len(plan["cells"]))
for w_ in plan["warnings"]:
    print("WARN:", w_)
plot_plan(plan).savefig("plan.png", dpi=120)
save_report("report_draft.json", plan, {"video": "SIMULATION (no real flight)"})
print("saved: dets.csv, plan.png, report_draft.json  (boundary:", BOUNDARY_JSON + ")")
