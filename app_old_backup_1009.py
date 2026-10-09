"""
AgriVision — Multi-Page Mobile-App Styled Streamlit Dashboard
Run with:  streamlit run app.py
Must be in the SAME folder as pest_detection_severity_spray.py
"""

import streamlit as st
import os
import json
import base64
import math
import tempfile
import re
import requests
from pathlib import Path
import pandas as pd
import streamlit.components.v1 as components
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium

from pest_detection_severity_spray import process_video, GRID_ROWS, GRID_COLS


# ==================================================================
# GIS & MISSION SIMULATION HELPERS (Shapely, GeoPandas & PyProj)
# ==================================================================
import gis_utils
from mission_sim import build_mission, zone_positions, zone_grid_polygons

SEV_COLORS = {"low": "#F59E0B", "moderate": "#F97316", "medium": "#F97316",
              "high": "#EF4444", "severe": "#B91C1C", "critical": "#B91C1C"}
DEFAULT_SEV_COLOR = "#EF4444"

def generate_boundary_and_geofence_from_zones(zones):
    if not zones:
        return False
    valid_zones = [z for z in zones if z.get("latitude") is not None and z.get("longitude") is not None]
    if len(valid_zones) < 3:
        return False
    coords = [(z["latitude"], z["longitude"]) for z in valid_zones]
    hull = gis_utils.build_convex_hull(coords)
    if hull and len(hull) >= 3:
        st.session_state.field_boundary = [
            {"lat": pt[0], "lon": pt[1], "name": f"Vertex {i+1}"}
            for i, pt in enumerate(hull)
        ]
        fit_geofence_to_boundary(st.session_state.field_boundary)
        return True
    return False

