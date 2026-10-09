"""Streamlit approval screen.

Run: streamlit run app_approval.py
Inputs: boundary JSON ([[lon, lat], ...]) aur detections CSV
(columns: track_id, conf, lon, lat, frame). Detections CSV run_e2e.py ke dets se bana sakti ho.
"""
import io
import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st

from gis_pipeline import (Config, Detection, load_report, plan_spray, plot_plan, save_report,
                          to_geojson, to_kml, to_waypoints, zone_summary)


def approval_panel(plan):
    st.pyplot(plot_plan(plan))
    st.write(f"Cells: {len(plan['cells'])} | Total dose: {sum(c['dose_l'] for c in plan['cells']):.2f} L | "
             f"Area: {plan['area_m'].area / 10000:.2f} ha")
    for w in plan["warnings"]:
        st.warning(w)
    st.dataframe(pd.DataFrame(zone_summary(plan["cells"], plan["boundary_m"])).T)  # grid se hi aati hai
    ok = st.checkbox("Maine map, inner boundary, grid aur no-spray areas dekh liye hain")
    who = st.text_input("Approver ka naam")
    if st.button("Approve", disabled=not (ok and who and plan["cells"])):
        os.makedirs("reports", exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_report(f"reports/{stamp}.json", plan, {"approved_by": who})
        st.session_state["approved"] = plan
        st.success("Approved aur save ho gaya")

    if "approved" in st.session_state:
        p = st.session_state["approved"]
        st.download_button("GeoJSON", json.dumps(to_geojson(p)), "mission.geojson")
        st.download_button("KML", to_kml(p), "mission.kml")
        st.download_button("Waypoints (ArduPilot)", to_waypoints(p), "mission.waypoints")


st.title("Spray plan approval")

with st.sidebar:
    st.header("Config (apni fasal/sprayer se set karo)")
    cfg = Config(
        margin_m=st.number_input("margin_m", value=5.0),
        cell_m=st.number_input("cell_m", value=10.0),
        min_conf=st.number_input("min_conf", value=0.5),
        min_frames=int(st.number_input("min_frames", value=3)),
        dose_l_per_ha=st.number_input("dose_l_per_ha (label se)", value=0.0),
    )

b_file = st.file_uploader("Boundary JSON", type="json")
d_file = st.file_uploader("Detections CSV", type="csv")
if b_file and d_file:
    boundary = json.load(b_file)
    df = pd.read_csv(d_file)
    dets = [Detection(int(r.track_id), float(r.conf), float(r.lon), float(r.lat), int(r.frame))
            for r in df.itertuples()]
    try:
        plan = plan_spray(boundary, dets, cfg=cfg)
        approval_panel(plan)
    except ValueError as e:
        st.error(str(e))

files = sorted(os.listdir("reports"), reverse=True) if os.path.isdir("reports") else []
pick = st.selectbox("Purani report", [""] + files)
if pick:
    rep = load_report(f"reports/{pick}")
    st.json(rep["meta"])
    st.json(rep["warnings"])
