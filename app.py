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

from pest_detection_severity_spray import (
    process_video, GRID_ROWS, GRID_COLS,
    boundary_polygon, geo_distance_m, view_for_polygon, build_spray_layers,
)
import html as _html


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
# SPRAY STATUS STYLES (the GIS geometry itself lives in pest_detection_severity_spray.py)
# ==================================================================
SPRAY_STATUS_STYLES = {
    "Spray Required": ("🔴 Pending Spray", "#EF4444"),
    "Spraying In Progress": ("🟡 Spraying Active", "#F59E0B"),
    "Sprayed": ("🟢 Sprayed (Completed)", "#10B981"),
}


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
if "field_boundary" not in st.session_state:
    st.session_state.field_boundary = []
if "inner_margin_m" not in st.session_state:
    st.session_state.inner_margin_m = 5.0
if "grid_cell_m" not in st.session_state:
    st.session_state.grid_cell_m = 10.0
if "grid_cell_status" not in st.session_state:
    st.session_state.grid_cell_status = {}

lang = st.session_state.language
theme = st.session_state.theme
t = TRANSLATIONS[lang]


def get_outer_polygon():
    """The user's outer (flight) boundary as a shapely Polygon, or None if not defined yet."""
    return boundary_polygon(st.session_state.field_boundary)


def boundary_signature():
    return json.dumps([(p["lat"], p["lon"]) for p in st.session_state.field_boundary])


def report_matches_boundary():
    """True if the last detection run used the boundary that is currently defined."""
    rep = st.session_state.report
    return rep is not None and rep.get("boundary_sig") == boundary_signature()


def get_gis_state():
    """GIS layers of the last detection run (infected points -> inner boundary -> spray grid).
    The points and zone geometry come from the pipeline; only the margin / cell size can be
    changed here, which re-runs the same GIS function (no video reprocessing).
    Returns None until a run exists for the CURRENT boundary."""
    rep = st.session_state.report
    poly = get_outer_polygon()
    if rep is None or poly is None or not report_matches_boundary():
        return None
    margin = float(st.session_state.inner_margin_m)
    cell = float(st.session_state.grid_cell_m)
    sig = (boundary_signature(), margin, cell, id(rep))
    cached = st.session_state.get("gis_cache")
    if cached and cached["sig"] == sig:
        return cached["data"]
    data = build_spray_layers(poly, rep["infected_points"], rep["zone_geo"], margin, cell)
    st.session_state.gis_cache = {"sig": sig, "data": data}
    st.session_state.grid_cell_status = {}
    return data