# ------------------------------------------------------------------
# animated map (self-contained Leaflet page)
# ------------------------------------------------------------------
_HTML = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 html,body{margin:0;padding:0;font-family:Segoe UI,Arial,sans-serif;background:transparent}
 #wrap{display:flex;flex-wrap:wrap;gap:10px}
 #map{flex:2 1 420px;height:460px;border-radius:14px;border:2px solid #254E06}
 #side{flex:1 1 220px;min-width:210px}
 .badge{display:inline-block;background:#254E06;color:#F5EAB9;font-weight:700;font-size:12px;padding:4px 10px;border-radius:12px;margin-bottom:6px}
 #hud{background:#F5EAB9;color:#254E06;border:1px solid #7E8407;border-radius:12px;padding:8px 10px;font-size:13px;margin-bottom:8px}
 #hud b{display:block;font-size:14px}
 table{width:100%;border-collapse:collapse;font-size:12px;background:#fffdf3;color:#254E06;border-radius:10px;overflow:hidden}
 th{background:#7E8407;color:#fff;padding:5px;text-align:left} td{padding:5px;border-top:1px solid #e6dca8}
 .st{font-weight:800;padding:2px 7px;border-radius:9px;color:#fff;font-size:11px}
 button{background:#7E8407;color:#fff;border:0;border-radius:10px;padding:8px 14px;font-weight:700;cursor:pointer;margin:6px 6px 0 0}
 button.dark{background:#254E06} select{padding:6px;border-radius:8px}
</style></head><body>
<div id="wrap">
 <div id="map"></div>
 <div id="side">
  <span class="badge">SIMULATION MODE</span>
  <div id="hud"><b id="phase">Ready</b><span id="pos">-</span></div>
  <table><thead><tr><th>Zone</th><th>Severity</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table>
  <div>
   <button id="go">Start</button><button id="pause" class="dark">Pause</button><button id="reset" class="dark">Reset</button><br>
   Speed <select id="spd"><option value="1">1x</option><option value="2" selected>2x</option><option value="4">4x</option><option value="8">8x</option></select>
  </div>
 </div>
</div>
<script>
const D = __DATA__;
const KEY = __KEY__;
const map = L.map('map', {zoomControl:true});
if (KEY) {
  L.tileLayer('https://api.maptiler.com/tiles/satellite-v2/{z}/{x}/{y}.jpg?key='+KEY, {maxZoom:20, tileSize:512, zoomOffset:-1, attribution:'MapTiler'}).addTo(map);
} else {
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'OpenStreetMap'}).addTo(map);
}
const outer = L.polygon(D.outer, {color:'#10B981', weight:3, fillOpacity:0.08}).addTo(map).bindPopup('Outer boundary (geofence)');
map.fitBounds(outer.getBounds(), {padding:[20,20]});
let innerLayer = null, surveyLine = null, sprayLine = null, drone = null;
const droneIcon = L.divIcon({html:'<div style="font-size:26px;filter:drop-shadow(0 1px 2px #000)">&#128641;</div>', className:'', iconSize:[28,28], iconAnchor:[14,14]});
const mk = {};
const status = {};
const COL = {Pending:'#F59E0B', Spraying:'#2563EB', Sprayed:'#10B981', Detected:'#F59E0B'};

function renderTable(){
  const tb = document.getElementById('rows'); tb.innerHTML='';
  D.points.forEach(p=>{
    const s = status[p.zone_id];
    if(!s) return;
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>Z'+p.zone_id+'</td><td>'+p.severity+'</td><td><span class="st" style="background:'+COL[s]+'">'+s+'</span></td>';
    tb.appendChild(tr);
  });
}
function setPoint(p, s){
  status[p.zone_id] = s;
  const c = COL[s];
  if(!mk[p.zone_id]){
    mk[p.zone_id] = L.circleMarker([p.lat,p.lon], {radius:9, color:'#fff', weight:2, fillOpacity:0.95, fillColor:c}).addTo(map);
  }
  mk[p.zone_id].setStyle({fillColor:c});
  mk[p.zone_id].bindPopup('Zone '+p.zone_id+' ('+p.severity+')<br>'+p.lat.toFixed(6)+', '+p.lon.toFixed(6)+'<br>'+s);
  renderTable();
}

let phase = 'idle', i = 0, timer = null, paused = false, sprayHold = 0, nextTarget = 0;
function hud(txt, pos){
  document.getElementById('phase').textContent = txt;
  document.getElementById('pos').textContent = pos ? pos[0].toFixed(6)+', '+pos[1].toFixed(6) : '-';
}
function clearAll(){
  Object.values(mk).forEach(m=>map.removeLayer(m));
  for (const k in mk) delete mk[k];
  for (const k in status) delete status[k];
  [innerLayer, surveyLine, sprayLine, drone].forEach(l=>{ if(l) map.removeLayer(l); });
  innerLayer = surveyLine = sprayLine = drone = null;
  renderTable();
}
function reset(){
  if(timer) clearInterval(timer); timer=null; phase='idle'; i=0; sprayHold=0; nextTarget=0; paused=false;
  clearAll(); hud('Ready', D.start);
  document.getElementById('go').textContent = 'Start';
}
function tick(){
  if(paused) return;
  if(phase==='survey'){
    const pos = D.survey[i];
    drone.setLatLng(pos);
    surveyLine.addLatLng(pos);
    D.points.forEach(p=>{ if(!status[p.zone_id] && p.detect_idx<=i) setPoint(p,'Pending'); });
    hud('Survey flight: scanning field', pos);
    i++;
    if(i>=D.survey.length){
      D.points.forEach(p=>{ if(!status[p.zone_id]) setPoint(p,'Pending'); });
      if(D.inner.length>=3){
        innerLayer = L.polygon(D.inner, {color:'#EF4444', weight:3, dashArray:'6 6', fillOpacity:0.12}).addTo(map).bindPopup('Inner boundary (affected area)');
      }
      phase='spray'; i=0; nextTarget=0; sprayLine = L.polyline([], {color:'#2563EB', weight:3}).addTo(map);
    }
  } else if(phase==='spray'){
    if(sprayHold>0){
      sprayHold--;
      const p = D.points[nextTarget-1];
      hud('Spraying zone '+p.zone_id, [p.lat,p.lon]);
      if(sprayHold===0) setPoint(p,'Sprayed');
      return;
    }
    if(i>=D.spray_route.length){
      phase='done'; clearInterval(timer); timer=null;
      hud('Mission complete: all affected points sprayed', D.start);
      document.getElementById('go').textContent='Replay';
      return;
    }
    const pos = D.spray_route[i];
    drone.setLatLng(pos); sprayLine.addLatLng(pos);
    hud('Flying to spray point', pos);
    if(nextTarget<D.points.length && i===D.arrive_idx[nextTarget]){
      setPoint(D.points[nextTarget],'Spraying');
      nextTarget++; sprayHold=12;
    }
    i++;
  }
}
function start(){
  if(phase==='done' || phase==='idle'){
    reset();
    drone = L.marker(D.start, {icon:droneIcon}).addTo(map);
    surveyLine = L.polyline([], {color:'#FACC15', weight:2, opacity:0.9}).addTo(map);
    phase='survey'; i=0;
  }
  paused=false;
  if(timer) clearInterval(timer);
  const sp = parseInt(document.getElementById('spd').value);
  timer = setInterval(tick, Math.max(10, 80/sp));
  document.getElementById('go').textContent='Running';
}
document.getElementById('go').onclick = start;
document.getElementById('pause').onclick = ()=>{ paused=!paused; document.getElementById('pause').textContent = paused?'Resume':'Pause'; };
document.getElementById('reset').onclick = reset;
document.getElementById('spd').onchange = ()=>{ if(timer && phase!=='done'){ clearInterval(timer); timer=setInterval(tick, Math.max(10, 80/parseInt(document.getElementById('spd').value))); } };
reset();
</script></body></html>
"""


def mission_html(mission, maptiler_key=None):
    data = {k: mission[k] for k in ("outer", "inner", "survey", "spray_route", "arrive_idx", "points", "start")}
    return (_HTML.replace("__DATA__", json.dumps(data))
                 .replace("__KEY__", json.dumps(maptiler_key or "")))


# ------------------------------------------------------------------
# Streamlit section (call from the Field Map & GPS page)
# ------------------------------------------------------------------
def render_mission_section(st, pd, boundary, boundary_is_default, zones,
                           maptiler_key=None, get_sub_areas=None, render_table=None):
    st.markdown("#### 🚁 6. Simulated Survey & Spray Mission")
    st.write("Simulation mode: the drone flies a survey pattern inside your outer boundary, finds the affected points, "
             "the affected points are joined into an inner boundary, and the drone sprays them one by one. "
             "All positions are real map coordinates. With real hardware, the simulated flight track is replaced by the drone's GPS log.")

    if boundary_is_default:
        st.info("Draw your field's outer boundary (polygon) on the map above first. The default demo boundary is only a placeholder.")
    if not zones:
        st.info("Run the detection pipeline on the Home page so the mission knows which zones are affected.")
        return

    spacing = st.slider("Survey line spacing (m)", 3, 40, 0, help="0 = automatic", key="mission_spacing") or None
    margin = st.slider("Inner boundary margin (m)", 0, 15, 3, key="mission_margin")

    m = build_mission(boundary, zones, line_spacing_m=spacing, inner_margin_m=float(margin))
    if "error" in m:
        st.warning(m["error"])
        return

    if not m["points"]:
        st.success("No affected zones in the report, so there is nothing to spray. The drone would only fly the survey.")
    if m["skipped_zones"]:
        st.warning(f"Zone(s) {m['skipped_zones']} fall outside your outer boundary shape and were left out. "
                   "Draw the boundary so all 9 zones are covered.")
    if m["route_leaves_outer"]:
        st.warning("Your outer boundary is not convex, so a few straight flight legs may leave it. Use a simpler shape for the demo.")

    components.html(mission_html(m, maptiler_key), height=520, scrolling=False)

    c1, c2, c3 = st.columns(3)
    c1.metric("Affected points", len(m["points"]))
    c2.metric("Survey path", f"{m['survey_length_m']:,.0f} m")
    c3.metric("Line spacing", f"{m['line_spacing_m']:.0f} m")

    rows = [[f"<strong>{p['rank']}</strong>", f"Zone {p['zone_id']}", p["severity"].capitalize(),
             f"<code>{p['lat']:.6f}</code>", f"<code>{p['lon']:.6f}</code>"] for p in m["points"]]
    if rows and render_table:
        st.markdown("##### Spray order and coordinates (WGS84)")
        render_table(["Order", "Zone", "Severity", "Latitude", "Longitude"], rows)

    if m["inner"]:
        inner_rows = [[f"I{i + 1}", f"<code>{p[0]:.6f}</code>", f"<code>{p[1]:.6f}</code>"] for i, p in enumerate(m["inner"])]
        if render_table:
            st.markdown("##### Inner boundary vertices")
            render_table(["Vertex", "Latitude", "Longitude"], inner_rows)

    if m["points"]:
        df = pd.DataFrame([{"Order": p["rank"], "Zone": p["zone_id"], "Severity": p["severity"],
                            "Latitude": p["lat"], "Longitude": p["lon"]} for p in m["points"]])
        st.download_button("⬇️ Download mission points (.CSV)", df.to_csv(index=False).encode("utf-8"),
                           file_name="AgriVision_Mission_Points.csv", mime="text/csv",
                           use_container_width=True, key="mission_dl")

        if get_sub_areas and st.button("✅ Mark these zones as Sprayed in Spray Status", use_container_width=True,
                                       key="mission_apply"):
            for p in m["points"]:
                for sa in get_sub_areas(p["zone_id"], p["severity"], True):
                    sa["status"], sa["badge"], sa["color"] = "Sprayed", "🟢 Sprayed (Completed)", "#10B981"
            st.success("Spray status updated for the affected zones.")
    st.caption("Simulation: positions come from the drawn boundary and the video's zone results, not from a drone. "
               "Map and GPS accuracy is typically 1 to 3 m.")

import html as _html
import gis_utils


def flat(markup: str) -> str:
    """Collapse an HTML snippet to a single line before st.markdown(unsafe_allow_html=True)."""
    return " ".join(line.strip() for line in str(markup).splitlines() if line.strip())


def esc(value) -> str:
    """HTML-escape dynamic text so it can't break the markup."""
    return _html.escape(str(value), quote=True)


st.set_page_config(
    page_title="AgriVision — Smart Drone Crop Protection",
    layout="wide",
    page_icon="🌾",
    initial_sidebar_state="expanded"
)

# ==================================================================
# GIS & GEOFENCE UTILITIES (Shapely, GeoPandas & PyProj)
# ==================================================================
def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    return gis_utils.calculate_gis_distance(lat1, lon1, lat2, lon2)

def check_geofence(lat, lon, center_lat, center_lon, radius_m):
    if center_lat is None or center_lon is None:
        return True, 0.0
    return gis_utils.check_geofence_gis(lat, lon, center_lat, center_lon, radius_m)

def fit_geofence_to_zones(zones):
    if not zones:
        return
    lats = [z["latitude"] for z in zones if z.get("latitude") is not None]
    lons = [z["longitude"] for z in zones if z.get("longitude") is not None]
    if not lats or not lons:
        return
    center_lat, center_lon = sum(lats) / len(lats), sum(lons) / len(lons)
    farthest = max(gis_utils.calculate_gis_distance(center_lat, center_lon, la, lo) for la, lo in zip(lats, lons))
    st.session_state.geofence_center_lat = round(center_lat, 6)
    st.session_state.geofence_center_lon = round(center_lon, 6)
    st.session_state.geofence_radius_m = float(min(1000.0, max(50.0, round(farthest * 1.2 + 10.0, 1))))


def point_in_polygon(lat, lon, polygon):
    poly = gis_utils.coords_to_polygon(polygon)
    if poly is None:
        return True
    return gis_utils.point_inside_polygon(lat, lon, poly)


def fit_geofence_to_boundary(boundary):
    poly = gis_utils.coords_to_polygon(boundary)
    if poly is None:
        return
    c_lat, c_lon, radius = gis_utils.fit_geofence_to_boundary_gis(poly)
    st.session_state.geofence_center_lat = c_lat
    st.session_state.geofence_center_lon = c_lon
    st.session_state.geofence_radius_m = radius



def geocode_location(query):
    """Convert an address / place name (or "lat, lon") into (lat, lon, formatted_address, status)
    using LocationIQ. The API key is read from st.secrets["LOCATIONIQ_KEY"].
    status is one of: "success", "empty", "not_found", "network_error", "error"
    (for "error" the third value is a short human-readable reason)."""
    if not query or not query.strip():
        return None, None, None, "empty"
    query_str = query.strip()

    coord_match = re.match(r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", query_str)
    if coord_match:
        try:
            lat = float(coord_match.group(1))
            lon = float(coord_match.group(2))
            return lat, lon, f"Coordinates: {lat:.6f}, {lon:.6f}", "success"
        except ValueError:
            pass

    try:
        api_key = st.secrets["LOCATIONIQ_KEY"]
    except Exception:
        return None, None, "LOCATIONIQ_KEY not found in .streamlit/secrets.toml", "error"

    try:
        resp = requests.get(
            "https://us1.locationiq.com/v1/search",
            params={"key": api_key, "q": query_str, "format": "json", "limit": 1},
            timeout=8,
        )
    except Exception:
        return None, None, None, "network_error"

    if resp.status_code == 200:
        try:
            data = resp.json()
            if data:
                item = data[0]
                return float(item["lat"]), float(item["lon"]), str(item.get("display_name", query_str)), "success"
        except Exception:
            return None, None, "Unexpected response from LocationIQ", "error"
        return None, None, None, "not_found"
    if resp.status_code == 404:
        return None, None, None, "not_found"
    if resp.status_code in (401, 403):
        return None, None, "LocationIQ rejected the API key (HTTP %d)" % resp.status_code, "error"
    if resp.status_code == 429:
        return None, None, "LocationIQ free-tier request limit reached (HTTP 429)", "error"
    return None, None, "LocationIQ search failed (HTTP %d)" % resp.status_code, "error"


# ==================================================================
# ACCESSIBLE PEST & DISEASE CATALOG
# ==================================================================
PEST_ACCESSIBLE_INFO = {
    "rust": {
        "name": "Corn Leaf Rust",
        "name_ur": "مئی کا زنگ (Corn Rust)",
        "icon": "🌽",
        "desc": "Fungal rust disease creating reddish-brown spots on crop leaves.",
        "desc_ur": "پودوں کے پتوں پر سرخ مائل بھورے دھبے بنانے والی فنگس بیماری۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Apply Fungicide (Mancozeb or Azoxystrobin) & reduce canopy dampness."
    },
    "blight": {
        "name": "Leaf Blight Disease",
        "name_ur": "پتوں کا جھلساؤ (Blight)",
        "icon": "🌾",
        "desc": "Fungal infection causing dried, grayish-brown leaf lesions.",
        "desc_ur": "پتوں کو خشک اور بھورا کر دینے والی شدید فنگل انفیکشن۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Spray Copper oxychloride or propiconazole."
    },
    "mildew": {
        "name": "Powdery Mildew",
        "name_ur": "سفید پھپھوندی (Mildew)",
        "icon": "🌫️",
        "desc": "White powdery fungal spots coating the leaf surface.",
        "desc_ur": "پتے کی سطح پر سفید پاؤڈر نما دھبے بننا۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Sulfur-based spray or Neem oil application."
    },
    "stink bug": {
        "name": "Spotted Stink Bug",
        "name_ur": "بدبودار کیڑا (Stink Bug)",
        "icon": "🪲",
        "desc": "Shield-shaped bug that punctures leaves and fruits to suck sap.",
        "desc_ur": "پتوں اور پھلوں سے رس چوسنے والا ڈھال نما کیڑا۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Pyrethroid insecticides or biopesticide."
    },
    "locust": {
        "name": "Locust Swarm",
        "name_ur": "ٹڈی دل (Locust)",
        "icon": "🦗",
        "desc": "Chewing insect swarm that destroys crop foliage rapidly.",
        "desc_ur": "فصلوں کے پتوں اور تنے کو منٹوں میں تباہ کرنے والا کیڑا۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Targeted ultra-low-volume organophosphate aerial spray."
    },
    "thrips": {
        "name": "Thrips Pest",
        "name_ur": "تھریپس (Thrips)",
        "icon": "🪰",
        "desc": "Tiny slender insects causing silvery leaf streaks and curling.",
        "desc_ur": "پتوں کو موڑنے اور چاندی نما دھبے بنانے والے باریک کیڑے۔",
        "risk_badge": "🟡 Low Risk",
        "treatment": "Imidacloprid or Spinosad spray."
    },
    "bollworm": {
        "name": "Cotton Bollworm",
        "name_ur": "امریکی سنڈی (Bollworm)",
        "icon": "🐛",
        "desc": "Caterpillar larva eating crop leaves, buds, and fruit.",
        "desc_ur": "پتوں اور ڈوڈیوں کو کھانے والی خطرناک سنڈی۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Emamectin benzoate or Chlorantraniliprole."
    },
    "aphid": {
        "name": "Aphid Colony",
        "name_ur": "سست تیلہ (Aphids)",
        "icon": "🐜",
        "desc": "Small sap-sucking insects causing yellowing and stunted growth.",
        "desc_ur": "رس چوسنے والے چھوٹے کیڑے جو فصل کو زرد کر دیتے ہیں۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Acetamiprid spray & ladybug bio-control."
    },
    "borer": {
        "name": "Stem Borer",
        "name_ur": "تنے کی سنڈی (Stem Borer)",
        "icon": "🐛",
        "desc": "Tunneling moth larva boring inside plant stalks and stems.",
        "desc_ur": "پودے کے تنے کے اندر سوراخ کر کے کھوکھلا کرنے والی سنڈی۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Cartap hydrochloride or Granular Carbofuran."
    },
    "fall armyworm": {
        "name": "Fall Armyworm",
        "name_ur": "فال آرمی ورم (Fall Armyworm)",
        "icon": "🐛",
        "desc": "Aggressive caterpillar attacking maize, sorghum, and sugarcane whorls.",
        "desc_ur": "مکئی اور دیگر فصلوں کے درمیانی حصے کو کھانے والی خطرناک سنڈی۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Spinetoram or Chlorantraniliprole whorl application."
    },
    "chlorosis": {
        "name": "Nitrogen / HSV Chlorosis",
        "name_ur": "زردی / کلوروسس (Chlorosis)",
        "icon": "🍂",
        "desc": "Yellowing of leaf tissue due to nutrient deficiency or root stress.",
        "desc_ur": "غذائی اجزاء کی کمی یا جڑوں میں خرابی کی وجہ سے پتوں کا پیلا ہونا۔",
        "risk_badge": "🟡 Low Risk",
        "treatment": "Targeted Nitrogen (Urea) soil fertilizer & micronutrient spray."
    },
    "necrosis": {
        "name": "Necrotic Spotting",
        "name_ur": "مرجھایا ہوا خلیہ (Necrosis)",
        "icon": "🟤",
        "desc": "Dead brown tissue spots caused by drought or fungal decay.",
        "desc_ur": "خشک سالی یا شدید فنگس سے پودے کے خلیات کا مٹ جانا۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Irrigation optimization & broad-spectrum bio-fungicide."
    }
}


# ==================================================================
# TRANSLATIONS DICTIONARY
# ==================================================================
TRANSLATIONS = {
    "en": {
        "app_title": "AgriVision",
        "app_subtitle": "Drone Precision Pest Detection & Spray Optimization",
        "nav_home": "🏠 Home / Overview",
        "nav_visual": "📊 Visual Report",
        "nav_gps": "📍 Field Map & GPS",
        "nav_problems": "🔍 Problem Areas",
        "nav_spray": "💦 Spray Advisory",
        "nav_pests": "🐛 Pest Catalog",
        "nav_settings": "⚙️ Settings",
        
        "hero_title": "Smart Drone Crop Protection Platform",
        "hero_desc": "Real-time ground & aerial YOLO object detection combined with HSV vegetation analysis, field boundary definition, sub-area spray mapping, and variable-rate chemical spray advisory.",
        
        "metric_total_zones": "Total Field Zones",
        "metric_problem_zones": "Problem Zones",
        "metric_spray_zones": "Zones Needing Spray",
        "metric_health_status": "Overall Crop Status",
        
        "status_healthy": "✅ Healthy Field",
        "status_action_needed": "⚠️ Action Required",
        
        "upload_heading": "📹 Video Input & Detection Pipeline",
        "upload_label": "Upload Drone / Field Video (.mp4, .avi, .mov)",
        "demo_select_label": "Or Select Demo Video",
        "btn_run_pipeline": "▶️ Run Detection Pipeline",
        "processing_msg": "Processing drone video frame-by-frame... Please wait.",
        
        "empty_title": "No Detection Data Available Yet",
        "empty_desc": "Please upload a video and click 'Run Detection Pipeline' on the Home page to generate full crop diagnostic report.",
        "btn_go_home": "👉 Go to Home Page",
        
        "report_ready_msg": "Diagnostics complete! Explore details in the navigation tabs.",
        "view_full_report": "View Full Diagnostic Report ➔",
        
        "no_issue": "No issue detected",
        "no_pests": "No pest detected",
        "sev_none": "Healthy",
        "sev_low": "Low",
        "sev_medium": "Medium",
        "sev_high": "High",
        "clean_canopy": "Healthy canopy greenness. Zero pest or chlorosis signals detected.",
        
        "table_zone": "Zone",
        "table_lat": "Latitude",
        "table_lon": "Longitude",
        "table_dist": "Distance to Target",
        "table_geofence": "Geofence Status",
        "table_alt": "Rec. Altitude",
        "table_severity": "Severity",
        "table_spray_status": "Spray Action",
        "table_dose": "Dosage (L/ha)",
        "table_chemical": "Chemical Formulation",
        "table_drone_flow": "Flow Rate",
        
        "geofence_inside": "🟢 INSIDE TARGET",
        "geofence_outside": "🔴 OUTSIDE BOUNDARY",
        
        "settings_title": "System Settings & Configuration",
        "theme_toggle_label": "🎨 Interface Theme",
        "theme_light": "Light Mode",
        "theme_dark": "Dark Mode",
        "lang_toggle_label": "🌐 Interface Language",
        "lang_en": "English",
        "lang_ur": "اردو (Urdu Script)",
        "geofence_setup": "🗺️ Geofence Center Setup",
        "center_lat": "Center Latitude",
        "center_lon": "Center Longitude",
        "geofence_radius": "Fence Radius (meters)"
    },
    "ur": {
        "app_title": "ایگری ویژن",
        "app_subtitle": "ڈراؤن کے ذریعے فصلوں کی دیکھ بھال اور کیڑوں کی تشخیص",
        "nav_home": "🏠 ہوم / جائزہ",
        "nav_visual": "📊 تصویری رپورٹ",
        "nav_gps": "📍 جی پی ایس لوکیشن",
        "nav_problems": "🔍 متاثرہ علاقے",
        "nav_spray": "💦 سپرے مشورہ",
        "nav_pests": "🐛 کیڑوں کی معلومات",
        "nav_settings": "⚙️ سیٹنگز",
        
        "hero_title": "سمارٹ ڈرون کراپ پروٹیکشن سسٹم",
        "hero_desc": "یولو (YOLO) جدید آبجیکٹ ڈیٹیکشن، ڈرون امیجنگ اور سپرے ڈوز کا جدید ترین نظام۔",
        
        "metric_total_zones": "کل کھیت زونز",
        "metric_problem_zones": "متاثرہ زونز",
        "metric_spray_zones": "سپرے کی ضرورت والے زون",
        "metric_health_status": "فصل کی مجموعی صورتحال",
        
        "status_healthy": "✅ فصل بالکل ٹھیک ہے",
        "status_action_needed": "⚠️ فوری توجہ درکار ہے",
        
        "upload_heading": "📹 ویڈیو اپ لوڈ کریں اور تشخیص شروع کریں",
        "upload_label": "ڈرون یا کھیت کی ویڈیو اپ لوڈ کریں (.mp4, .avi, .mov)",
        "demo_select_label": "یا ڈیمو ویڈیو منتخب کریں",
        "btn_run_pipeline": "▶️ پروسیسنگ شروع کریں",
        "processing_msg": "ڈرون ویڈیو کی پروسیسنگ جاری ہے... براہ کرم انتظار کریں۔",
        
        "empty_title": "فی الحال کوئی رپورٹ موجود نہیں ہے",
        "empty_desc": "رپورٹ دیکھنے کے لیے براہ کرم ہوم پیج پر جا کر ویڈیو اپ لوڈ کریں اور پروسیسنگ چلائیں۔",
        "btn_go_home": "👉 ہوم پیج پر جائیں",
        
        "report_ready_msg": "پروسیسنگ مکمل ہو گئی! تمام نتائج دیکھنے کے لیے سائڈبار مینو استعمال کریں۔",
        "view_full_report": "مکمل رپورٹ دیکھیں ➔",
        
        "no_issue": "کوئی مسئلہ نہیں پایا گیا",
        "no_pests": "کوئی کیڑا نہیں ملا",
        "sev_none": "صحت مند",
        "sev_low": "کم",
        "sev_medium": "درمیانہ",
        "sev_high": "شدید",
        "clean_canopy": "فصل صحت مند اور سبز ہے۔ کیڑوں یا بیماری کی کوئی علامت نہیں ہے۔",
        
        "table_zone": "زون",
        "table_lat": "لیٹیٹیوڈ",
        "table_lon": "لانگیٹیوڈ",
        "table_dist": "فاصلہ (میٹر)",
        "table_geofence": "جیو فینس کی صورتحال",
        "table_alt": "ڈرون کی بلندی",
        "table_severity": "شدت",
        "table_spray_status": "سپرے ایکشن",
        "table_dose": "مقدار (لیٹر فی ہیکٹر)",
        "table_chemical": "دوائی کا نام",
        "table_drone_flow": "ڈرون کا فلو ریٹ",
        
        "geofence_inside": "🟢 اندر موجود ہے",
        "geofence_outside": "🔴 حدود سے باہر",
        
        "settings_title": "سستم سیٹنگز",
        "theme_toggle_label": "🎨 انٹرفیس تھیم",
        "theme_light": "لائٹ موڈ",
        "theme_dark": "ڈارک موڈ",
        "lang_toggle_label": "🌐 زبان (Language)",
        "lang_en": "English",
        "lang_ur": "اردو (Urdu Script)",
        "geofence_setup": "🗺️ جیو فینس سنٹر کی لوکیشن",
        "center_lat": "مرکزی لیٹیٹیوڈ",
        "center_lon": "مرکزی لانگیٹیوڈ",
        "geofence_radius": "حدود کا رداس (میٹر)"
    }
}


# ==================================================================
# SESSION STATE INITIALIZATION
# ==================================================================
if "current_page" not in st.session_state:
    st.session_state.current_page = "home"
if "language" not in st.session_state:
    st.session_state.language = "en"
if "theme" not in st.session_state:
    st.session_state.theme = "light"
if "report" not in st.session_state:
    st.session_state.report = None
if "geofence_center_lat" not in st.session_state:
    st.session_state.geofence_center_lat = None
if "geofence_center_lon" not in st.session_state:
    st.session_state.geofence_center_lon = None
if "geofence_radius_m" not in st.session_state:
    st.session_state.geofence_radius_m = 50.0

# Bumped on every successful search so the folium map component is rebuilt from scratch
if "map_version" not in st.session_state:
    st.session_state.map_version = 0

if "field_boundary" not in st.session_state:
    st.session_state.field_boundary = []

if "boundary_is_default" not in st.session_state:
    st.session_state.boundary_is_default = False
if "targets_are_default" not in st.session_state:
    st.session_state.targets_are_default = False

if "sub_area_sprays" not in st.session_state:
    st.session_state.sub_area_sprays = {}

if "marked_ground_areas" not in st.session_state:
    st.session_state.marked_ground_areas = []


lang = st.session_state.language
theme = st.session_state.theme
t = TRANSLATIONS[lang]


def get_sub_areas_for_zone(zone_id, severity="none", spray_needed=False):
    """Retrieve or initialize sub-area spray definitions for a specific grid zone."""
    zid_str = str(zone_id)
    if zid_str not in st.session_state.sub_area_sprays:
        if spray_needed or severity != "none":
            # every affected sub-area starts as Pending; the mission / the user changes it later
            st.session_state.sub_area_sprays[zid_str] = [
                {"sub_id": f"Z{zid_str}-{q}", "quadrant": f"{name} Sub-Zone", "status": "Spray Required", "badge": "🔴 Pending Spray", "color": "#EF4444"}
                for q, name in (("NW", "North-West"), ("NE", "North-East"), ("SW", "South-West"), ("SE", "South-East"))
            ]
        else:
            st.session_state.sub_area_sprays[zid_str] = [
                {"sub_id": f"Z{zid_str}-NW", "quadrant": "North-West Sub-Zone", "status": "Healthy / No Spray", "badge": "⚪ Healthy Zone", "color": "#10B981"},
                {"sub_id": f"Z{zid_str}-NE", "quadrant": "North-East Sub-Zone", "status": "Healthy / No Spray", "badge": "⚪ Healthy Zone", "color": "#10B981"},
                {"sub_id": f"Z{zid_str}-SW", "quadrant": "South-West Sub-Zone", "status": "Healthy / No Spray", "badge": "⚪ Healthy Zone", "color": "#10B981"},
                {"sub_id": f"Z{zid_str}-SE", "quadrant": "South-East Sub-Zone", "status": "Healthy / No Spray", "badge": "⚪ Healthy Zone", "color": "#10B981"},
            ]
    return st.session_state.sub_area_sprays[zid_str]


def sync_zones_to_boundary():
    """Once the user has drawn an outer boundary, give every pipeline zone a GPS point on that
    boundary (same positions the mission uses). Safe to call repeatedly."""
    rep = st.session_state.get("report")
    if rep is None or st.session_state.get("boundary_is_default", True):
        return
    pos = zone_positions(st.session_state.field_boundary, GRID_ROWS, GRID_COLS)
    for z in rep.get("zones", []):
        p = pos.get((z.get("row"), z.get("col")))
        if p:
            z["latitude"], z["longitude"] = p


# ==================================================================
# INJECT CUSTOM STYLING (THEME, GRADIENTS & CONTRAST OVERRIDES)
# ==================================================================
def inject_custom_styles(theme_mode, lang_code):
    is_dark = (theme_mode == "dark")
    is_ur = (lang_code == "ur")

    if is_dark:
        bg_main = "#0F172A"
        page_gradient = "linear-gradient(160deg, #0B1220 0%, #0F172A 50%, #0B2A24 100%)"
        sidebar_bg = "linear-gradient(180deg, #0F172A 0%, #1E293B 100%)"
        bg_card = "linear-gradient(145deg, #1E293B 0%, #0F172A 100%)"
        bg_card_secondary = "#334155"
        text_primary = "#F8FAFC"
        text_secondary = "#94A3B8"
        border_color = "#334155"
        accent_green = "#10B981"
        btn_gradient = "linear-gradient(135deg, #059669 0%, #10B981 100%)"
        btn_hover = "linear-gradient(135deg, #047857 0%, #059669 100%)"
        table_bg = "#1E293B"
        table_head_gradient = "linear-gradient(135deg, #064E3B 0%, #1E3A5F 100%)"
        table_row_alt = "#0F172A"
        nav_active_bg = "linear-gradient(135deg, #059669 0%, #10B981 100%)"
        widget_bg = "#1E293B"
        widget_border = "#334155"
        header_bg = "rgba(15, 23, 42, 0.88)"
        progress_track = "#334155"
        code_bg = "#0F172A"
        code_text = "#34D399"
        alert_info = ("#0C4A6E", "#E0F2FE", "#38BDF8")
        alert_success = ("#14532D", "#DCFCE7", "#4ADE80")
        alert_warning = ("#78350F", "#FEF3C7", "#FBBF24")
        alert_error = ("#7F1D1D", "#FEE2E2", "#F87171")
        sev_styles = {
            "none":   ("linear-gradient(135deg, #064E3B 0%, #022C22 100%)", "#10B981", "#A7F3D0"),
            "low":    ("linear-gradient(135deg, #451A03 0%, #290F02 100%)", "#F59E0B", "#FDE68A"),
            "medium": ("linear-gradient(135deg, #7C2D12 0%, #431407 100%)", "#F97316", "#FDBA74"),
            "high":   ("linear-gradient(135deg, #7F1D1D 0%, #450A0A 100%)", "#EF4444", "#FCA5A5"),
        }
    else:
        bg_main = "#F8FAFC"
        page_gradient = "linear-gradient(160deg, #ECFDF5 0%, #F8FAFC 40%, #E0F2FE 100%)"
        sidebar_bg = "linear-gradient(180deg, #F0FDF4 0%, #DCFCE7 55%, #E0F2FE 100%)"
        bg_card = "linear-gradient(145deg, #FFFFFF 0%, #F0FDF4 100%)"
        bg_card_secondary = "#ECFDF5"
        text_primary = "#0F172A"
        text_secondary = "#475569"
        border_color = "#CFE8DA"
        accent_green = "#059669"
        btn_gradient = "linear-gradient(135deg, #059669 0%, #10B981 100%)"
        btn_hover = "linear-gradient(135deg, #047857 0%, #059669 100%)"
        table_bg = "#FFFFFF"
        table_head_gradient = "linear-gradient(135deg, #D1FAE5 0%, #DBEAFE 100%)"
        table_row_alt = "#F0FDF4"
        nav_active_bg = "linear-gradient(135deg, #059669 0%, #10B981 100%)"
        widget_bg = "#FFFFFF"
        widget_border = "#B7D8C4"
        header_bg = "rgba(248, 250, 252, 0.88)"
        progress_track = "#D1E7DB"
        code_bg = "#ECFDF5"
        code_text = "#047857"
        alert_info = ("#E0F2FE", "#075985", "#7DD3FC")
        alert_success = ("#DCFCE7", "#166534", "#86EFAC")
        alert_warning = ("#FEF3C7", "#92400E", "#FCD34D")
        alert_error = ("#FEE2E2", "#991B1B", "#FCA5A5")
        sev_styles = {
            "none":   ("linear-gradient(135deg, #BBF7D0 0%, #4ADE80 100%)", "#16A34A", "#052E16"),
            "low":    ("linear-gradient(135deg, #FEF08A 0%, #FACC15 100%)", "#CA8A04", "#422006"),
            "medium": ("linear-gradient(135deg, #FED7AA 0%, #FB923C 100%)", "#EA580C", "#431407"),
            "high":   ("linear-gradient(135deg, #FECACA 0%, #F87171 100%)", "#DC2626", "#450A0A"),
        }

    rtl_direction = "rtl" if is_ur else "ltr"
    color_scheme = "dark" if is_dark else "light"
    font_family = "'Urdu Typesetting', 'Nastaliq', 'Segoe UI', Tahoma, Geneva, sans-serif" if is_ur else "'Inter', 'Segoe UI', Roboto, sans-serif"

    sev_css = ""
    for level, (sev_bg, sev_border, sev_text) in sev_styles.items():
        sev_css += (
            f".severity-{level} {{ background: {sev_bg} !important; "
            f"border-color: {sev_border} !important; color: {sev_text} !important; }}\n"
        )

    alert_css = ""
    for kind, (a_bg, a_text, a_border) in [
        ("Info", alert_info), ("Success", alert_success),
        ("Warning", alert_warning), ("Error", alert_error),
    ]:
        alert_css += (
            f'[data-testid="stAlert"]:has([data-testid="stAlertContent{kind}"]), '
            f'div[data-baseweb="notification"][kind="{kind.lower()}"] {{ '
            f"background: {a_bg} !important; border: 1px solid {a_border} !important; }}\n"
            f'[data-testid="stAlert"]:has([data-testid="stAlertContent{kind}"]) *, '
            f'div[data-baseweb="notification"][kind="{kind.lower()}"] * {{ color: {a_text} !important; }}\n'
        )

    css = f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

    :root, html {{
        color-scheme: {color_scheme};
    }}
    html, body {{
        background-color: {bg_main} !important;
        color: {text_primary} !important;
        font-family: {font_family} !important;
        direction: {rtl_direction} !important;
    }}
    .stApp {{
        background: {page_gradient} !important;
        background-attachment: fixed !important;
        color: {text_primary} !important;
    }}
    [data-testid="stAppViewContainer"], [data-testid="stMain"], section.main {{
        background: transparent !important;
        color: {text_primary} !important;
    }}

    :where([data-testid="stMarkdownContainer"]) :where(p, li, h1, h2, h3, h4, h5, h6, strong, em, span):not([style]) {{
        color: {text_primary} !important;
    }}
    [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] * {{
        color: {text_primary} !important;
        font-weight: 600 !important;
    }}
    hr {{
        border-color: {border_color} !important;
    }}

    /* ITEM 1: FIX MAP PREVIEW EXPANDER BAR TURNING BLACK WHEN NOT HOVERED */
    [data-testid="stExpander"],
    details[data-testid="stExpander"] {{
        background-color: {widget_bg} !important;
        border: 1px solid {widget_border} !important;
        border-radius: 14px !important;
        overflow: hidden !important;
    }}
    [data-testid="stExpander"] summary,
    details[data-testid="stExpander"] > summary,
    details[data-testid="stExpander"] summary:not(:hover),
    details[data-testid="stExpander"] summary:hover,
    details[data-testid="stExpander"] summary:focus,
    details[data-testid="stExpander"] summary:active {{
        background-color: {bg_card_secondary} !important;
        background: {bg_card_secondary} !important;
        color: {text_primary} !important;
        border-radius: 12px !important;
    }}
    [data-testid="stExpander"] summary *,
    details[data-testid="stExpander"] summary *,
    details[data-testid="stExpander"] summary p,
    details[data-testid="stExpander"] summary span {{
        color: {text_primary} !important;
        -webkit-text-fill-color: {text_primary} !important;
    }}
    [data-testid="stExpander"] summary svg,
    details[data-testid="stExpander"] summary svg {{
        fill: {text_primary} !important;
        stroke: {text_primary} !important;
    }}

    /* ============ STREAMLIT TOP HEADER / TOOLBAR ============ */
    header[data-testid="stHeader"], [data-testid="stHeader"] {{
        background: {header_bg} !important;
        backdrop-filter: blur(8px);
        border-bottom: 1px solid {border_color};
    }}
    [data-testid="stHeader"] *, [data-testid="stToolbar"] * {{
        color: {text_primary} !important;
    }}
    [data-testid="stToolbar"] {{
        background: transparent !important;
    }}
    [data-testid="stDecoration"] {{
        display: none !important;
    }}

    /* REMOVE DEPLOY BUTTON ENTIRELY */
    [data-testid="stAppDeployButton"],
    .stAppDeployButton,
    header [data-testid="stAppDeployButton"],
    button[data-testid="stAppDeployButton"] {{
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
        width: 0 !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
    }}

    /* SIMPLIFY 3-DOT MENU: dividers hidden, items are filtered by the script below (only Clear cache remains) */
    [data-testid="stMainMenuPopover"] hr,
    [data-testid="stMainMenuPopover"] [role="separator"],
    ul[data-testid="main-menu-list"] hr {{
        display: none !important;
    }}

    .block-container {{
        padding-top: 4rem !important;
        padding-bottom: 3rem !important;
        max-width: 1400px;
    }}

    /* ============ SIDEBAR ============ */
    [data-testid="stSidebar"] {{
        background: {sidebar_bg} !important;
        border-right: 1px solid {border_color} !important;
    }}
    [data-testid="stSidebar"] [data-testid="stSidebarHeader"] {{
        background: transparent !important;
    }}
    [data-testid="stSidebar"] * {{
        color: {text_primary};
    }}
    [data-testid="stSidebar"] .sidebar-brand-title {{
        color: {text_primary} !important;
        font-size: 20px;
        font-weight: 800;
        margin: 0;
        line-height: 1.2;
    }}
    [data-testid="stSidebar"] .sidebar-brand-subtitle {{
        color: {text_secondary} !important;
        font-size: 11px;
        margin: 0;
        font-weight: 600;
        letter-spacing: 0.5px;
    }}

    [data-testid="stSidebar"] .stButton button {{
        width: 100% !important;
        border-radius: 12px !important;
        padding: 12px 16px !important;
        font-weight: 600 !important;
        font-size: 15px !important;
        text-align: start !important;
        justify-content: flex-start !important;
        border: 1px solid transparent !important;
        background: transparent !important;
        box-shadow: none !important;
        color: {text_primary} !important;
        transition: all 0.2s ease !important;
        margin-bottom: 4px !important;
    }}
    [data-testid="stSidebar"] .stButton button:hover {{
        background: {bg_card_secondary} !important;
        border-color: {border_color} !important;
        transform: none !important;
    }}
    [data-testid="stSidebar"] .stButton button p {{
        color: {text_primary} !important;
    }}
    [data-testid="stSidebar"] .stButton button[kind="primary"],
    [data-testid="stSidebar"] .stButton button[data-testid="stBaseButton-primary"] {{
        background: {nav_active_bg} !important;
        color: #FFFFFF !important;
        font-weight: 700 !important;
        box-shadow: 0 4px 14px rgba(5, 150, 105, 0.35) !important;
    }}
    [data-testid="stSidebar"] .stButton button[kind="primary"] p,
    [data-testid="stSidebar"] .stButton button[data-testid="stBaseButton-primary"] p {{
        color: #FFFFFF !important;
    }}

    /* ============ TOP APP BAR ============ */
    .top-app-bar {{
        background: linear-gradient(135deg, #0F172A 0%, #12305A 55%, #064E3B 100%);
        color: #FFFFFF;
        padding: 18px 24px;
        border-radius: 16px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 24px;
        box-shadow: 0 10px 25px -5px rgba(15, 23, 42, 0.25);
        border-left: 6px solid #10B981;
    }}
    .top-app-bar-brand {{
        display: flex;
        align-items: center;
        gap: 14px;
    }}
    .top-app-bar .top-app-bar-title {{
        font-size: 24px;
        font-weight: 800;
        line-height: 1.2;
        margin: 0;
        padding: 0;
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
        letter-spacing: -0.5px;
    }}
    .top-app-bar .top-app-bar-subtitle {{
        font-size: 13px;
        margin: 2px 0 0 0;
        color: #CBD5E1 !important;
        -webkit-text-fill-color: #CBD5E1 !important;
    }}
    .top-app-bar-page-badge {{
        background: rgba(16, 185, 129, 0.2);
        border: 1px solid rgba(16, 185, 129, 0.5);
        color: #6EE7B7 !important;
        padding: 6px 14px;
        border-radius: 20px;
        font-weight: 700;
        font-size: 13px;
    }}

    /* ============ CARDS ============ */
    .agri-card {{
        background: {bg_card};
        color: {text_primary};
        border: 1px solid {border_color};
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03);
        margin-bottom: 16px;
        position: relative;
        overflow: hidden;
    }}
    .agri-card::before {{
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 4px;
        background: linear-gradient(90deg, #10B981 0%, #0EA5E9 100%);
    }}
    .agri-card :where(h4, strong, span, div, p, code):not([style*="color"]) {{
        color: inherit !important;
    }}
    .metric-value {{
        font-size: 32px;
        font-weight: 800;
        color: {accent_green};
        margin-top: 4px;
        margin-bottom: 2px;
    }}
    .agri-card .metric-label {{
        font-size: 13px;
        font-weight: 700;
        color: {text_secondary} !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}

    /* ============ HERO BANNER ============ */
    .hero-banner {{
        background: linear-gradient(135deg, #064E3B 0%, #047857 35%, #0F766E 65%, #12305A 100%);
        border-radius: 20px;
        padding: 40px;
        color: #FFFFFF;
        position: relative;
        overflow: hidden;
        margin-bottom: 28px;
        border: 1px solid rgba(52, 211, 153, 0.3);
        box-shadow: 0 20px 25px -5px rgba(6, 78, 59, 0.35);
    }}
    .hero-banner::before {{
        content: "";
        position: absolute;
        inset: 0;
        background: repeating-linear-gradient(115deg, rgba(255, 255, 255, 0.05) 0px, rgba(255, 255, 255, 0.05) 2px, transparent 2px, transparent 22px);
        pointer-events: none;
    }}
    .hero-banner > * {{
        position: relative;
        z-index: 1;
    }}
    .hero-banner .hero-title {{
        font-size: 34px;
        font-weight: 900;
        line-height: 1.2;
        margin-bottom: 12px;
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
    }}
    .hero-banner .hero-desc {{
        font-size: 16px;
        color: #E2E8F0 !important;
        -webkit-text-fill-color: #E2E8F0 !important;
        max-width: 800px;
        line-height: 1.6;
        margin-bottom: 20px;
    }}
    .hero-tag-pill {{
        display: inline-block;
        background: rgba(16, 185, 129, 0.25);
        border: 1px solid #10B981;
        color: #A7F3D0 !important;
        -webkit-text-fill-color: #A7F3D0 !important;
        padding: 5px 14px;
        border-radius: 12px;
        font-size: 12px;
        font-weight: 700;
        margin-right: 8px;
        margin-bottom: 8px;
    }}

    /* ============ HORIZONTAL SCROLL HINT (shown on narrow screens only) ============ */
    .scroll-hint {{
        display: none;
        text-align: right;
        font-size: 12px;
        font-weight: 700;
        color: #10B981;
        margin: 10px 4px -6px 0;
    }}
    .scroll-hint .scroll-arrow {{
        display: inline-block;
        animation: scrollNudge 1.2s ease-in-out infinite;
    }}
    @keyframes scrollNudge {{
        0%, 100% {{ transform: translateX(0); }}
        50% {{ transform: translateX(6px); }}
    }}
    @media (max-width: 1100px) {{
        .scroll-hint {{ display: block; }}
    }}

    /* ============ CUSTOM HTML TABLES ============ */
    .agri-table-container {{
        width: 100%;
        overflow-x: auto;
        border-radius: 14px;
        border: 1px solid {border_color};
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.04);
        margin-top: 12px;
        margin-bottom: 24px;
    }}
    table.agri-table {{
        width: 100%;
        border-collapse: collapse;
        background-color: {table_bg};
        color: {text_primary};
        font-size: 14px;
        text-align: start;
    }}
    table.agri-table th {{
        background: {table_head_gradient};
        color: {text_primary} !important;
        font-weight: 700;
        padding: 14px 18px;
        border-bottom: 2px solid {border_color};
        text-transform: uppercase;
        font-size: 12px;
        letter-spacing: 0.5px;
        text-align: start;
    }}
    table.agri-table td {{
        padding: 14px 18px;
        border-bottom: 1px solid {border_color};
        color: {text_primary};
    }}
    table.agri-table td strong {{
        color: {text_primary} !important;
    }}
    table.agri-table code,
    [data-testid="stMarkdownContainer"] code {{
        background: {code_bg} !important;
        color: {code_text} !important;
        padding: 2px 8px;
        border-radius: 6px;
        font-size: 13px;
    }}
    table.agri-table tr:nth-child(even) td {{
        background-color: {table_row_alt};
    }}
    table.agri-table tr:hover td {{
        background-color: {bg_card_secondary};
    }}

    /* ============ PROBLEM DETECTION CARDS + ZONE MATRIX TILES ============ */
    .zone-card, .zone-tile {{
        border-radius: 16px;
        border-width: 2px;
        border-style: solid;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.08);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }}
    .zone-card {{
        padding: 20px;
        margin-bottom: 16px;
    }}
    .zone-tile {{
        padding: 16px;
        text-align: center;
    }}
    .zone-card:hover, .zone-tile:hover {{
        transform: translateY(-2px);
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.15);
    }}
    .zone-card *, .zone-tile * {{
        color: inherit !important;
    }}
    {sev_css}

    /* ============ EMPTY STATE ============ */
    .empty-state-box {{
        background: {bg_card};
        border: 2px dashed {border_color};
        border-radius: 20px;
        padding: 48px;
        text-align: center;
        margin: 32px 0;
    }}
    .empty-state-icon {{
        font-size: 56px;
        margin-bottom: 16px;
    }}
    .empty-state-box .empty-state-title {{
        font-size: 22px;
        font-weight: 700;
        color: {text_primary} !important;
        margin-bottom: 8px;
    }}
    .empty-state-box .empty-state-desc {{
        font-size: 15px;
        color: {text_secondary} !important;
        max-width: 500px;
        margin: 0 auto 24px auto;
    }}

    /* Buttons */
    .stButton button, .stDownloadButton button, button[kind="primary"] {{
        background: {btn_gradient} !important;
        border: 1px solid #059669 !important;
        color: #FFFFFF !important;
        font-weight: 700 !important;
        border-radius: 12px !important;
        padding: 10px 24px !important;
        box-shadow: 0 4px 12px rgba(5, 150, 105, 0.3) !important;
        transition: all 0.2s ease !important;
    }}
    .stButton button p, .stDownloadButton button p {{
        color: inherit !important;
    }}
    .stButton button:hover, .stDownloadButton button:hover, button[kind="primary"]:hover {{
        background: {btn_hover} !important;
        transform: translateY(-1px) !important;
        box-shadow: 0 6px 16px rgba(5, 150, 105, 0.45) !important;
    }}

    /* UPLOAD BUTTON BACKGROUND COLOR IN LIGHT MODE */
    [data-testid="stFileUploader"] {{
        background-color: {widget_bg} !important;
        border-radius: 16px !important;
        padding: 12px !important;
        border: 1px solid {widget_border} !important;
    }}
    [data-testid="stFileUploader"] section,
    [data-testid="stFileUploaderDropzone"] {{
        background-color: {bg_card_secondary} !important;
        border: 2px dashed {widget_border} !important;
        border-radius: 12px !important;
    }}
    [data-testid="stFileUploader"] section *,
    [data-testid="stFileUploaderDropzone"] * {{
        color: {text_primary} !important;
    }}
    [data-testid="stFileUploader"] button,
    [data-testid="stFileUploaderDropzone"] button,
    [data-testid="stFileUploader"] button[kind="secondary"],
    [data-testid="stFileUploaderDropzone"] button[kind="secondary"],
    [data-testid="stFileUploader"] [data-testid="stBaseButton-secondary"] {{
        background: {btn_gradient} !important;
        background-color: #059669 !important;
        border: 1px solid #059669 !important;
        color: #FFFFFF !important;
        font-weight: 700 !important;
        border-radius: 10px !important;
        box-shadow: 0 4px 12px rgba(5, 150, 105, 0.25) !important;
    }}
    [data-testid="stFileUploader"] button *,
    [data-testid="stFileUploaderDropzone"] button * {{
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
    }}

    /* Selectbox */
    div[data-baseweb="select"] > div {{
        background-color: {widget_bg} !important;
        border: 1px solid {widget_border} !important;
        border-radius: 12px !important;
    }}
    div[data-baseweb="select"] *, div[data-baseweb="select"] input {{
        color: {text_primary} !important;
    }}
    div[data-baseweb="popover"], div[data-baseweb="popover"] > div,
    div[data-baseweb="menu"], ul[role="listbox"] {{
        background-color: {widget_bg} !important;
        border: 1px solid {widget_border} !important;
        border-radius: 12px !important;
    }}
    li[role="option"] {{
        background-color: {widget_bg} !important;
        color: {text_primary} !important;
    }}

    /* Inputs */
    input[type="text"], input[type="number"], input[type="password"], textarea {{
        background-color: {widget_bg} !important;
        color: {text_primary} !important;
        -webkit-text-fill-color: {text_primary} !important;
        border-radius: 10px !important;
    }}

    [data-testid="stAlert"], div[data-testid="stNotification"] {{
        border-radius: 14px !important;
    }}
    {alert_css}
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)

inject_custom_styles(theme, lang)

video_and_menu_handler_js = """
<script>
function applyVideoAndMenuRules() {
    try {
        const doc = window.parent.document;
        
        const videos = doc.querySelectorAll('video');
        videos.forEach(vid => {
            vid.setAttribute('controlsList', 'noplaybackrate');
            if (vid.controlsList && !vid.controlsList.contains('noplaybackrate')) {
                vid.controlsList.add('noplaybackrate');
            }
        });

        doc.querySelectorAll('[data-testid="stAppDeployButton"], .stAppDeployButton').forEach(btn => {
            btn.style.setProperty('display', 'none', 'important');
        });
        doc.querySelectorAll('header button, [data-testid="stToolbar"] button').forEach(btn => {
            const txt = (btn.innerText || btn.textContent || "").trim().toLowerCase();
            if (txt === "deploy") {
                btn.style.setProperty('display', 'none', 'important');
            }
        });

        const menuItems = doc.querySelectorAll('[data-testid="main-menu-list"] li, [data-testid="stMainMenuPopover"] li');
        menuItems.forEach(item => {
            const label = (item.innerText || item.textContent || "").toLowerCase().trim();
            if (!label.includes("clear cache")) {
                item.style.setProperty('display', 'none', 'important');
            }
        });
    } catch(e) {}
}

setInterval(applyVideoAndMenuRules, 250);
document.addEventListener("DOMContentLoaded", applyVideoAndMenuRules);
</script>
"""
components.html(flat(video_and_menu_handler_js), height=0)


# ==================================================================
# CUSTOM TABLE RENDERER
# ==================================================================
def render_custom_table(headers, rows):
    header_html = "".join([f"<th>{h}</th>" for h in headers])
    rows_html = ""
    for row in rows:
        cells = "".join([f"<td>{cell}</td>" for cell in row])
        rows_html += f"<tr>{cells}</tr>"
        
    table_code = f"""
    <div class="scroll-hint">Swipe sideways to see more <span class="scroll-arrow">➜</span></div>
    <div class="agri-table-container">
        <table class="agri-table">
            <thead>
                <tr>{header_html}</tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
    """
    st.markdown(flat(table_code), unsafe_allow_html=True)


def sanitize_problem_description(raw_desc, severity):
    if severity == "none" or (raw_desc or "").strip().lower() in ["none", "none.", "no issue", "null", ""]:
        return t["no_issue"]
    return raw_desc


SEVERITY_ALIASES = {"moderate": "medium", "severe": "high", "critical": "high", "healthy": "none"}

def severity_class(severity):
    sev = str(severity).strip().lower()
    sev = SEVERITY_ALIASES.get(sev, sev)
    return sev if sev in ("none", "low", "medium", "high") else "none"

def severity_label(severity):
    return t[f"sev_{severity_class(severity)}"]


# ==================================================================
# PIPELINE ADAPTER FUNCTION
# ==================================================================
def format_pipeline_output(raw_report, class_tally, output_video_path, input_video_path):
    meta = raw_report.get("_meta", {})
    zones = []
    zone_index = 1
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            key = f"zone_{r}_{c}"
            zdata = raw_report.get(key, {})
            
            sev = zdata.get("sustained_severity", "none")
            spray_pct = zdata.get("recommended_spray_dose_percent", 0)
            spray_needed = zdata.get("sprayed", spray_pct > 0)
            dose_l_ha = float(spray_pct)
            
            z_breakdown = zdata.get("zone_class_breakdown", {})
            detected_classes = [cls for cls, cnt in z_breakdown.items() if cnt > 0]
            
            dom = zdata.get("dominant_pest_class")
            if dom and dom not in detected_classes and str(dom).strip().lower() not in ["none", "null", ""]:
                detected_classes.append(dom)
                
            disc_ratio = zdata.get("p95_discoloration_ratio", 0.0)
            yolo_ratio = zdata.get("p95_yolo_coverage_ratio", 0.0)
            comb_ratio = zdata.get("p95_combined_severity_ratio", 0.0)
            affected_canopy_pct = max(disc_ratio, yolo_ratio, comb_ratio) * 100.0
            
            if dom and dom in PEST_ACCESSIBLE_INFO:
                chemical = PEST_ACCESSIBLE_INFO[dom].get("treatment", "Targeted Pesticide")
            elif detected_classes:
                chemical = "Broad-Spectrum Crop Protection Spray"
            elif sev != "none":
                chemical = "Foliar Fertilizer & Fungicide"
            else:
                chemical = "N/A"
                
            if sev == "none":
                problem_desc = t["no_issue"]
            else:
                symptoms = []
                if detected_classes:
                    symptoms.append(f"Pests: {', '.join(detected_classes)}")
                if zdata.get("p95_chlorosis_ratio", 0.0) > 0.05:
                    symptoms.append(f"Chlorosis yellowing ({zdata.get('p95_chlorosis_ratio', 0.0)*100:.1f}%)")
                if zdata.get("p95_necrosis_ratio", 0.0) > 0.05:
                    symptoms.append(f"Necrotic spots ({zdata.get('p95_necrosis_ratio', 0.0)*100:.1f}%)")
                if zdata.get("p95_wilting_score", 0.0) > 0.05:
                    symptoms.append("Canopy wilting stress")
                if not symptoms:
                    symptoms.append(f"Elevated crop stress signals")
                problem_desc = f"{sev.upper()} severity. " + "; ".join(symptoms) + "."

            zones.append({
                "zone_id": zone_index,
                "row": r,
                "col": c,
                "severity": sev,
                "spray_needed": spray_needed,
                "spray_dose_percent": spray_pct,
                "dose_liters_per_ha": dose_l_ha,
                "recommended_chemical": chemical,
                "detected_classes": detected_classes,
                "affected_canopy_pct": round(affected_canopy_pct, 1),
                "problem_description": problem_desc,
                "latitude": zdata.get("latitude"),
                "longitude": zdata.get("longitude"),
                "observed_frames": zdata.get("observed_frames", 0),
                "raw": zdata
            })
            zone_index += 1

    return {
        "zones": zones,
        "annotated_video_path": output_video_path,
        "class_tally": class_tally,
        "_gis": raw_report.get("_gis", {}),
        "metadata": {
            "filename": os.path.basename(input_video_path),
            "total_frames_analyzed": meta.get("frames_analyzed", "Completed"),
            "fps": meta.get("output_fps", 20),
            "ground_fixed_zones": meta.get("ground_fixed_zones", False),
            "motion_tracking_failures": meta.get("motion_tracking_failures", 0),
            "assumed_altitude_m": meta.get("assumed_altitude_m"),
        }
    }


# ==================================================================
# NAVIGATION DRAWER (SIDEBAR)
# ==================================================================
with st.sidebar:
    st.markdown(
        f"""
        <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid {'#334155' if theme == 'dark' else '#E2E8F0'};">
            <span style="font-size: 32px;">🌾</span>
            <div>
                <div class="sidebar-brand-title">AgriVision</div>
                <div class="sidebar-brand-subtitle">PRECISION FARMING SYSTEM</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
    
    pages = [
        ("home", t["nav_home"]),
        ("visual_report", t["nav_visual"]),
        ("gps", t["nav_gps"]),
        ("problems", t["nav_problems"]),
        ("spray", t["nav_spray"]),
        ("pests", t["nav_pests"]),
        ("settings", t["nav_settings"]),
    ]
    
    for page_id, page_name in pages:
        is_active = (st.session_state.current_page == page_id)
        if st.button(
            page_name,
            key=f"nav_btn_{page_id}",
            type="primary" if is_active else "secondary",
            use_container_width=True,
        ):
            st.session_state.current_page = page_id
            st.rerun()
            
    st.markdown("---")
    
    if st.session_state.report is not None:
        rep = st.session_state.report
        problem_count = sum(1 for z in rep["zones"] if z["severity"] != "none")
        st.markdown(
            f"""
            <div style="background: {'rgba(16,185,129,0.1)' if problem_count == 0 else 'rgba(239,68,68,0.1)'}; 
                        border: 1px solid {'#10B981' if problem_count == 0 else '#EF4444'}; 
                        border-radius: 12px; padding: 12px; font-size: 12px;">
                <strong style="color: {'#10B981' if problem_count == 0 else '#EF4444'};">
                    {'✅ Field Healthy' if problem_count == 0 else f'⚠️ {problem_count} Problem Zones'}
                </strong>
                <p style="margin: 4px 0 0 0; font-size: 11px; opacity: 0.8;">
                    Processed: {rep.get('metadata', {}).get('filename', 'Video')}
                </p>
            </div>
            """,
            unsafe_allow_html=True
        )


# ==================================================================
# TOP APP BAR
# ==================================================================
page_labels = {
    "home": t["nav_home"],
    "visual_report": t["nav_visual"],
    "gps": t["nav_gps"],
    "problems": t["nav_problems"],
    "spray": t["nav_spray"],
    "pests": t["nav_pests"],
    "settings": t["nav_settings"]
}
current_label = page_labels.get(st.session_state.current_page, "AgriVision")

st.markdown(
    f"""
    <div class="top-app-bar">
        <div class="top-app-bar-brand">
            <div>
                <div class="top-app-bar-title">{t["app_title"]}</div>
                <div class="top-app-bar-subtitle">{t["app_subtitle"]}</div>
            </div>
        </div>
        <div class="top-app-bar-page-badge">
            {current_label}
        </div>
    </div>
    """,
    unsafe_allow_html=True
)


def render_empty_state():
    st.markdown(
        f"""
        <div class="empty-state-box">
            <div class="empty-state-icon">🛰️</div>
            <div class="empty-state-title">{t["empty_title"]}</div>
            <div class="empty-state-desc">{t["empty_desc"]}</div>
        </div>
        """,
        unsafe_allow_html=True
    )
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button(t["btn_go_home"], type="primary", use_container_width=True, key="empty_go_home"):
            st.session_state.current_page = "home"
            st.rerun()


# ==================================================================
# PAGE 1: 🏠 HOME / OVERVIEW
# ==================================================================
if st.session_state.current_page == "home":
    st.markdown(
        f"""
        <div class="hero-banner">
            <div>
                <span class="hero-tag-pill">🛰️ AI DRONE MONITORING</span>
                <span class="hero-tag-pill">🌱 YOLO V8 + HSV ANALYSIS</span>
                <span class="hero-tag-pill">💦 PRECISION DOSAGE</span>
            </div>
            <div class="hero-title">{t["hero_title"]}</div>
            <div class="hero-desc">{t["hero_desc"]}</div>
        </div>
        """,
        unsafe_allow_html=True
    )
    
    if st.session_state.report is not None:
        rep = st.session_state.report
        zones = rep["zones"]
        total_zones = len(zones)
        problem_zones = sum(1 for z in zones if z["severity"] != "none")
        spray_zones = sum(1 for z in zones if z["spray_needed"])
        health_str = t["status_healthy"] if problem_zones == 0 else t["status_action_needed"]
        
        mcol1, mcol2, mcol3, mcol4 = st.columns(4)
        with mcol1:
            st.markdown(
                f"""
                <div class="agri-card">
                    <div class="metric-label">{t["metric_total_zones"]}</div>
                    <div class="metric-value" style="color: #3B82F6;">{total_zones}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with mcol2:
            st.markdown(
                f"""
                <div class="agri-card">
                    <div class="metric-label">{t["metric_problem_zones"]}</div>
                    <div class="metric-value" style="color: {'#10B981' if problem_zones == 0 else '#EF4444'};">{problem_zones}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with mcol3:
            st.markdown(
                f"""
                <div class="agri-card">
                    <div class="metric-label">{t["metric_spray_zones"]}</div>
                    <div class="metric-value" style="color: {'#10B981' if spray_zones == 0 else '#F59E0B'};">{spray_zones}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with mcol4:
            st.markdown(
                f"""
                <div class="agri-card">
                    <div class="metric-label">{t["metric_health_status"]}</div>
                    <div style="font-size: 18px; font-weight: 700; margin-top: 10px; color: {'#10B981' if problem_zones == 0 else '#EF4444'};">{health_str}</div>
                </div>
                """,
                unsafe_allow_html=True
            )

    st.markdown(f"### {t['upload_heading']}")
    
    col_up, col_demo = st.columns([2, 1])
    
    project_dir = Path(__file__).parent
    demo_files = []
    test_video_dir = project_dir / "test_videos"
    search_dirs = [test_video_dir, project_dir]
    for sd in search_dirs:
        if sd.exists():
            for f in sd.glob("*.mp4"):
                if "annotated" not in f.name.lower() and "output" not in f.name.lower():
                    demo_files.append(str(f))
                
    uploaded_file = None
    selected_demo = None
    
    with col_up:
        uploaded_file = st.file_uploader(t["upload_label"], type=["mp4", "avi", "mov"])
        
    with col_demo:
        if demo_files:
            selected_demo = st.selectbox(t["demo_select_label"], ["-- Select Demo --"] + demo_files)
        else:
            st.info("No demo videos found in project directory.")
            
    target_video_path = None
    
    if uploaded_file is not None:
        tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tfile.write(uploaded_file.read())
        target_video_path = tfile.name
        st.video(target_video_path)
    elif selected_demo and selected_demo != "-- Select Demo --":
        target_video_path = selected_demo
        st.video(target_video_path)

    st.markdown("<br>", unsafe_allow_html=True)
    
    if target_video_path:
        if st.button(t["btn_run_pipeline"], type="primary", use_container_width=True):
            progress_bar = st.progress(0)
            status_text = st.empty()
            status_text.info(t["processing_msg"])
            
            def progress_cb(current, total):
                if total > 0:
                    pct = min(1.0, current / total)
                    progress_bar.progress(pct)
                    status_text.text(f"Processing frame {current}/{total} ({int(pct*100)}%)...")

            try:
                output_dir = os.path.join(os.path.dirname(__file__), "outputs")
                os.makedirs(output_dir, exist_ok=True)
                out_vid_path = os.path.join(output_dir, "annotated_video.mp4")
                rep_json_path = os.path.join(output_dir, "spray_report.json")
                
                raw_report, class_tally, final_out_vid, model_warnings = process_video(
                    video_path=target_video_path,
                    output_video=out_vid_path,
                    report_path=rep_json_path,
                    progress_callback=progress_cb,
                    outer_boundary_coords=st.session_state.field_boundary
                )
                
                formatted_report = format_pipeline_output(raw_report, class_tally, final_out_vid, target_video_path)
                st.session_state.report = formatted_report
                if st.session_state.get("boundary_is_default", True):
                    # no outer boundary drawn yet: keep the old automatic behaviour
                    if not generate_boundary_and_geofence_from_zones(formatted_report["zones"]):
                        fit_geofence_to_zones(formatted_report["zones"])
                else:
                    # the user drew the outer boundary: keep it as the geofence
                    fit_geofence_to_boundary(st.session_state.field_boundary)
                sync_zones_to_boundary()
                st.session_state.sub_area_sprays = {}
                
                progress_bar.progress(1.0)
                status_text.success("✅ Detection pipeline completed successfully.")
                progress_bar.empty()
            except Exception as e:
                status_text.empty()
                progress_bar.empty()
                st.error(f"Error running pipeline: {str(e)}")
                
    if st.session_state.report is not None:
        st.markdown("---")
        st.markdown(
            f"""
            <div style="background: rgba(16,185,129,0.1); border: 1px solid #10B981; border-radius: 16px; padding: 20px; display: flex; align-items: center; justify-content: space-between;">
                <div>
                    <h3 style="margin: 0; color: #10B981; font-weight: 700;">Detection Report Ready</h3>
                    <p style="margin: 4px 0 0 0; opacity: 0.8;">Explore annotated video, GPS geofencing, problem areas, and spray advisory across navigation tabs.</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
        if st.button(t["view_full_report"], type="primary"):
            st.session_state.current_page = "visual_report"
            st.rerun()


# ==================================================================
# PAGE 2: 📊 VISUAL REPORT
# ==================================================================
elif st.session_state.current_page == "visual_report":
    if st.session_state.report is None:
        render_empty_state()
    else:
        rep = st.session_state.report
        st.markdown("### 📊 Annotated Video & Field Grid Matrix")
        
        c_vid, c_grid = st.columns([3, 2])
        
        with c_vid:
            ann_path = rep.get("annotated_video_path")
            if ann_path and os.path.exists(ann_path):
                st.video(ann_path)
                with open(ann_path, "rb") as f:
                    st.download_button(
                        label="⬇️ Download Annotated Video (.mp4)",
                        data=f.read(),
                        file_name=os.path.basename(ann_path),
                        mime="video/mp4",
                        use_container_width=True
                    )
            else:
                st.warning("Annotated video file not found on disk.")
                
        with c_grid:
            st.markdown("#### 3x3 Field Zone Severity Matrix")
            zones = rep["zones"]
            
            tiles = []
            for z in zones:
                zid = z["zone_id"]
                sev = z["severity"]
                pest_list = ", ".join(z.get("detected_classes", [])) or t["no_pests"]

                tiles.append(f"""
                <div class="zone-tile severity-{severity_class(sev)}">
                    <div style="font-size: 12px; font-weight: 700; opacity: 0.9;">ZONE {esc(zid)}</div>
                    <div style="font-size: 16px; font-weight: 800; text-transform: uppercase; margin: 4px 0;">{severity_label(sev)}</div>
                    <div title="{esc(pest_list)}" style="font-size: 11px; opacity: 0.95; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{esc(pest_list)}</div>
                </div>
                """)

            grid_html = (
                "<div style='display: grid; grid-template-columns: repeat(3, 1fr); "
                "gap: 10px; margin-top: 10px;'>" + "".join(tiles) + "</div>"
            )
            st.markdown(flat(grid_html), unsafe_allow_html=True)
            
            st.markdown("<br>", unsafe_allow_html=True)
            meta_info = rep.get('metadata', {})
            st.info(f"💡 **Total Frames Analyzed:** {meta_info.get('total_frames_analyzed', 'N/A')} | **FPS:** {meta_info.get('fps', 'N/A')}")
            if meta_info.get("ground_fixed_zones"):
                st.caption("Ground-fixed zones: Z1-Z9 always refer to the same part of the field, even while the drone moves.")
            if meta_info.get("motion_tracking_failures", 0) > 0:
                st.warning(f"Drone motion could not be tracked in {meta_info['motion_tracking_failures']} frame(s) "
                           "(blur or low texture); the previous motion was reused for those frames.")


# ==================================================================
# PAGE 3: 📍 FIELD MAP & GPS COORDINATES
# OpenStreetMap + Folium + streamlit-folium + LocationIQ Geocoder
# ==================================================================
elif st.session_state.current_page == "gps":
    sync_zones_to_boundary()
    st.markdown("### 📍 Field Map & GPS Coordinates")
    st.write("Comprehensive farm telemetry, field perimeter definition, target ground mapping, and sub-area spray monitoring.")
    
    c_lat = st.session_state.geofence_center_lat
    c_lon = st.session_state.geofence_center_lon
    radius = st.session_state.geofence_radius_m

    # 1. LOCATION SEARCH BAR WITH TRAILING SEARCH ICON
    scol1, scol2 = st.columns([0.88, 0.12])
    with scol1:
        search_query = st.text_input(
            "Search Location",
            placeholder="Search address or location (e.g. Islamabad, Bahria Town, 33.5673, 73.1015)...",
            label_visibility="collapsed",
            key="map_search_input"
        )
    with scol2:
        do_search = st.button("🔍", key="search_btn_icon", use_container_width=True, help="Search Location")

    just_searched = False
    trigger_search = do_search or (search_query and search_query.strip() != st.session_state.get("last_search_query", ""))
    
    if trigger_search and search_query and search_query.strip():
        st.session_state["last_search_query"] = search_query.strip()
        s_lat, s_lon, s_addr, status = geocode_location(search_query.strip())
        
        if status == "success" and s_lat is not None and s_lon is not None:
            just_searched = True
            s_lat_r, s_lon_r = round(float(s_lat), 6), round(float(s_lon), 6)

            st.session_state.map_center = [s_lat, s_lon]
            st.session_state.map_zoom = 17
            st.session_state.search_marker = (s_lat, s_lon, s_addr)

            # Rebuild the map component from scratch: its old remembered click / drawing
            # is what kept overwriting the searched coordinates with the same old point.
            st.session_state.map_version += 1
            st.session_state["last_click_pt"] = None
            st.session_state["last_draw_sig"] = None
            st.session_state.drawn_polygon = None

            st.success(f"📍 Map navigated to: {s_addr} ({s_lat_r:.6f}, {s_lon_r:.6f})")
        elif status == "error":
            st.error(f"⚠️ {s_addr}")
        elif status == "network_error":
            st.error("⚠️ Geocoding service is temporarily unreachable. Please check your internet connection or enter direct coordinates (e.g. 33.5673, 73.1015).")
        else:
            st.error(f"❌ Location '{search_query.strip()}' could not be found. Please check spelling or enter direct coordinates (e.g. 33.5673, 73.1015).")

    # 2. CREATE FOLIUM MAP WITH SATELLITE / OPENSTREETMAP TILES
    # Derive a safe map center: prefer field boundary centroid, then stored geofence center, then world origin
    _boundary = st.session_state.field_boundary
    if _boundary and len(_boundary) >= 1:
        _safe_lat = sum(p["lat"] for p in _boundary) / len(_boundary)
        _safe_lon = sum(p["lon"] for p in _boundary) / len(_boundary)
    elif c_lat is not None and c_lon is not None:
        _safe_lat, _safe_lon = c_lat, c_lon
    else:
        _safe_lat, _safe_lon = 30.0, 70.0  # generic fallback — will be overridden by user boundary

    map_center = st.session_state.get("map_center", [_safe_lat, _safe_lon])
    # Guard against None in map_center (e.g., loaded from old session state)
    if not map_center or map_center[0] is None or map_center[1] is None:
        map_center = [_safe_lat, _safe_lon]
    map_zoom = st.session_state.get("map_zoom", 14)
    
    try:
        maptiler_key = st.secrets["MAPTILER_KEY"]
    except Exception:
        maptiler_key = None

    if maptiler_key:
        m = folium.Map(
            location=map_center,
            zoom_start=map_zoom,
            tiles=None,
            max_zoom=20,
            control_scale=True
        )
        folium.TileLayer(
            tiles="https://api.maptiler.com/tiles/satellite-v2/{z}/{x}/{y}.jpg?key=" + maptiler_key,
            attr="&copy; MapTiler &copy; OpenStreetMap contributors",
            name="Satellite",
            max_zoom=20
        ).add_to(m)
    else:
        m = folium.Map(
            location=map_center,
            zoom_start=map_zoom,
            tiles="OpenStreetMap",
            control_scale=True
        )
        st.warning("MAPTILER_KEY not found in .streamlit/secrets.toml, showing the default OpenStreetMap layer.")

    # Draw Field Outer Perimeter Polygon
    poly_coords = [[pt["lat"], pt["lon"]] for pt in st.session_state.field_boundary]
    if len(poly_coords) >= 3:
        folium.Polygon(
            locations=poly_coords,
            color="#10B981",
            fill=True,
            fill_color="#34D399",
            fill_opacity=0.25,
            weight=3,
            popup="🌿 Outer Field Boundary Perimeter"
        ).add_to(m)

    # Draw Boundary Vertices Marker Circles
    for idx, pt in enumerate(st.session_state.field_boundary):
        folium.CircleMarker(
            location=[pt["lat"], pt["lon"]],
            radius=6,
            color="#047857",
            fill=True,
            fill_color="#6EE7B7",
            fill_opacity=1.0,
            popup=f"Boundary Vertex #{idx+1}: {pt.get('name', 'Point')} ({pt['lat']:.6f}, {pt['lon']:.6f})"
        ).add_to(m)

    # Draw Marked Ground Target Areas
    for g in st.session_state.marked_ground_areas:
        folium.Marker(
            location=[g["lat"], g["lon"]],
            popup=f"<b>📍 Target Ground Area: {g['label']}</b><br>{g['notes']}<br><code>{g['lat']:.6f}, {g['lon']:.6f}</code>",
            icon=folium.Icon(color="orange", icon="info-sign")
        ).add_to(m)
        folium.Circle(
            location=[g["lat"], g["lon"]],
            radius=14,
            color="#F59E0B",
            fill=True,
            fill_color="#FBBF24",
            fill_opacity=0.35
        ).add_to(m)

    # Draw Geofence Radius (only when a real center exists)
    if c_lat is not None and c_lon is not None:
        folium.Circle(
            location=[c_lat, c_lon],
            radius=radius,
            color="#3B82F6",
            fill=True,
            fill_color="#60A5FA",
            fill_opacity=0.1,
            dash_array="4, 4",
            popup=f"Geofence Radius: {radius}m"
        ).add_to(m)

    # Draw Searched Location Marker if active
    if st.session_state.get("search_marker"):
        slat, slon, saddr = st.session_state.search_marker
        folium.Marker(
            location=[slat, slon],
            popup=f"<b>🔍 Searched Location:</b><br>{saddr}<br><code>{slat:.6f}, {slon:.6f}</code>",
            icon=folium.Icon(color="green", icon="search")
        ).add_to(m)

    # Zones (3x3) and their 4 sub-areas, drawn inside the outer boundary
    try:
        _grid = zone_grid_polygons(st.session_state.field_boundary, GRID_ROWS, GRID_COLS)
        _zinfo = {}
        if st.session_state.get("report") is not None:
            _zinfo = {(z["row"], z["col"]): z for z in st.session_state.report["zones"]}
        for (_r, _c), _g in _grid.items():
            _z = _zinfo.get((_r, _c))
            _zid = _z["zone_id"] if _z else _r * GRID_COLS + _c + 1
            if _z:
                for _sa, _sa_def in zip(("NW", "NE", "SW", "SE"), get_sub_areas_for_zone(_zid, _z["severity"], _z["spray_needed"])):
                    _pts = _g["subs"].get(_sa)
                    if _pts:
                        folium.Polygon(locations=_pts, color=_sa_def["color"], weight=1, fill=True,
                                       fill_color=_sa_def["color"], fill_opacity=0.35, interactive=False).add_to(m)
            if _g["zone"]:
                folium.Polygon(locations=_g["zone"], color="#FFFFFF", weight=2, fill=False, interactive=False).add_to(m)
                _cy = sum(p[0] for p in _g["zone"]) / len(_g["zone"])
                _cx = sum(p[1] for p in _g["zone"]) / len(_g["zone"])
                folium.Marker(
                    location=[_cy, _cx],
                    icon=folium.DivIcon(html=f'<div style="font-weight:800;font-size:13px;color:#fff;text-shadow:0 0 3px #000;white-space:nowrap;pointer-events:none;transform:translate(-50%,-50%)">Z{_zid}</div>'),
                ).add_to(m)
    except Exception:
        pass  # the zone overlay must never break the map

    # Add Leaflet.draw Plugin for drawing boundary points / ground targets directly on map
    Draw(
        export=False,
        position="topleft",
        draw_options={
            "polyline": False,
            "rectangle": True,
            "polygon": True,
            "circle": False,
            "marker": True,
            "circlemarker": False
        },
        edit_options={"edit": True, "remove": True}
    ).add_to(m)

    # Render Folium Map using streamlit-folium.
    # The key changes after every search, so the component starts fresh (no stale click / drawing).
    map_output = st_folium(
        m,
        width="100%",
        height=440,
        key=f"folium_field_map_{st.session_state.map_version}",
        returned_objects=["last_clicked", "last_active_drawing", "all_drawings", "center", "zoom"]
    )

    # Remember where the user is looking (pan / zoom), so drawing a boundary elsewhere
    # does not send the map back to the searched point after the rerun.
    if map_output:
        _c = map_output.get("center")
        _z = map_output.get("zoom")
        if isinstance(_c, dict) and _c.get("lat") is not None and _c.get("lng") is not None:
            st.session_state.map_center = [round(float(_c["lat"]), 6), round(float(_c["lng"]), 6)]
        if _z:
            st.session_state.map_zoom = int(_z)

    # Exact coordinates of what is drawn on the map become the field boundary and the target points
    if map_output and map_output.get("all_drawings"):
        latest_poly = None
        drawn_pts = []
        for shape in map_output["all_drawings"]:
            geom = (shape or {}).get("geometry") or {}
            if geom.get("type") == "Polygon" and geom.get("coordinates"):
                ring = geom["coordinates"][0]
                if len(ring) > 1 and ring[0] == ring[-1]:
                    ring = ring[:-1]
                if len(ring) >= 3:
                    latest_poly = [[round(float(pt[1]), 6), round(float(pt[0]), 6)] for pt in ring]
            elif geom.get("type") == "Point" and geom.get("coordinates") and len(geom["coordinates"]) >= 2:
                pc = geom["coordinates"]
                drawn_pts.append((round(float(pc[1]), 6), round(float(pc[0]), 6)))

        map_changed = False
        boundary_changed = False
        if latest_poly:
            current_poly = [[p["lat"], p["lon"]] for p in st.session_state.field_boundary]
            if current_poly != latest_poly:
                new_boundary = [{"lat": la, "lon": lo, "name": f"Vertex {i + 1}"} for i, (la, lo) in enumerate(latest_poly)]
                st.session_state.field_boundary = new_boundary
                st.session_state.boundary_is_default = False
                fit_geofence_to_boundary(new_boundary)
                map_changed = True
                boundary_changed = True

        if drawn_pts:
            inside_pts = [p for p in drawn_pts if point_in_polygon(p[0], p[1], st.session_state.field_boundary)]
            current_pts = [(g["lat"], g["lon"]) for g in st.session_state.marked_ground_areas]
            if current_pts != inside_pts:
                st.session_state.marked_ground_areas = [
                    {"id": i + 1, "lat": la, "lon": lo, "label": f"Target Ground Area {i + 1}", "notes": "Marked on map"}
                    for i, (la, lo) in enumerate(inside_pts)
                ]
                st.session_state.targets_are_default = False
                map_changed = True
            if len(drawn_pts) > len(inside_pts):
                st.warning(f"{len(drawn_pts) - len(inside_pts)} marker(s) are outside the field boundary and were not added. Place the marker inside the green shape.")
        elif boundary_changed:
            # no markers drawn: drop old target areas that lie outside the new boundary
            st.session_state.marked_ground_areas = [
                g for g in st.session_state.marked_ground_areas
                if point_in_polygon(g["lat"], g["lon"], st.session_state.field_boundary)
            ]

        if map_changed:
            st.session_state.auto_geo_info = None
            st.rerun()

    if st.session_state.get("auto_geo_info"):
        st.info(st.session_state.auto_geo_info)

    st.markdown("---")
    
    # ==============================================================
    # 1. FIELD BOUNDARY DEFINITION TOOL
    # ==============================================================
    st.markdown("#### 📐 1. Field Boundary Definition (Outer Perimeter)")
    st.write("View and configure boundary perimeter coordinates of the field being monitored.")
    
    boundary_notice = st.session_state.pop("boundary_notice", None)
    if boundary_notice:
        st.success("✅ " + boundary_notice)

    st.markdown("##### Current Outer Perimeter Vertices:")
    boundary_rows = []
    for idx, pt in enumerate(st.session_state.field_boundary):
        boundary_rows.append([
            f"Vertex #{idx + 1}",
            pt.get("name", f"Point {idx+1}"),
            f"<code>{pt['lat']:.6f}</code>",
            f"<code>{pt['lon']:.6f}</code>"
        ])
    render_custom_table(["Vertex", "Label", "Latitude", "Longitude"], boundary_rows)

    st.markdown("---")

    # ==============================================================
    # 2. MAP-BASED GROUND COVERAGE MARKING TOOL
    # ==============================================================
    st.markdown("#### 🎯 2. Map-Based Ground Coverage Marking")
    st.write("Manually mark specific ground areas on the map that require targeted ground coverage.")
    
    st.markdown("##### Marked Ground Target Areas:")
    g_rows = []
    for g in st.session_state.marked_ground_areas:
        g_rows.append([
            f"Target #{g['id']}",
            g["label"],
            f"<code>{g['lat']:.6f}</code>",
            f"<code>{g['lon']:.6f}</code>",
            g["notes"]
        ])
    render_custom_table(["ID", "Target Area Label", "Latitude", "Longitude", "Coverage Notes"], g_rows)

    st.markdown("---")

    # ==============================================================
    # 3. PRECISE SUB-AREA SPRAY DEFINITION & STATUS INDICATOR
    # ==============================================================
    st.markdown("#### 💦 3. Precise Sub-Area Spray Definition & Spray Status Indicator")
    st.write("Define exact sub-areas within each grid requiring spraying and view live spray status indicators.")
    
    selected_zone_num = st.selectbox("Select Grid Zone to Inspect / Define Sub-Areas:", list(range(1, 10)), format_func=lambda x: f"Grid Zone {x}")
    
    zone_sev = "none"
    zone_spray = False
    if st.session_state.report is not None:
        for z in st.session_state.report["zones"]:
            if z["zone_id"] == selected_zone_num:
                zone_sev = z["severity"]
                zone_spray = z["spray_needed"]
                
    sub_areas = get_sub_areas_for_zone(selected_zone_num, zone_sev, zone_spray)
    
    scol1, scol2 = st.columns([1, 1])
    with scol1:
        st.markdown(f"##### Sub-Area Layout Diagram for **Grid Zone {selected_zone_num}**:")
        sub_tiles = []
        for sa in sub_areas:
            sub_tiles.append(f"""
            <div style="background: {sa['color']}22; border: 2px solid {sa['color']}; border-radius: 12px; padding: 14px; text-align: center;">
                <div style="font-size: 11px; font-weight: 700; opacity: 0.8;">{esc(sa['sub_id'])}</div>
                <div style="font-size: 13px; font-weight: 800; margin: 4px 0;">{esc(sa['quadrant'])}</div>
                <span style="font-size: 11px; font-weight: 800; padding: 3px 8px; border-radius: 10px; background: {sa['color']}; color: #FFFFFF;">
                    {esc(sa['badge'])}
                </span>
            </div>
            """)
        
        sub_grid_html = "<div style='display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px;'>" + "".join(sub_tiles) + "</div>"
        st.markdown(flat(sub_grid_html), unsafe_allow_html=True)
        
    with scol2:
        st.markdown("##### Update Sub-Area Spray Status:")
        sub_target = st.selectbox("Select Target Sub-Area:", [sa["sub_id"] for sa in sub_areas])
        new_status = st.selectbox("Set Live Spray Status:", [
            "🟡 Spraying In Progress",
            "🟢 Sprayed (Completed)",
            "🔴 Spray Required (Pending)",
            "⚪ Healthy Zone"
        ])
        
        if st.button("💾 Update Sub-Area Status", type="primary", use_container_width=True):
            status_map = {
                "🟡 Spraying In Progress": ("Spraying In Progress", "🟡 Spraying Active", "#F59E0B"),
                "🟢 Sprayed (Completed)": ("Sprayed", "🟢 Sprayed (Completed)", "#10B981"),
                "🔴 Spray Required (Pending)": ("Spray Required", "🔴 Pending Spray", "#EF4444"),
                "⚪ Healthy Zone": ("Healthy / No Spray", "⚪ Healthy Zone", "#10B981")
            }
            s_name, s_badge, s_col = status_map[new_status]
            for sa in sub_areas:
                if sa["sub_id"] == sub_target:
                    sa["status"] = s_name
                    sa["badge"] = s_badge
                    sa["color"] = s_col
            st.rerun()

    st.markdown("---")
    
    # 4. Zone GPS Table
    st.markdown("#### 📍 4. Zone GPS Telemetry & Geofencing Data")
    if st.session_state.report is not None:
        rep = st.session_state.report
        headers = [t["table_zone"], t["table_lat"], t["table_lon"], t["table_dist"], t["table_geofence"], t["table_alt"]]
        rows = []
        gps_export_data = []
        for z in rep["zones"]:
            zid = z["zone_id"]
            z_lat = z.get("latitude")
            z_lon = z.get("longitude")
            if z_lat is not None and z_lon is not None:
                z_lat = round(z_lat, 6)
                z_lon = round(z_lon, 6)
                if c_lat is not None and c_lon is not None:
                    is_inside, dist_m = check_geofence(z_lat, z_lon, c_lat, c_lon, radius)
                    fence_badge = (
                        f"<span style='color: #10B981; font-weight: 700;'>{t['geofence_inside']}</span>"
                        if is_inside else
                        f"<span style='color: #EF4444; font-weight: 700;'>{t['geofence_outside']}</span>"
                    )
                    dist_str = f"{dist_m} m"
                else:
                    is_inside, dist_m = True, 0.0
                    fence_badge = "<span style='color: #94A3B8;'>— No geofence set</span>"
                    dist_str = "—"
                lat_cell = f"<code>{z_lat:.6f}</code>"
                lon_cell = f"<code>{z_lon:.6f}</code>"
            else:
                is_inside, dist_m = False, 0.0
                fence_badge = "<span style='color: #94A3B8;'>— No GPS yet</span>"
                dist_str = "—"
                lat_cell = "<span style='color:#94A3B8;'>pending pipeline</span>"
                lon_cell = "<span style='color:#94A3B8;'>pending pipeline</span>"
            alt_str = "4.5 m (Autonomous)"

            rows.append([
                f"<strong>Zone {zid}</strong>",
                lat_cell,
                lon_cell,
                dist_str,
                fence_badge,
                alt_str
            ])
            gps_export_data.append({"Zone": zid, "Latitude": z_lat, "Longitude": z_lon, "Distance_Meters": dist_m, "Inside_Geofence": is_inside, "Severity": z["severity"]})
            
        render_custom_table(headers, rows)
        
        df_gps = pd.DataFrame(gps_export_data)
        csv_data = df_gps.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="⬇️ Export Zone GPS Data (.CSV)",
            data=csv_data,
            file_name="AgriVision_Zone_GPS.csv",
            mime="text/csv"
        )
    else:
        st.info("Run detection pipeline on Home page to view telemetry for all field zones.")

    st.markdown("---")

    # ==============================================================
    # 5. FIELD MAP: AERIAL VIEW, GROUND VIEW & EXACT COORDINATES
    # (read-only view of the saved boundary and target points)
    # ==============================================================
    st.markdown("#### 🛰️ 5. Field Map: Aerial View, Ground View & Exact Coordinates")
    st.write("Aerial view of the saved field boundary and target points, the exact coordinates of every point, and a ground-level view for any selected point.")

    s5_boundary = st.session_state.field_boundary
    s5_targets = st.session_state.marked_ground_areas

    def s5_area_ha_and_perimeter_m(poly):
        """Field area (hectares) and perimeter (meters) of a boundary given as [{"lat","lon"}, ...]."""
        if len(poly) < 3:
            return 0.0, 0.0
        lat0 = sum(p["lat"] for p in poly) / len(poly)
        lon0 = poly[0]["lon"]
        kx = 111320.0 * math.cos(math.radians(lat0))
        ky = 110574.0
        xy = [((p["lon"] - lon0) * kx, (p["lat"] - poly[0]["lat"]) * ky) for p in poly]
        twice_area = 0.0
        perimeter = 0.0
        for i in range(len(poly)):
            j = (i + 1) % len(poly)
            twice_area += xy[i][0] * xy[j][1] - xy[j][0] * xy[i][1]
            perimeter += calculate_haversine_distance(poly[i]["lat"], poly[i]["lon"], poly[j]["lat"], poly[j]["lon"])
        return abs(twice_area) / 2.0 / 10000.0, perimeter

    s5_area_ha, s5_perimeter_m = s5_area_ha_and_perimeter_m(s5_boundary)

    s5m1, s5m2, s5m3 = st.columns(3)
    with s5m1:
        st.markdown(
            f"""
            <div class="agri-card">
                <div class="metric-label">Field Area</div>
                <div class="metric-value">{s5_area_ha:.3f} ha</div>
            </div>
            """,
            unsafe_allow_html=True
        )
    with s5m2:
        st.markdown(
            f"""
            <div class="agri-card">
                <div class="metric-label">Perimeter</div>
                <div class="metric-value" style="color: #3B82F6;">{s5_perimeter_m:,.1f} m</div>
            </div>
            """,
            unsafe_allow_html=True
        )
    with s5m3:
        st.markdown(
            f"""
            <div class="agri-card">
                <div class="metric-label">Vertices / Targets</div>
                <div class="metric-value" style="color: #F59E0B;">{len(s5_boundary)} / {len(s5_targets)}</div>
            </div>
            """,
            unsafe_allow_html=True
        )

    # --- aerial map ---
    if s5_boundary:
        s5_center = [
            sum(p["lat"] for p in s5_boundary) / len(s5_boundary),
            sum(p["lon"] for p in s5_boundary) / len(s5_boundary),
        ]
    else:
        s5_center = [c_lat, c_lon]

    # --- exact coordinates table ---
    st.markdown("##### Exact Coordinates (WGS84)")
    s5_rows = []
    for s5_i, s5_p in enumerate(s5_boundary):
        s5_rows.append([
            f"<strong>V{s5_i + 1}</strong>",
            "Boundary vertex",
            esc(s5_p.get("name", f"Vertex {s5_i + 1}")),
            f"<code>{s5_p['lat']:.6f}</code>",
            f"<code>{s5_p['lon']:.6f}</code>"
        ])
    for s5_g in s5_targets:
        s5_rows.append([
            f"<strong>T{s5_g['id']}</strong>",
            "Target point",
            esc(s5_g["label"]),
            f"<code>{s5_g['lat']:.6f}</code>",
            f"<code>{s5_g['lon']:.6f}</code>"
        ])
    if s5_rows:
        render_custom_table(["ID", "Type", "Label", t["table_lat"], t["table_lon"]], s5_rows)
    else:
        st.info("No boundary vertices or target points saved yet.")

    s5_csv_rows = (
        [{"ID": f"V{i + 1}", "Type": "boundary_vertex", "Label": p.get("name", ""), "Latitude": p["lat"], "Longitude": p["lon"]}
         for i, p in enumerate(s5_boundary)] +
        [{"ID": f"T{g['id']}", "Type": "target_point", "Label": g["label"], "Latitude": g["lat"], "Longitude": g["lon"]}
         for g in s5_targets]
    )
    s5_csv = pd.DataFrame(s5_csv_rows, columns=["ID", "Type", "Label", "Latitude", "Longitude"]).to_csv(index=False).encode("utf-8")

    st.download_button(
        label="⬇️ Download Coordinates (.CSV)",
        data=s5_csv,
        file_name="AgriVision_Field_Coordinates.csv",
        mime="text/csv",
        use_container_width=True,
        key="s5_dl_csv"
    )

    # --- ground-level view ---
    st.markdown("##### Ground-Level View")
    s5_points = []
    if s5_boundary:
        s5_points.append(("Field centre", s5_center[0], s5_center[1]))
    for s5_i, s5_p in enumerate(s5_boundary):
        s5_points.append((f"Vertex V{s5_i + 1}", s5_p["lat"], s5_p["lon"]))
    for s5_g in s5_targets:
        s5_points.append((f"Target T{s5_g['id']}: {s5_g['label']}", s5_g["lat"], s5_g["lon"]))

    if s5_points:
        s5_pick = st.selectbox(
            "Select a point to view at ground level:",
            list(range(len(s5_points))),
            format_func=lambda i: s5_points[i][0],
            key="s5_ground_point"
        )
        s5_label, s5_plat, s5_plon = s5_points[min(s5_pick, len(s5_points) - 1)]
        st.markdown(f"Exact point: <code>{s5_plat:.6f}, {s5_plon:.6f}</code>", unsafe_allow_html=True)

        st.caption("Ground-level imagery is often missing on farmland and rural tracks. Satellite imagery can also be offset from the real ground by a few meters, "
                   "so for survey-grade boundaries use GPS/RTK readings taken in the field.")
    else:
        st.info("Add boundary vertices or target points above to open a ground-level view.")

    # ==============================================================
    # 6. SIMULATED SURVEY & SPRAY MISSION (outer boundary -> inner boundary -> spray)
    # ==============================================================
    st.markdown("---")
    render_mission_section(
        st, pd,
        boundary=st.session_state.field_boundary,
        boundary_is_default=st.session_state.get("boundary_is_default", True),
        zones=(st.session_state.report["zones"] if st.session_state.report is not None else []),
        maptiler_key=maptiler_key,
        get_sub_areas=get_sub_areas_for_zone,
        render_table=render_custom_table,
    )


# ==================================================================
# PAGE 4: 🔍 PROBLEM AREAS
# ==================================================================
elif st.session_state.current_page == "problems":
    if st.session_state.report is None:
        render_empty_state()
    else:
        rep = st.session_state.report
        st.markdown("### 🔍 Problem Detection Area")
        st.write("Detailed zone-by-zone diagnostic evaluation with soft severity color gradient cards.")
        
        zones = rep["zones"]
        cols = st.columns(3)
        
        for idx, z in enumerate(zones):
            zid = z["zone_id"]
            sev = z["severity"]
            raw_desc = z.get("problem_description", "")
            clean_desc = sanitize_problem_description(raw_desc, sev)
            pests = z.get("detected_classes", [])
            pests_str = ", ".join(pests) if pests else t["no_pests"]
            canopy_pct = z.get("affected_canopy_pct", 0.0)
            
            card_class = f"severity-{severity_class(sev)}"
            
            sub_list = get_sub_areas_for_zone(zid, sev, z["spray_needed"])
            sub_badges_html = " ".join([
                f"<span style='font-size: 10px; padding: 2px 6px; border-radius: 8px; background: {sa['color']}; color: #FFFFFF; font-weight: 700;'>{esc(sa['sub_id'])}: {esc(sa['status'])}</span>"
                for sa in sub_list
            ])
            
            col_target = cols[idx % 3]
            with col_target:
                col_target.markdown(
                    flat(f"""
                    <div class="zone-card {card_class}">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <strong style="font-size: 16px;">📍 ZONE {esc(zid)}</strong>
                            <span style="text-transform: uppercase; font-weight: 800; font-size: 12px; padding: 4px 10px; border-radius: 12px; background: rgba(0,0,0,0.15);">
                                {severity_label(sev)}
                            </span>
                        </div>
                        <p style="margin: 8px 0; font-size: 14px; line-height: 1.5; font-weight: 500;">
                            {esc(clean_desc)}
                        </p>
                        <div style="font-size: 12px; opacity: 0.9; margin-top: 12px; border-top: 1px solid rgba(0,0,0,0.15); padding-top: 8px;">
                            <div>🐛 <strong>Detected Pests:</strong> {esc(pests_str)}</div>
                            <div>🍂 <strong>Affected Canopy:</strong> {canopy_pct:.1f}%</div>
                            <div style="margin-top: 6px; font-size: 11px;">💦 <strong>Sub-Area Statuses:</strong></div>
                            <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px;">{sub_badges_html}</div>
                        </div>
                    </div>
                    """),
                    unsafe_allow_html=True
                )


# ==================================================================
# PAGE 5: 💦 SPRAY ADVISORY
# ==================================================================
elif st.session_state.current_page == "spray":
    if st.session_state.report is None:
        render_empty_state()
    else:
        rep = st.session_state.report
        st.markdown("### 💦 Precision Spray Advisory & Chemical Dosage")
        
        zones = rep["zones"]
        
        spray_attention_zones = [z for z in zones if z.get("spray_needed") or z.get("severity", "none") != "none"]
        
        spray_zones_count = len(spray_attention_zones)
        total_dose = sum(z.get("dose_liters_per_ha", 0.0) for z in spray_attention_zones)
        
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                f"""
                <div class="agri-card" style="border-left: 6px solid #F59E0B;">
                    <div class="metric-label">Zones Requiring Spray / Attention</div>
                    <div class="metric-value" style="color: #F59E0B;">{spray_zones_count} / {len(zones)}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with c2:
            st.markdown(
                f"""
                <div class="agri-card" style="border-left: 6px solid #10B981;">
                    <div class="metric-label">Total Estimated Spray Volume</div>
                    <div class="metric-value" style="color: #10B981;">{total_dose:.1f} L/ha</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            
        if not spray_attention_zones:
            st.markdown(
                f"""
                <div class="agri-card" style="border-left: 6px solid #10B981; padding: 24px; text-align: center;">
                    <h3 style="color: #10B981; margin: 0;">✅ All Field Areas are Healthy</h3>
                    <p style="margin: 8px 0 0 0; opacity: 0.8;">Zero zones require chemical spraying or attention.</p>
                </div>
                """,
                unsafe_allow_html=True
            )
        else:
            headers = [
                t["table_zone"],
                t["table_severity"],
                t["table_spray_status"],
                t["table_dose"],
                t["table_chemical"],
                t["table_drone_flow"]
            ]
            
            rows = []
            for z in spray_attention_zones:
                zid = z["zone_id"]
                sev = z["severity"]
                spray = z["spray_needed"]
                dose = z.get("dose_liters_per_ha", 0.0)
                chem = z.get("recommended_chemical", "Targeted Pesticide")
                
                action_badge = "<span style='color: #EF4444; font-weight: 800;'>SPRAY REQUIRED</span>" if spray else "<span style='color: #F59E0B; font-weight: 700;'>ATTENTION</span>"
                flow_rate = f"{round(dose * 0.4, 2)} L/min"
                
                rows.append([
                    f"<strong>Zone {zid}</strong>",
                    f"<span style='text-transform: uppercase; font-weight: 700;'>{severity_label(sev)}</span>",
                    action_badge,
                    f"{dose:.1f} L/ha",
                    chem,
                    flow_rate
                ])
                
            render_custom_table(headers, rows)

            st.markdown("#### 🎯 Targeted Sub-Area Spray Breakdown")
            for z in spray_attention_zones:
                zid = z["zone_id"]
                sub_list = [sa for sa in get_sub_areas_for_zone(zid, z["severity"], z["spray_needed"]) if sa["status"] != "Healthy / No Spray"]
                if not sub_list:
                    continue
                
                sub_badges_html = ""
                for sa in sub_list:
                    sub_badges_html += f"""
                    <div style="background: {sa['color']}22; border: 1px solid {sa['color']}; border-radius: 10px; padding: 8px 12px; margin-right: 8px; margin-bottom: 8px; font-size: 12px;">
                        <strong>{esc(sa['sub_id'])} ({esc(sa['quadrant'])}):</strong> 
                        <span style="color: {sa['color']}; font-weight: 800;">{esc(sa['badge'])}</span>
                    </div>
                    """
                
                st.markdown(
                    flat(f"""
                    <div style="background: rgba(0,0,0,0.03); border: 1px solid rgba(0,0,0,0.1); border-radius: 12px; padding: 14px; margin-bottom: 12px;">
                        <strong style="font-size: 14px;">📍 Grid Zone {zid} Sub-Area Spray Indicators:</strong>
                        <div style="display: flex; flex-wrap: wrap; margin-top: 8px;">{sub_badges_html}</div>
                    </div>
                    """),
                    unsafe_allow_html=True
                )


# ==================================================================
# PAGE 6: 🐛 PEST CLASSES
# ==================================================================
elif st.session_state.current_page == "pests":
    st.markdown("### 🐛 Accessible Crop Pest & Disease Catalog")
    st.write("Plain-language technical explanations and recommended treatments for farmers.")
    
    if st.session_state.report is not None:
        rep = st.session_state.report
        detected_set = set()
        for z in rep["zones"]:
            for p in z.get("detected_classes", []):
                detected_set.add(p.lower())
                
        if detected_set:
            st.markdown(
                f"""
                <div style="background: rgba(245, 158, 11, 0.15); border: 1px solid #F59E0B; border-radius: 16px; padding: 16px; margin-bottom: 24px;">
                    <h4 style="margin: 0 0 8px 0; color: #D97706; font-size: 15px;">⚠️ Active Pests Detected in Current Video:</h4>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                        {"".join([f'<span style="background: #F59E0B; color: #FFFFFF; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 700;">{p.upper()}</span>' for p in detected_set])}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )
            
    pcols = st.columns(2)
    items = list(PEST_ACCESSIBLE_INFO.items())
    
    for idx, (key, info) in enumerate(items):
        name = info["name_ur"] if lang == "ur" else info["name"]
        desc = info["desc_ur"] if lang == "ur" else info["desc"]
        
        target_col = pcols[idx % 2]
        with target_col:
            target_col.markdown(
                f"""
                <div class="agri-card" style="border-left: 6px solid #10B981;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span style="font-size: 28px;">{info['icon']}</span>
                            <strong style="font-size: 17px;">{name}</strong>
                        </div>
                        <span style="font-size: 11px; font-weight: 700; background: rgba(0,0,0,0.06); padding: 4px 10px; border-radius: 10px;">
                            {info['risk_badge']}
                        </span>
                    </div>
                    <p style="font-size: 13.5px; color: { '#CBD5E1' if theme == 'dark' else '#475569' }; line-height: 1.5; margin-bottom: 12px;">
                        {desc}
                    </p>
                    <div style="font-size: 12px; background: { '#334155' if theme == 'dark' else '#F1F5F9' }; padding: 10px; border-radius: 10px;">
                        💊 <strong>Treatment Advisory:</strong> {info['treatment']}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )


# ==================================================================
# PAGE 7: ⚙️ SETTINGS
# ==================================================================
elif st.session_state.current_page == "settings":
    st.markdown(f"### {t['settings_title']}")
    
    c1, c2 = st.columns(2)
    
    with c1:
        st.markdown(f"#### {t['theme_toggle_label']}")
        theme_choice = st.radio(
            "Select Theme Mode:",
            [t["theme_light"], t["theme_dark"]],
            index=0 if st.session_state.theme == "light" else 1
        )
        new_theme = "light" if theme_choice == t["theme_light"] else "dark"
        if new_theme != st.session_state.theme:
            st.session_state.theme = new_theme
            st.rerun()
            
    with c2:
        st.markdown(f"#### {t['lang_toggle_label']}")
        lang_choice = st.radio(
            "Select Language:",
            [t["lang_en"], t["lang_ur"]],
            index=0 if st.session_state.language == "en" else 1
        )
        new_lang = "en" if lang_choice == t["lang_en"] else "ur"
        if new_lang != st.session_state.language:
            st.session_state.language = new_lang
            st.rerun()
            
    st.markdown("---")
    st.markdown(f"#### {t['geofence_setup']}")
    
    gcol1, gcol2, gcol3 = st.columns(3)
    with gcol1:
        new_lat = st.number_input(t["center_lat"], value=st.session_state.geofence_center_lat, format="%.6f")
    with gcol2:
        new_lon = st.number_input(t["center_lon"], value=st.session_state.geofence_center_lon, format="%.6f")
    with gcol3:
        new_rad = st.number_input(t["geofence_radius"], value=st.session_state.geofence_radius_m, min_value=1.0, max_value=5000.0)
        
    if (new_lat != st.session_state.geofence_center_lat or 
        new_lon != st.session_state.geofence_center_lon or 
        new_rad != st.session_state.geofence_radius_m):
        st.session_state.geofence_center_lat = new_lat
        st.session_state.geofence_center_lon = new_lon
        st.session_state.geofence_radius_m = new_rad
        st.success("✅ Geofence configuration updated successfully!")