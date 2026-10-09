"""Real video + SRT + boundary pe end-to-end run.

Use: python run_e2e.py flight.mp4 flight.SRT boundary.json best.pt [HFOV]
boundary.json: [[lon, lat], [lon, lat], ...]
HFOV (optional): camera ka HORIZONTAL FOV degrees mein, e.g. 82. Na do to neeche wala default use hoga.
"""
import csv
import json
import sys
from ultralytics import YOLO
from gis_pipeline import *

VIDEO, SRT, BOUNDARY_JSON, WEIGHTS = sys.argv[1:5]
# apne drone camera ke spec se HORIZONTAL FOV (diagonal FOV nahi!)
# Ya to yahan default badlo, ya command ke aakhir mein number do (python run_e2e.py ... best.pt 82)
HFOV = float(sys.argv[5]) if len(sys.argv) > 5 else 82.0
PEST_CLASSES = None  # e.g. {0, 1}; None = sab classes

poses = parse_srt(SRT)
boundary = json.load(open(BOUNDARY_JSON))
model = YOLO(WEIGHTS)
dets = []
for i, r in enumerate(model.track(VIDEO, stream=True, persist=True, verbose=False)):
    pose = poses[min(i, len(poses) - 1)]
    h, w = r.orig_shape
    for b in r.boxes:
        if b.id is None:
            continue
        if PEST_CLASSES and int(b.cls) not in PEST_CLASSES:
            continue
        cx, cy = b.xywh[0][:2].tolist()
        ll = pixel_to_lonlat(cx, cy, pose, w, h, HFOV)
        if ll:
            dets.append(Detection(int(b.id), float(b.conf), ll[0], ll[1], i))

# detections pehle hi CSV mein save: approval screen (app_approval.py) ko yehi chahiye,
# aur planning mein koi error aaye to bhi detections zaya na hon
with open("dets.csv", "w", newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["track_id", "conf", "lon", "lat", "frame"])
    for d in dets:
        wr.writerow([d.track_id, d.conf, d.lon, d.lat, d.frame])

cfg = Config(dose_l_per_ha=0)   # label se bharo
plan = plan_spray(boundary, dets, cfg=cfg)
print("detections:", len(dets), "| points:", len(plan["points_m"]), "| cells:", len(plan["cells"]))
for w_ in plan["warnings"]:
    print("WARN:", w_)
plot_plan(plan).savefig("plan.png", dpi=120)
save_report("report_draft.json", plan, {"video": VIDEO})
print("saved: dets.csv, plan.png, report_draft.json")