def get_sub_areas_for_zone(zone_id, severity="none", spray_needed=False):
    """Spray-grid cells (inside the auto-generated inner boundary) that belong to a zone."""
    gis = get_gis_state()
    if gis is None:
        return []
    out = []
    for c in gis["grid"]:
        if c["zone_id"] != zone_id:
            continue
        status = st.session_state.grid_cell_status.get(c["cell_id"], "Spray Required")
        badge, color = SPRAY_STATUS_STYLES[status]
        out.append({
            "sub_id": c["cell_id"],
            "quadrant": f"{c['lat']:.6f}, {c['lon']:.6f}",
            "status": status,
            "badge": badge,
            "color": color,
        })
    return out


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
    gis_raw = raw_report.get("_gis") or {}
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
                "observed_frames": zdata.get("observed_frames", 0),
                "raw": zdata
            })
            zone_index += 1

    return {
        "zones": zones,
        "annotated_video_path": output_video_path,
        "class_tally": class_tally,
        "infected_points": gis_raw.get("infected", []),
        "zone_geo": gis_raw.get("zone_geo", {}),
        "boundary_sig": boundary_signature(),
        "metadata": {
            "filename": os.path.basename(input_video_path),
            "total_frames_analyzed": meta.get("frames_analyzed", "Completed"),
            "fps": meta.get("output_fps", 20),
            "ground_fixed_zones": meta.get("ground_fixed_zones", False),
            "motion_tracking_failures": meta.get("motion_tracking_failures", 0),
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
    
    project_dir = Path(__file__).resolve().parent
    demo_files = []
    if project_dir.exists():
        for f in project_dir.glob("*.mp4"):
            if "annotated" not in f.name.lower() and "output" not in f.name.lower():
                demo_files.append(f.name)
                
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
        target_video_path = str(project_dir / selected_demo)
        st.video(target_video_path)

    st.markdown("<br>", unsafe_allow_html=True)
    
    if target_video_path:
        if get_outer_polygon() is None:
            st.warning("Define the field boundary first (Field Map & GPS page). The drone analysis runs inside that boundary.")
        elif st.button(t["btn_run_pipeline"], type="primary", use_container_width=True):
            progress_bar = st.progress(0)
            status_text = st.empty()
            status_text.info(t["processing_msg"])
            
            def progress_cb(current, total):
                if total > 0:
                    pct = min(1.0, current / total)
                    progress_bar.progress(pct)
                    status_text.text(f"Processing frame {current}/{total} ({int(pct*100)}%)...")

            try:
                output_dir = str(Path(__file__).resolve().parent / "outputs")
                os.makedirs(output_dir, exist_ok=True)
                out_vid_path = os.path.join(output_dir, "annotated_video.mp4")
                rep_json_path = os.path.join(output_dir, "spray_report.json")
                
                raw_report, class_tally, final_out_vid, model_warnings = process_video(
                    video_path=target_video_path,
                    output_video=out_vid_path,
                    report_path=rep_json_path,
                    progress_callback=progress_cb,
                    boundary=[(p["lat"], p["lon"]) for p in st.session_state.field_boundary],
                    inner_margin_m=float(st.session_state.inner_margin_m),
                    grid_cell_m=float(st.session_state.grid_cell_m),
                )
                
                formatted_report = format_pipeline_output(raw_report, class_tally, final_out_vid, target_video_path)
                st.session_state.report = formatted_report
                
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
    st.markdown("### 📍 Field Map & GPS Coordinates")
    st.write("Define the flight boundary on the map. After detection, the infected points, the inner spray boundary and the spray grid are generated automatically.")

    # Grid settings sit above the map so a change is applied in the same run
    pc1, pc2 = st.columns(2)
    with pc1:
        st.session_state.inner_margin_m = st.number_input(
            "Inner boundary margin (m)", min_value=0.0, max_value=500.0, step=1.0,
            value=float(st.session_state.inner_margin_m), key="w_inner_margin"
        )
    with pc2:
        st.session_state.grid_cell_m = st.number_input(
            "Spray grid cell size (m)", min_value=1.0, max_value=500.0, step=1.0,
            value=float(st.session_state.grid_cell_m), key="w_grid_cell"
        )

    outer_poly = get_outer_polygon()
    gis = get_gis_state()
    if st.session_state.report is not None and outer_poly is not None and not report_matches_boundary():
        st.warning("The field boundary was changed after the last detection run. "
                   "Run the detection pipeline again on the Home page so the points and grid match the new boundary.")

    with st.expander("🗺️ Interactive Field Map View (Tap / Click to Expand)", expanded=False):
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

        trigger_search = do_search or (search_query and search_query.strip() != st.session_state.get("last_search_query", ""))

        if trigger_search and search_query and search_query.strip():
            st.session_state["last_search_query"] = search_query.strip()
            s_lat, s_lon, s_addr, status = geocode_location(search_query.strip())

            if status == "success" and s_lat is not None and s_lon is not None:
                st.session_state.map_center = [s_lat, s_lon]
                st.session_state.map_zoom = 17
                st.session_state.search_marker = (s_lat, s_lon, s_addr)
                s_lat_r, s_lon_r = round(float(s_lat), 6), round(float(s_lon), 6)
                st.session_state.last_captured_lat = s_lat_r
                st.session_state.last_captured_lon = s_lon_r
                st.session_state["bound_v_lat"] = s_lat_r
                st.session_state["bound_v_lon"] = s_lon_r
                st.success(f"📍 Map navigated to: {s_addr}")
            elif status == "error":
                st.error(f"⚠️ {s_addr}")
            elif status == "network_error":
                st.error("⚠️ Geocoding service is temporarily unreachable. Please check your internet connection or enter direct coordinates (e.g. 33.5673, 73.1015).")
            else:
                st.error(f"❌ Location '{search_query.strip()}' could not be found. Please check spelling or enter direct coordinates (e.g. 33.5673, 73.1015).")

        # 2. CREATE FOLIUM MAP (no demo location: boundary view, searched place, or a world view)
        if "map_center" in st.session_state:
            map_center = st.session_state.map_center
            map_zoom = st.session_state.get("map_zoom", 16)
        elif outer_poly is not None:
            map_center, map_zoom = view_for_polygon(outer_poly)
        else:
            map_center, map_zoom = [20.0, 0.0], 2

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

        # Outer boundary (user defined)
        poly_coords = [[pt["lat"], pt["lon"]] for pt in st.session_state.field_boundary]
        if len(poly_coords) >= 3:
            folium.Polygon(
                locations=poly_coords,
                color="#10B981",
                fill=True,
                fill_color="#34D399",
                fill_opacity=0.2,
                weight=3,
                popup="🌿 Outer Field Boundary (flight area)"
            ).add_to(m)

        for idx, pt in enumerate(st.session_state.field_boundary):
            folium.CircleMarker(
                location=[pt["lat"], pt["lon"]],
                radius=6,
                color="#047857",
                fill=True,
                fill_color="#6EE7B7",
                fill_opacity=1.0,
                popup=f"Boundary Vertex #{idx+1}: {esc(pt.get('name', 'Point'))} ({pt['lat']:.6f}, {pt['lon']:.6f})"
            ).add_to(m)

        # Automatic GIS layers (only after detection has run)
        if gis is not None:
            for zid, zg in gis["zone_geo"].items():
                for ring in zg["rings"]:
                    folium.Polygon(
                        locations=ring, color="#94A3B8", weight=1, dash_array="3, 6",
                        fill=False, tooltip=f"Zone {zid}"
                    ).add_to(m)
            for ring in gis["inner_rings"]:
                folium.Polygon(
                    locations=ring, color="#F59E0B", weight=3, dash_array="8, 6",
                    fill=True, fill_color="#FBBF24", fill_opacity=0.12,
                    tooltip="Inner spray boundary (auto-generated)"
                ).add_to(m)
            for cell in gis["grid"]:
                cell_status = st.session_state.grid_cell_status.get(cell["cell_id"], "Spray Required")
                cell_color = SPRAY_STATUS_STYLES[cell_status][1]
                for ring in cell["rings"]:
                    folium.Polygon(
                        locations=ring, color=cell_color, weight=1,
                        fill=True, fill_color=cell_color, fill_opacity=0.3,
                        tooltip=f"{cell['cell_id']} - {cell_status}"
                    ).add_to(m)
            for ip in gis["infected"]:
                folium.Marker(
                    location=[ip["lat"], ip["lon"]],
                    popup=f"<b>Infected point - Zone {ip['zone_id']}</b><br>{esc(ip['pests'] or t['no_pests'])}<br><code>{ip['lat']:.6f}, {ip['lon']:.6f}</code>",
                    icon=folium.Icon(color="orange", icon="info-sign")
                ).add_to(m)

        if st.session_state.get("search_marker"):
            slat, slon, saddr = st.session_state.search_marker
            folium.Marker(
                location=[slat, slon],
                popup=f"<b>🔍 Searched Location:</b><br>{esc(saddr)}<br><code>{slat:.6f}, {slon:.6f}</code>",
                icon=folium.Icon(color="green", icon="search")
            ).add_to(m)

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

        map_output = st_folium(
            m,
            width="100%",
            height=440,
            key="folium_field_map",
            returned_objects=["last_clicked", "last_active_drawing", "all_drawings"]
        )

        # Remember the latest drawn polygon / rectangle (used by "Use drawn shape as boundary")
        if map_output and map_output.get("all_drawings") is not None:
            latest_poly = None
            for shape in map_output["all_drawings"]:
                geom = (shape or {}).get("geometry") or {}
                if geom.get("type") == "Polygon" and geom.get("coordinates"):
                    ring = geom["coordinates"][0]
                    if len(ring) > 1 and ring[0] == ring[-1]:
                        ring = ring[:-1]
                    if len(ring) >= 3:
                        latest_poly = [[round(float(pt[1]), 6), round(float(pt[0]), 6)] for pt in ring]
            st.session_state.drawn_polygon = latest_poly

        # 3. AUTO-CAPTURE CLICKED / DRAWN COORDINATES INTO THE VERTEX FIELDS
        if map_output:
            captured_lat = None
            captured_lon = None

            click_pt = None
            last_click = map_output.get("last_clicked")
            if last_click and isinstance(last_click, dict):
                click_pt = (round(float(last_click["lat"]), 6), round(float(last_click["lng"]), 6))

            draw_pt = None
            draw_sig = None
            drawing = map_output.get("last_active_drawing")
            if drawing and isinstance(drawing, dict) and drawing.get("geometry"):
                gtype = drawing["geometry"].get("type")
                coords = drawing["geometry"].get("coordinates")
                draw_sig = json.dumps(coords)
                if gtype == "Point" and coords and len(coords) >= 2:
                    draw_pt = (round(float(coords[1]), 6), round(float(coords[0]), 6))
                elif gtype == "Polygon" and coords and coords[0]:
                    ring = coords[0]
                    if len(ring) > 1 and ring[0] == ring[-1]:
                        ring = ring[:-1]
                    draw_pt = (
                        round(sum(p[1] for p in ring) / len(ring), 6),
                        round(sum(p[0] for p in ring) / len(ring), 6),
                    )

            if draw_sig is not None and draw_sig != st.session_state.get("last_draw_sig"):
                st.session_state["last_draw_sig"] = draw_sig
                if draw_pt:
                    captured_lat, captured_lon = draw_pt
            elif click_pt is not None and click_pt != st.session_state.get("last_click_pt"):
                captured_lat, captured_lon = click_pt

            if click_pt is not None:
                st.session_state["last_click_pt"] = click_pt

            if captured_lat is not None and captured_lon is not None:
                st.session_state.last_captured_lat = captured_lat
                st.session_state.last_captured_lon = captured_lon
                st.session_state["bound_v_lat"] = captured_lat
                st.session_state["bound_v_lon"] = captured_lon
                st.rerun()

        if st.session_state.get("last_captured_lat") is not None:
            st.info(f"📍 **Auto-Captured Map Coordinates:** Latitude `{st.session_state.last_captured_lat:.6f}`, Longitude `{st.session_state.last_captured_lon:.6f}`")

    st.markdown("---")

    # ==============================================================
    # 1. OUTER BOUNDARY (FLIGHT AREA) - USER DEFINED
    # ==============================================================
    st.markdown("#### 📐 1. Field Boundary (Flight Area)")
    st.write("Draw the outer boundary the drone flies in. Detection is mapped only inside this boundary. Add vertices in order around the field.")

    boundary_notice = st.session_state.pop("boundary_notice", None)
    if boundary_notice:
        st.success("✅ " + boundary_notice)

    bcol1, bcol2 = st.columns([2, 1])
    with bcol1:
        st.markdown("##### Current Outer Perimeter Vertices:")
        if st.session_state.field_boundary:
            boundary_rows = []
            for idx, pt in enumerate(st.session_state.field_boundary):
                boundary_rows.append([
                    f"Vertex #{idx + 1}",
                    esc(pt.get("name", f"Point {idx+1}")),
                    f"<code>{pt['lat']:.6f}</code>",
                    f"<code>{pt['lon']:.6f}</code>"
                ])
            render_custom_table(["Vertex", "Label", "Latitude", "Longitude"], boundary_rows)
        else:
            st.info("No boundary defined yet. Draw a rectangle or polygon on the map and press the green button, or add vertices manually.")

        drawn_poly = st.session_state.get("drawn_polygon")
        if drawn_poly:
            if st.button(f"🟩 Use drawn shape as boundary ({len(drawn_poly)} corners)", type="primary", use_container_width=True, key="use_drawn_boundary"):
                new_boundary = [
                    {"lat": lat, "lon": lon, "name": f"Vertex {i + 1}"}
                    for i, (lat, lon) in enumerate(drawn_poly)
                ]
                st.session_state.field_boundary = new_boundary
                new_poly = boundary_polygon(new_boundary)
                if new_poly is not None:
                    st.session_state.map_center, st.session_state.map_zoom = view_for_polygon(new_poly)
                st.session_state["boundary_notice"] = "Boundary updated from the drawn shape."
                st.rerun()
        else:
            st.caption("Draw a rectangle or polygon on the map above to use it as the field boundary.")

    with bcol2:
        st.markdown("##### Add Boundary Point:")
        v_name = st.text_input("Vertex Label", value=f"Vertex #{len(st.session_state.field_boundary)+1}")

        def coord_input(label, key, lo, hi):
            if st.session_state.get(key) is not None:
                return st.number_input(label, min_value=lo, max_value=hi, format="%.6f", key=key)
            try:
                return st.number_input(label, min_value=lo, max_value=hi, format="%.6f", value=None,
                                       placeholder="Click on the map", key=key)
            except Exception:
                return st.number_input(label, min_value=lo, max_value=hi, format="%.6f", value=0.0, key=key)

        v_lat = coord_input("Latitude", "bound_v_lat", -90.0, 90.0)
        v_lon = coord_input("Longitude", "bound_v_lon", -180.0, 180.0)

        btn_c1, btn_c2 = st.columns(2)
        with btn_c1:
            if st.button("➕ Add Vertex", type="primary", use_container_width=True):
                if v_lat is None or v_lon is None:
                    st.error("Click a point on the map or type both coordinates first.")
                else:
                    st.session_state.field_boundary.append({"lat": float(v_lat), "lon": float(v_lon), "name": v_name})
                    st.rerun()
        with btn_c2:
            if st.button("🗑️ Clear Boundary", use_container_width=True):
                st.session_state.field_boundary = []
                st.session_state.pop("gis_cache", None)
                st.session_state.grid_cell_status = {}
                st.rerun()

    st.markdown("---")

    # ==============================================================
    # 2. AUTO-DETECTED INFECTED POINTS
    # ==============================================================
    st.markdown("#### 🎯 2. Auto-Detected Infected Points")
    st.write("Marked automatically from the detection results inside the field boundary. No manual marking needed.")

    if st.session_state.report is None:
        st.info("Run the detection pipeline on the Home page to get infected points.")
    elif gis is None:
        st.info("Define the field boundary above to place the detection results on the map.")
    elif not gis["infected"]:
        st.success("No infected points detected inside the field boundary.")
    else:
        inf_rows = []
        for i, ip in enumerate(gis["infected"], start=1):
            inf_rows.append([
                f"Point #{i}",
                f"Zone {ip['zone_id']}",
                severity_label(ip["severity"]),
                esc(ip["pests"] or t["no_pests"]),
                f"<code>{ip['lat']:.6f}</code>",
                f"<code>{ip['lon']:.6f}</code>",
            ])
        render_custom_table(["Point", "Zone", "Severity", "Detected Pests", "Latitude", "Longitude"], inf_rows)

    st.markdown("---")

    # ==============================================================
    # 3. AUTO INNER BOUNDARY AND SPRAY GRID
    # ==============================================================
    st.markdown("#### 💦 3. Auto Inner Boundary & Spray Grid")
    st.write("The inner boundary is generated around the infected area, and the spray grid is generated inside it. Only these grid cells are sprayed.")

    if gis is None:
        st.info("Run the detection pipeline with a field boundary defined to generate the inner boundary and the grid.")
    elif not gis["grid"]:
        st.success("No infected area, so no inner boundary or spray grid was generated.")
    else:
        grid_note = f"💡 **Inner boundary area:** {gis['inner_area_m2']:.0f} m² of {gis['field_area_m2']:.0f} m² | **Grid cells:** {len(gis['grid'])} | **Cell size:** {gis['cell_m_used']} m"
        if gis["cell_m_used"] != gis["cell_m_requested"]:
            grid_note += f" (increased from {gis['cell_m_requested']:.0f} m to keep the grid manageable)"
        st.info(grid_note)

        grid_rows = []
        for cell in gis["grid"]:
            cell_status = st.session_state.grid_cell_status.get(cell["cell_id"], "Spray Required")
            badge, color = SPRAY_STATUS_STYLES[cell_status]
            grid_rows.append([
                f"<strong>{cell['cell_id']}</strong>",
                f"Zone {cell['zone_id']}",
                f"<code>{cell['lat']:.6f}</code>",
                f"<code>{cell['lon']:.6f}</code>",
                severity_label(cell["severity"]),
                f"{cell['spray_dose_percent']}%",
                f"<span style='color: {color}; font-weight: 700;'>{badge}</span>",
            ])
        render_custom_table(["Cell", "Zone", "Latitude", "Longitude", "Severity", "Dose", "Spray Status"], grid_rows)

        st.markdown("##### Update Cell Spray Status:")
        sel_cell = st.selectbox("Select Grid Cell:", [c["cell_id"] for c in gis["grid"]])
        new_status = st.selectbox(
            "Set Spray Status:", list(SPRAY_STATUS_STYLES.keys()),
            format_func=lambda s: SPRAY_STATUS_STYLES[s][0]
        )
        if st.button("💾 Update Cell Status", type="primary", use_container_width=True):
            st.session_state.grid_cell_status[sel_cell] = new_status
            st.rerun()

    st.markdown("---")

    # ==============================================================
    # 4. ZONE GPS TELEMETRY (REAL MAP COORDINATES)
    # ==============================================================
    st.markdown("#### 📍 4. Zone GPS Telemetry & Field Check")
    if st.session_state.report is None:
        st.info("Run the detection pipeline on the Home page to view coordinates for all field zones.")
    elif gis is None:
        st.info("Define the field boundary above to get the map coordinates of each zone.")
    else:
        rep = st.session_state.report
        headers = [t["table_zone"], t["table_lat"], t["table_lon"], t["table_dist"], t["table_geofence"], t["table_alt"]]
        rows = []
        gps_export_data = []
        for z in rep["zones"]:
            zid = z["zone_id"]
            zg = gis["zone_geo"][zid]
            z_lat, z_lon = zg["lat"], zg["lon"]
            dist_m = round(geo_distance_m(gis["center"][0], gis["center"][1], z_lat, z_lon), 1)
            is_inside = zg["has_area"]
            fence_badge = f"<span style='color: #10B981; font-weight: 700;'>{t['geofence_inside']}</span>" if is_inside else f"<span style='color: #EF4444; font-weight: 700;'>{t['geofence_outside']}</span>"
            alt_str = "4.5 m (Autonomous)"

            rows.append([
                f"<strong>Zone {zid}</strong>",
                f"<code>{z_lat:.6f}</code>",
                f"<code>{z_lon:.6f}</code>",
                f"{dist_m} m",
                fence_badge,
                alt_str
            ])
            gps_export_data.append({"Zone": zid, "Latitude": z_lat, "Longitude": z_lon, "Distance_Meters": dist_m, "Inside_Field_Boundary": is_inside, "Severity": z["severity"]})

        render_custom_table(headers, rows)

        df_gps = pd.DataFrame(gps_export_data)
        csv_data = df_gps.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="⬇️ Export Zone GPS Data (.CSV)",
            data=csv_data,
            file_name="AgriVision_Zone_GPS.csv",
            mime="text/csv"
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