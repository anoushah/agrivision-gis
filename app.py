
import streamlit as st
import os
import json
import time
import tempfile
import re
import requests
from pathlib import Path
import pandas as pd
import streamlit.components.v1 as components
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium

import importlib
import pest_detection_severity_spray as _psp
if getattr(_psp, "ZONE_FIT_VERSION", 0) < 2:
    importlib.reload(_psp)

from pest_detection_severity_spray import (
    process_video, GRID_ROWS, GRID_COLS,
    boundary_polygon, geo_distance_m, view_for_polygon, build_spray_layers,
)
import html as _html


def flat(markup: str) -> str:
    return " ".join(line.strip() for line in str(markup).splitlines() if line.strip())


def esc(value) -> str:
    return _html.escape(str(value), quote=True)


st.set_page_config(
    page_title="AgriVision — Smart Drone Crop Protection",
    layout="wide",
    page_icon="🌾",
    initial_sidebar_state="expanded"
)

if getattr(_psp, "ZONE_FIT_VERSION", 0) < 2:
    st.error("Old pest_detection_severity_spray.py is loaded. Replace it with the new file placed next to app.py, then fully stop and restart Streamlit.")
    st.stop()

SPRAY_STATUS_STYLES = {
    "Spray Required": ("🔴 Pending Spray", "#EF4444"),
    "Spraying In Progress": ("🟡 Spraying Active", "#F59E0B"),
    "Sprayed": ("🟢 Sprayed (Completed)", "#10B981"),
}


GEO_NOISE = re.compile(
    r"\b(agri\w*|agricu\w*|agriculture|field|fields|farm|farms|farmland|khet|kheti|zameen|land|near|me)\b",
    re.I,
)

GEO_COUNTRY_BIAS = "pk"

_AREA_KINDS = {
    "city", "town", "village", "hamlet", "suburb", "neighbourhood", "quarter", "locality",
    "administrative", "state", "county", "district", "region", "municipality", "tehsil",
    "residential", "farmland", "farm", "farmyard", "yes",
}
_KIND_ZOOM = {
    "administrative": 11, "state": 8, "county": 10, "district": 10, "region": 9, "city": 12, "municipality": 12,
    "town": 13, "tehsil": 11, "village": 14, "hamlet": 15, "suburb": 14, "neighbourhood": 15, "quarter": 15,
    "locality": 15, "residential": 16, "farmland": 16, "farm": 16,
}


def _locationiq_search(api_key, q, country=""):
    params = {
        "key": api_key, "q": q, "format": "json", "limit": 5,
        "addressdetails": 1, "normalizecity": 1, "dedupe": 1, "accept-language": "en",
    }
    if country:
        params["countrycodes"] = country
    return requests.get("https://us1.locationiq.com/v1/search", params=params, timeout=10)


def _search_once(api_key, q, country=""):
    try:
        resp = _locationiq_search(api_key, q, country)
        if resp.status_code == 429:
            time.sleep(1.2)
            resp = _locationiq_search(api_key, q, country)
    except Exception:
        return [], "network_error"
    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            return [], "error:Place search gave an unclear reply. Please try again."
        if data and isinstance(data, list):
            return data, "ok"
        return [], "not_found"
    if resp.status_code == 404:
        return [], "not_found"
    if resp.status_code in (401, 403):
        return [], "error:Place search was refused because its key is not valid. Please ask your admin to check it."
    if resp.status_code == 429:
        return [], "error:Place search has reached its free limit for now. Please try again later."
    return [], "error:Place search did not work. Please try again later."


def geocode_candidates(query):
    if not query or not query.strip():
        return [], "empty", None
    query_str = " ".join(query.strip().split())

    coord_match = re.match(r"^\s*(-?\d+\.?\d*)[,\s]+(-?\d+\.?\d*)\s*$", query_str)
    if coord_match:
        try:
            lat = float(coord_match.group(1))
            lon = float(coord_match.group(2))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                name = f"{lat:.6f}, {lon:.6f}"
                return [{"lat": lat, "lon": lon, "name": name, "kind": "coordinates", "zoom": 17,
                         "is_area": False, "precision": "exact", "matched_query": query_str, "label": name}], "success", None
        except ValueError:
            pass

    try:
        api_key = st.secrets["LOCATIONIQ_KEY"]
    except Exception:
        return [], "error", "Place search is not set up yet. Please ask your admin to add the place search key."

    attempts = []

    def add(q, country, precision):
        item = (q, country, precision)
        if q and item not in attempts:
            attempts.append(item)

    cleaned = " ".join(GEO_NOISE.sub(" ", query_str).split())
    first_word = query_str.split(" ")[0]
    if GEO_COUNTRY_BIAS:
        add(query_str, GEO_COUNTRY_BIAS, "exact")
    add(query_str, "", "exact")
    if cleaned and cleaned.lower() != query_str.lower():
        if GEO_COUNTRY_BIAS:
            add(cleaned, GEO_COUNTRY_BIAS, "near")
        add(cleaned, "", "near")
    if first_word and first_word.lower() not in (query_str.lower(), cleaned.lower()):
        add(first_word, GEO_COUNTRY_BIAS, "area")

    last_status = "not_found"
    for i, (q, country, precision) in enumerate(attempts):
        if i:
            time.sleep(0.55)
        items, status = _search_once(api_key, q, country)
        if status == "network_error":
            return [], "network_error", None
        if status.startswith("error:"):
            return [], "error", status[len("error:"):]
        if status != "ok":
            last_status = status
            continue

        cands, seen = [], set()
        for item in items:
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, ValueError, TypeError):
                continue
            key = (round(lat, 4), round(lon, 4))
            if key in seen:
                continue
            seen.add(key)
            kind = str(item.get("type") or item.get("class") or "").lower()
            name = str(item.get("display_name", q))
            label = name if len(name) <= 95 else name[:92] + "..."
            cands.append({
                "lat": lat, "lon": lon, "name": name, "kind": kind,
                "zoom": _KIND_ZOOM.get(kind, 16), "is_area": kind in _AREA_KINDS,
                "precision": precision, "matched_query": q,
                "label": f"{label}  [{kind}]" if kind else label,
            })
        if cands:
            return cands, "success", None
        last_status = "not_found"

    return [], last_status, None


def geocode_location(query):
    cands, status, msg = geocode_candidates(query)
    if status == "success" and cands:
        c = cands[0]
        return c["lat"], c["lon"], c["name"], "success"
    return None, None, msg, status


def apply_geo_candidate(c):
    st.session_state.map_center = [c["lat"], c["lon"]]
    st.session_state.map_zoom = c["zoom"]
    st.session_state["map_nonce"] = st.session_state.get("map_nonce", 0) + 1
    st.session_state.search_marker = (c["lat"], c["lon"], c["name"])
    lat_r, lon_r = round(float(c["lat"]), 6), round(float(c["lon"]), 6)
    st.session_state.last_captured_lat = lat_r
    st.session_state.last_captured_lon = lon_r
    st.session_state["bound_v_lat"] = lat_r
    st.session_state["bound_v_lon"] = lon_r


def _clear_map_search():
    st.session_state["map_search_input"] = ""
    st.session_state["last_search_query"] = ""
    st.session_state["geo_candidates"] = []


PEST_ACCESSIBLE_INFO = {
    "rust": {
        "name": "Corn Leaf Rust",
        "name_ur": "مئی کا زنگ (Corn Rust)",
        "icon": "🌽",
        "desc": "A fungus disease that makes reddish-brown spots on the leaves.",
        "desc_ur": "پودوں کے پتوں پر سرخ مائل بھورے دھبے بنانے والی فنگس بیماری۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Spray a fungus medicine (Mancozeb or Azoxystrobin) and do not let the leaves stay wet for long.",
        "treatment_ur": "فنگس کی دوا (مینکوزیب یا ازوکسی سٹروبن) سپرے کریں اور پتوں کو زیادہ دیر گیلا نہ رہنے دیں۔"
    },
    "blight": {
        "name": "Leaf Blight Disease",
        "name_ur": "پتوں کا جھلساؤ (Blight)",
        "icon": "🌾",
        "desc": "A fungus infection that dries the leaves and leaves grey-brown patches.",
        "desc_ur": "پتوں کو خشک اور بھورا کر دینے والی شدید فنگل انفیکشن۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Spray a fungus medicine (Copper oxychloride or Propiconazole).",
        "treatment_ur": "فنگس کی دوا (کاپر آکسی کلورائیڈ یا پروپیکونازول) سپرے کریں۔"
    },
    "mildew": {
        "name": "Powdery Mildew",
        "name_ur": "سفید پھپھوندی (Mildew)",
        "icon": "🌫️",
        "desc": "White powder-like patches that cover the top of the leaves.",
        "desc_ur": "پتے کی سطح پر سفید پاؤڈر نما دھبے بننا۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Spray sulfur-based medicine or Neem oil.",
        "treatment_ur": "گندھک والی دوا یا نیم کا تیل سپرے کریں۔"
    },
    "stink bug": {
        "name": "Spotted Stink Bug",
        "name_ur": "بدبودار کیڑا (Stink Bug)",
        "icon": "🪲",
        "desc": "A shield-shaped bug that sucks the juice out of leaves and fruits.",
        "desc_ur": "پتوں اور پھلوں سے رس چوسنے والا ڈھال نما کیڑا۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Spray a pest-killing medicine (Pyrethroid) or a natural, plant-based pest spray.",
        "treatment_ur": "کیڑے مار دوا (پائریتھرائیڈ) یا پودوں سے بنا قدرتی سپرے کریں۔"
    },
    "locust": {
        "name": "Locust Swarm",
        "name_ur": "ٹڈی دل (Locust)",
        "icon": "🦗",
        "desc": "A swarm of insects that eats the leaves and finishes a crop very fast.",
        "desc_ur": "فصلوں کے پتوں اور تنے کو منٹوں میں تباہ کرنے والا کیڑا۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Spray a strong pest-killing medicine (Organophosphate) from the air, in very small amounts, only on the affected spots.",
        "treatment_ur": "ہوا سے بہت کم مقدار میں تیز کیڑے مار دوا (آرگینو فاسفیٹ) صرف متاثرہ جگہوں پر سپرے کریں۔"
    },
    "thrips": {
        "name": "Thrips Pest",
        "name_ur": "تھریپس (Thrips)",
        "icon": "🪰",
        "desc": "Very tiny insects that leave silvery marks on leaves and make them curl.",
        "desc_ur": "پتوں کو موڑنے اور چاندی نما دھبے بنانے والے باریک کیڑے۔",
        "risk_badge": "🟡 Low Risk",
        "treatment": "Spray a pest-killing medicine (Imidacloprid or Spinosad).",
        "treatment_ur": "کیڑے مار دوا (امیڈاکلوپرڈ یا سپینوساڈ) سپرے کریں۔"
    },
    "bollworm": {
        "name": "Cotton Bollworm",
        "name_ur": "امریکی سنڈی (Bollworm)",
        "icon": "🐛",
        "desc": "A caterpillar that eats the leaves, buds and fruit of the crop.",
        "desc_ur": "پتوں اور ڈوڈیوں کو کھانے والی خطرناک سنڈی۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Spray a pest-killing medicine (Emamectin benzoate or Chlorantraniliprole).",
        "treatment_ur": "کیڑے مار دوا (ایمامیکٹن بینزوایٹ یا کلورانٹرانیلیپرول) سپرے کریں۔"
    },
    "aphid": {
        "name": "Aphid Colony",
        "name_ur": "سست تیلہ (Aphids)",
        "icon": "🐜",
        "desc": "Small insects that suck the juice from the plant, turning it yellow and slowing its growth.",
        "desc_ur": "رس چوسنے والے چھوٹے کیڑے جو فصل کو زرد کر دیتے ہیں۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Spray a pest-killing medicine (Acetamiprid) or let ladybirds eat the insects naturally.",
        "treatment_ur": "کیڑے مار دوا (ایسیٹامپرڈ) سپرے کریں، یا لیڈی برڈ کو قدرتی طور پر ان کیڑوں کو کھانے دیں۔"
    },
    "borer": {
        "name": "Stem Borer",
        "name_ur": "تنے کی سنڈی (Stem Borer)",
        "icon": "🐛",
        "desc": "A worm that makes holes inside the plant stem and hollows it out.",
        "desc_ur": "پودے کے تنے کے اندر سوراخ کر کے کھوکھلا کرنے والی سنڈی۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Use a pest-killing medicine (Cartap hydrochloride, or Carbofuran granules put near the plant).",
        "treatment_ur": "کیڑے مار دوا (کارٹاپ ہائیڈروکلورائیڈ، یا پودے کے قریب کاربوفیوران کے دانے) استعمال کریں۔"
    },
    "fall armyworm": {
        "name": "Fall Armyworm",
        "name_ur": "فال آرمی ورم (Fall Armyworm)",
        "icon": "🐛",
        "desc": "A dangerous caterpillar that eats the middle part of maize, sorghum and sugarcane plants.",
        "desc_ur": "مکئی اور دیگر فصلوں کے درمیانی حصے کو کھانے والی خطرناک سنڈی۔",
        "risk_badge": "🔴 High Risk",
        "treatment": "Spray a pest-killing medicine (Spinetoram or Chlorantraniliprole) into the middle of the plant, where the new leaves grow.",
        "treatment_ur": "کیڑے مار دوا (سپینیٹورام یا کلورانٹرانیلیپرول) پودے کے بیچ میں سپرے کریں جہاں نئے پتے نکلتے ہیں۔"
    },
    "chlorosis": {
        "name": "Yellowing Leaves (Lack of Plant Food)",
        "name_ur": "پتوں کا پیلا پن (غذا کی کمی)",
        "icon": "🍂",
        "desc": "Leaves turn yellow because the plant is not getting enough food, or its roots are weak.",
        "desc_ur": "غذائی اجزاء کی کمی یا جڑوں میں خرابی کی وجہ سے پتوں کا پیلا ہونا۔",
        "risk_badge": "🟡 Low Risk",
        "treatment": "Put Urea fertilizer in the soil near the plants and spray a leaf feeding mix (small-quantity plant nutrients).",
        "treatment_ur": "پودوں کے قریب مٹی میں یوریا کھاد ڈالیں اور پتوں پر غذائی اجزاء کا سپرے کریں۔"
    },
    "necrosis": {
        "name": "Dead Brown Spots",
        "name_ur": "مرجھایا ہوا خلیہ (Necrosis)",
        "icon": "🟤",
        "desc": "Dry, dead brown spots on leaves caused by too little water or a fungus.",
        "desc_ur": "خشک سالی یا شدید فنگس سے پودے کے خلیات کا مٹ جانا۔",
        "risk_badge": "🟠 Medium Risk",
        "treatment": "Water the field properly and spray a natural fungus medicine.",
        "treatment_ur": "کھیت کو صحیح طرح پانی دیں اور قدرتی فنگس کی دوا سپرے کریں۔"
    }
}


def pest_catalog_key(raw):
    s = str(raw or "").strip().lower().replace("_", " ").replace("-", " ")
    if not s:
        return None
    for key in sorted(PEST_ACCESSIBLE_INFO, key=len, reverse=True):
        if key in s:
            return key
    return None


def friendly_pest_name(raw):
    key = pest_catalog_key(raw)
    if key:
        return PEST_ACCESSIBLE_INFO[key]["name_ur" if lang == "ur" else "name"]
    s = str(raw or "").strip()
    parts = s.split("_", 1)
    if len(parts) == 2 and " " not in parts[0] and parts[1].strip():
        s = parts[1]
    return s.replace("_", " ").replace("-", " ").strip().title()


def friendly_pest_list(items):
    names = []
    for x in items or []:
        n = friendly_pest_name(x)
        if n and n not in names:
            names.append(n)
    return names


def friendly_pest_text(value):
    if not value:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(friendly_pest_list(value))
    return ", ".join(friendly_pest_list([p.strip() for p in str(value).split(",") if p.strip()]))


TRANSLATIONS = {
    "en": {
        "app_title": "AgriVision",
        "app_subtitle": "Find crop pests with a drone and spray only where needed",
        "nav_home": "🏠 Home / Overview",
        "nav_visual": "📊 Visual Report",
        "nav_gps": "📍 Field Map & Location",
        "nav_problems": "🔍 Problem Areas",
        "nav_spray": "💦 Spray Advisory",
        "nav_pests": "🐛 Pest Catalog",
        "nav_settings": "⚙️ Settings",

        "hero_title": "Smart Drone Crop Protection Platform",
        "hero_desc": "Your drone video is checked automatically to find pests and unhealthy plants. It then shows you exactly where to spray and how much medicine to use.",

        "metric_total_zones": "Total Field Zones",
        "metric_problem_zones": "Problem Zones",
        "metric_spray_zones": "Zones Needing Spray",
        "metric_health_status": "Overall Crop Status",

        "status_healthy": "✅ Healthy Field",
        "status_action_needed": "⚠️ Action Required",

        "upload_heading": "📹 Upload Video & Check Your Field",
        "upload_label": "Upload Drone / Field Video (.mp4, .avi, .mov)",
        "demo_select_label": "Or Select Demo Video",
        "btn_run_pipeline": "▶️ Start Field Check",
        "processing_msg": "Checking your drone video, picture by picture... Please wait.",

        "empty_title": "No Results Yet",
        "empty_desc": "Please upload a video and click 'Start Field Check' on the Home page to see your full crop report.",
        "btn_go_home": "👉 Go to Home Page",

        "report_ready_msg": "Field check complete! See the details in the menu.",
        "view_full_report": "View Full Crop Report ➔",

        "no_issue": "No issue detected",
        "no_pests": "No pest detected",
        "sev_none": "Healthy",
        "sev_low": "Low",
        "sev_medium": "Medium",
        "sev_high": "High",
        "clean_canopy": "Crop looks healthy and green. No pests or yellow leaves found.",

        "table_zone": "Zone",
        "table_lat": "Map Position (North-South)",
        "table_lon": "Map Position (East-West)",
        "table_dist": "Distance from Field Centre",
        "table_geofence": "Inside Your Field?",
        "table_alt": "Drone Flying Height",
        "table_severity": "Problem Level",
        "table_spray_status": "What To Do",
        "table_dose": "Spray Amount (litres per hectare)",
        "table_chemical": "Medicine To Use",
        "table_drone_flow": "Spray Speed (litres per minute)",

        "geofence_inside": "🟢 INSIDE YOUR FIELD",
        "geofence_outside": "🔴 OUTSIDE YOUR FIELD",

        "settings_title": "Settings",
        "theme_toggle_label": "🎨 Interface Theme",
        "theme_light": "Light Mode",
        "theme_dark": "Dark Mode",
        "lang_toggle_label": "🌐 Interface Language",
        "lang_en": "English",
        "lang_ur": "اردو",
        "geofence_setup": "🗺️ Field Centre Location",
        "center_lat": "Centre Position (North-South)",
        "center_lon": "Centre Position (East-West)",
        "geofence_radius": "Field Distance Limit (metres)"
    },
    "ur": {
        "app_title": "ایگری ویژن",
        "app_subtitle": "ڈراؤن کے ذریعے فصلوں کی دیکھ بھال اور کیڑوں کی تشخیص",
        "nav_home": "🏠 ہوم / جائزہ",
        "nav_visual": "📊 تصویری رپورٹ",
        "nav_gps": "📍 کھیت کا نقشہ اور مقام",
        "nav_problems": "🔍 متاثرہ علاقے",
        "nav_spray": "💦 سپرے مشورہ",
        "nav_pests": "🐛 کیڑوں کی معلومات",
        "nav_settings": "⚙️ سیٹنگز",

        "hero_title": "سمارٹ ڈرون کراپ پروٹیکشن سسٹم",
        "hero_desc": "آپ کی ڈرون ویڈیو خود بخود چیک ہوتی ہے تاکہ کیڑے اور بیمار پودے مل جائیں، اور پھر بتایا جاتا ہے کہ سپرے کہاں اور کتنا کرنا ہے۔",

        "metric_total_zones": "کل کھیت زونز",
        "metric_problem_zones": "متاثرہ زونز",
        "metric_spray_zones": "سپرے کی ضرورت والے زون",
        "metric_health_status": "فصل کی مجموعی صورتحال",

        "status_healthy": "✅ فصل بالکل ٹھیک ہے",
        "status_action_needed": "⚠️ فوری توجہ درکار ہے",

        "upload_heading": "📹 ویڈیو اپ لوڈ کریں اور اپنے کھیت کی جانچ کریں",
        "upload_label": "ڈرون یا کھیت کی ویڈیو اپ لوڈ کریں (.mp4, .avi, .mov)",
        "demo_select_label": "یا ڈیمو ویڈیو منتخب کریں",
        "btn_run_pipeline": "▶️ کھیت کی جانچ شروع کریں",
        "processing_msg": "ڈرون ویڈیو کی جانچ ہو رہی ہے... براہ کرم انتظار کریں۔",

        "empty_title": "فی الحال کوئی رپورٹ موجود نہیں ہے",
        "empty_desc": "رپورٹ دیکھنے کے لیے براہ کرم ہوم پیج پر جا کر ویڈیو اپ لوڈ کریں اور 'کھیت کی جانچ شروع کریں' دبائیں۔",
        "btn_go_home": "👉 ہوم پیج پر جائیں",

        "report_ready_msg": "جانچ مکمل ہو گئی! تمام نتائج دیکھنے کے لیے سائڈبار مینو استعمال کریں۔",
        "view_full_report": "مکمل رپورٹ دیکھیں ➔",

        "no_issue": "کوئی مسئلہ نہیں پایا گیا",
        "no_pests": "کوئی کیڑا نہیں ملا",
        "sev_none": "صحت مند",
        "sev_low": "کم",
        "sev_medium": "درمیانہ",
        "sev_high": "شدید",
        "clean_canopy": "فصل صحت مند اور سبز ہے۔ کیڑوں یا بیماری کی کوئی علامت نہیں ہے۔",

        "table_zone": "زون",
        "table_lat": "نقشے پر مقام (شمال-جنوب)",
        "table_lon": "نقشے پر مقام (مشرق-مغرب)",
        "table_dist": "کھیت کے مرکز سے فاصلہ (میٹر)",
        "table_geofence": "کیا آپ کے کھیت کے اندر ہے؟",
        "table_alt": "ڈرون کی اڑان کی اونچائی",
        "table_severity": "مسئلے کی سطح",
        "table_spray_status": "کیا کرنا ہے",
        "table_dose": "مقدار (لیٹر فی ہیکٹر)",
        "table_chemical": "دوائی کا نام",
        "table_drone_flow": "سپرے کی رفتار (لیٹر فی منٹ)",

        "geofence_inside": "🟢 آپ کے کھیت کے اندر",
        "geofence_outside": "🔴 آپ کے کھیت سے باہر",

        "settings_title": "سیٹنگز",
        "theme_toggle_label": "🎨 انٹرفیس تھیم",
        "theme_light": "لائٹ موڈ",
        "theme_dark": "ڈارک موڈ",
        "lang_toggle_label": "🌐 زبان",
        "lang_en": "English",
        "lang_ur": "اردو",
        "geofence_setup": "🗺️ کھیت کے مرکز کا مقام",
        "center_lat": "مرکزی مقام (شمال-جنوب)",
        "center_lon": "مرکزی مقام (مشرق-مغرب)",
        "geofence_radius": "کھیت کی حد (میٹر)"
    }
}


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
UR = {
 "🔴 High Risk": "🔴 زیادہ خطرہ",
 "🟠 Medium Risk": "🟠 درمیانہ خطرہ",
 "🟡 Low Risk": "🟡 کم خطرہ",
 "General crop protection spray": "فصل کی حفاظت کا عمومی سپرے",
 "Leaf feeding spray and fungus medicine": "پتوں کو غذا دینے والا سپرے اور فنگس کی دوا",
 "Pests found: {p}": "ملنے والے کیڑے: {p}",
 "Yellow leaves ({p:.1f}%)": "پیلے پتے ({p:.1f}%)",
 "Dead brown spots ({p:.1f}%)": "مردہ بھورے دھبے ({p:.1f}%)",
 "Plants are wilting": "پودے مرجھا رہے ہیں",
 "Crop looks stressed": "فصل دباؤ میں لگتی ہے",
 "Problem level: {lvl}. {s}.": "مسئلے کی سطح: {lvl}۔ {s}۔",
 "🔴 Pending Spray": "🔴 سپرے باقی ہے",
 "🟡 Spraying Active": "🟡 سپرے جاری ہے",
 "🟢 Sprayed (Completed)": "🟢 سپرے مکمل",
 "Spray Required": "سپرے ضروری ہے",
 "Spraying In Progress": "سپرے جاری ہے",
 "Sprayed": "سپرے ہو چکا",
 "Swipe sideways to see more": "مزید دیکھنے کے لیے سائیڈ پر سوائپ کریں",
 "SMART FARMING HELPER": "کھیتی کا سمارٹ مددگار",
 "✅ Field Healthy": "✅ کھیت ٹھیک ہے",
 "⚠️ {n} Problem Zones": "⚠️ {n} زونز میں مسئلہ ہے",
 "Video checked:": "جانچی گئی ویڈیو:",
 "🛰️ DRONE CROP WATCH": "🛰️ ڈرون سے فصل کی نگرانی",
 "🌱 SMART PEST & CROP HEALTH CHECK": "🌱 کیڑوں اور فصل کی صحت کی جانچ",
 "💦 RIGHT AMOUNT OF SPRAY": "💦 سپرے کی صحیح مقدار",
 "-- Select Demo --": "-- ڈیمو منتخب کریں --",
 "Selected video:": "منتخب ویڈیو:",
 "🗑️ Remove": "🗑️ ہٹائیں",
 "Please mark your field on the map first (Field Map & Location page). The check is done only inside your field.": "پہلے نقشے پر اپنا کھیت نشان زد کریں (کھیت کا نقشہ اور مقام والا صفحہ)۔ جانچ صرف آپ کے کھیت کے اندر ہوگی۔",
 "Checking picture {c} of {n} ({p}%)...": "تصویر {c} از {n} کی جانچ ہو رہی ہے ({p}%)...",
 "✅ Field check finished successfully.": "✅ کھیت کی جانچ کامیابی سے مکمل ہو گئی۔",
 "Something went wrong while checking your video. Please try again, or try a different video.": "ویڈیو کی جانچ کے دوران کوئی مسئلہ پیش آ گیا۔ براہ کرم دوبارہ کوشش کریں، یا کوئی اور ویڈیو آزمائیں۔",
 "Details for the support team": "مدد کرنے والی ٹیم کے لیے تفصیل",
 "Your Crop Report Is Ready": "آپ کی فصل کی رپورٹ تیار ہے",
 "See the marked video, field map, problem areas and spray advice from the menu on the left.": "بائیں طرف مینو سے نشان زد ویڈیو، کھیت کا نقشہ، مسئلے والے حصے اور سپرے کا مشورہ دیکھیں۔",
 "📊 Marked Video & Field Summary": "📊 نشان زد ویڈیو اور کھیت کا خلاصہ",
 "⬇️ Download Marked Video": "⬇️ نشان زد ویڈیو ڈاؤن لوڈ کریں",
 "The marked video could not be found. Please run the Field Check again.": "نشان زد ویڈیو نہیں ملی۔ براہ کرم کھیت کی جانچ دوبارہ کریں۔",
 "Field Health at a Glance (9 Zones)": "ایک نظر میں کھیت کی صحت (9 زونز)",
 "ZONE": "زون",
 "Pictures Checked from Video:": "ویڈیو سے جانچی گئی تصویریں:",
 "Zone 1 to Zone 9 always mean the same part of the field, even when the drone moves.": "زون 1 سے زون 9 ہمیشہ کھیت کے ایک ہی حصے کو ظاہر کرتے ہیں، چاہے ڈرون حرکت کرے۔",
 "The video was too shaky or blurry in {n} place(s), so those parts may be a little less accurate. A clearer, steadier video gives better results.": "ویڈیو {n} جگہوں پر ہلتی ہوئی یا دھندلی تھی، اس لیے ان حصوں کے نتائج تھوڑے کم درست ہو سکتے ہیں۔ صاف اور ٹھہری ہوئی ویڈیو سے بہتر نتائج ملتے ہیں۔",
 "📍 Field Map & Location": "📍 کھیت کا نقشہ اور مقام",
 "Mark your field on the map. After the field check, the problem spots, the spray area and the spray patches are shown automatically.": "نقشے پر اپنا کھیت نشان زد کریں۔ جانچ کے بعد مسئلے والی جگہیں، سپرے کا علاقہ اور سپرے کے چھوٹے حصے خود بخود دکھائے جائیں گے۔",
 "Edge Gap (metres)": "کنارے سے فاصلہ (میٹر)",
 "How far the spray area stays away from the edge of your field.": "سپرے کا علاقہ کھیت کے کنارے سے کتنا دور رہے گا۔",
 "Spray Patch Size (metres)": "سپرے کے حصے کا سائز (میٹر)",
 "Size of each small square patch that gets sprayed.": "ہر چھوٹے چوکور حصے کا سائز جس پر سپرے ہوگا۔",
 "You changed your field outline after the last check. Please run the Field Check again on the Home page so the results match your new outline.": "آپ نے پچھلی جانچ کے بعد کھیت کا خاکہ بدل دیا ہے۔ براہ کرم ہوم پیج پر کھیت کی جانچ دوبارہ کریں تاکہ نتائج نئے خاکے کے مطابق ہوں۔",
 "🗺️ Field Map (Tap to Open)": "🗺️ کھیت کا نقشہ (کھولنے کے لیے دبائیں)",
 "Search address or location...": "پتہ یا جگہ تلاش کریں...",
 "Search Location": "جگہ تلاش کریں",
 "Map moved to:": "نقشہ یہاں منتقل ہوا:",
 "⚠️ Place search is not working right now. Please check your internet connection, or type the location numbers yourself (for example 33.5673, 73.1015).": "⚠️ جگہ کی تلاش ابھی کام نہیں کر رہی۔ براہ کرم انٹرنیٹ چیک کریں، یا جگہ کے نمبر خود لکھیں (مثلاً 33.5673, 73.1015)۔",
 "❌ Location '{q}' could not be found. Please type only the place name (for example Sahiwal, Pakistan), or type the location numbers yourself (for example 33.5673, 73.1015).": "❌ جگہ '{q}' نہیں ملی۔ براہ کرم صرف جگہ کا نام لکھیں (مثلاً Sahiwal, Pakistan)، یا جگہ کے نمبر خود لکھیں (مثلاً 33.5673, 73.1015)۔",
 "Satellite view is not switched on, so the normal map is shown. (Ask your admin to add the satellite map key.)": "سیٹلائٹ منظر آن نہیں ہے، اس لیے عام نقشہ دکھایا گیا ہے۔ (سیٹلائٹ نقشے کی چابی لگانے کے لیے اپنے ایڈمن سے کہیں۔)",
 "🌿 Your Field (drone flying area)": "🌿 آپ کا کھیت (ڈرون کے اڑنے کا علاقہ)",
 "Corner": "کونا",
 "Zone": "زون",
 "Spray area (made automatically)": "سپرے کا علاقہ (خود بخود بنا)",
 "Problem spot": "مسئلے والی جگہ",
 "🔍 Searched Location:": "🔍 تلاش کی گئی جگہ:",
 "Location picked from the map:": "نقشے سے منتخب مقام:",
 "North-South": "شمال-جنوب",
 "East-West": "مشرق-مغرب",
 "📐 1. Your Field Outline": "📐 1. آپ کے کھیت کا خاکہ",
 "Mark the outline of the field the drone flies over. Results are shown only inside this outline. Add corner points one after another around the field.": "اس کھیت کا خاکہ بنائیں جس پر ڈرون اڑے گا۔ نتائج صرف اس خاکے کے اندر دکھائے جائیں گے۔ کھیت کے گرد ایک کے بعد ایک کونے کے نقطے شامل کریں۔",
 "Corner Points of Your Field:": "آپ کے کھیت کے کونے:",
 "Name": "نام",
 "No field outline yet. Draw a box or shape on the map and press the green button, or add corner points yourself.": "ابھی کھیت کا خاکہ نہیں بنا۔ نقشے پر ڈبہ یا شکل بنائیں اور سبز بٹن دبائیں، یا کونے خود شامل کریں۔",
 "🟩 Use drawn shape as my field ({n} corners)": "🟩 بنائی گئی شکل کو میرا کھیت بنائیں ({n} کونے)",
 "Field outline updated from the shape you drew.": "آپ کی بنائی گئی شکل سے کھیت کا خاکہ اپ ڈیٹ ہو گیا۔",
 "Draw a box or shape on the map above to use it as your field outline.": "اوپر نقشے پر ڈبہ یا شکل بنائیں تاکہ اسے اپنے کھیت کا خاکہ بنایا جا سکے۔",
 "Add a Corner Point:": "کونے کا نقطہ شامل کریں:",
 "Corner Name": "کونے کا نام",
 "Click on the map": "نقشے پر کلک کریں",
 "Position (North-South)": "مقام (شمال-جنوب)",
 "Position (East-West)": "مقام (مشرق-مغرب)",
 "➕ Add Corner": "➕ کونا شامل کریں",
 "🗑️ Clear Outline": "🗑️ خاکہ مٹائیں",
 "Click a point on the map, or type both location numbers first.": "پہلے نقشے پر کوئی نقطہ دبائیں، یا دونوں مقام کے نمبر لکھیں۔",
 "🎯 2. Problem Spots Found Automatically": "🎯 2. خود بخود ملنے والی مسئلے کی جگہیں",
 "Marked automatically from the video results inside your field. Nothing to mark by hand.": "ویڈیو کے نتائج سے آپ کے کھیت کے اندر خود بخود نشان زد ہوئیں۔ ہاتھ سے کچھ نشان زد کرنے کی ضرورت نہیں۔",
 "Run the Field Check on the Home page to see problem spots.": "مسئلے والی جگہیں دیکھنے کے لیے ہوم پیج پر کھیت کی جانچ چلائیں۔",
 "Mark your field outline above to see the results on the map.": "نقشے پر نتائج دیکھنے کے لیے اوپر اپنے کھیت کا خاکہ بنائیں۔",
 "No problem spots found inside your field.": "آپ کے کھیت کے اندر کوئی مسئلے والی جگہ نہیں ملی۔",
 "Spot": "جگہ",
 "Pests Found": "ملنے والے کیڑے",
 "💦 3. Spray Area & Spray Patches": "💦 3. سپرے کا علاقہ اور سپرے کے حصے",
 "A spray area is drawn around the problem spots and divided into small square patches. Only these patches are sprayed.": "مسئلے والی جگہوں کے گرد سپرے کا علاقہ بنتا ہے جسے چھوٹے چوکور حصوں میں بانٹا جاتا ہے۔ سپرے صرف انہی حصوں پر ہوگا۔",
 "Mark your field outline and run the Field Check to see the spray area and patches.": "سپرے کا علاقہ اور حصے دیکھنے کے لیے کھیت کا خاکہ بنائیں اور کھیت کی جانچ چلائیں۔",
 "No problem area was found, so no spray area is needed.": "کوئی مسئلے والا علاقہ نہیں ملا، اس لیے سپرے کے علاقے کی ضرورت نہیں۔",
 "💡 **Spray area:** {a:.0f} out of {b:.0f} square metres | **Spray patches:** {n} | **Patch size:** {c} metres": "💡 **سپرے کا علاقہ:** {b:.0f} مربع میٹر میں سے {a:.0f} | **سپرے کے حصے:** {n} | **حصے کا سائز:** {c} میٹر",
 " (made bigger than the {r:.0f} metres you chose, to keep the number of patches reasonable)": " (آپ کے چنے ہوئے {r:.0f} میٹر سے بڑا رکھا گیا تاکہ حصوں کی تعداد مناسب رہے)",
 "Patch": "حصہ",
 "Spray Amount": "سپرے کی مقدار",
 "Spray Status": "سپرے کی حالت",
 "Update Patch Spray Status:": "حصے کی سپرے حالت بدلیں:",
 "Select Patch:": "حصہ منتخب کریں:",
 "Set Spray Status:": "سپرے کی حالت منتخب کریں:",
 "💾 Save Patch Status": "💾 حالت محفوظ کریں",
 "📍 4. Zone Locations": "📍 4. زونز کے مقام",
 "Run the Field Check on the Home page to see the location of every zone.": "ہر زون کا مقام دیکھنے کے لیے ہوم پیج پر کھیت کی جانچ چلائیں۔",
 "Mark your field outline above to see the location of each zone.": "ہر زون کا مقام دیکھنے کے لیے اوپر اپنے کھیت کا خاکہ بنائیں۔",
 "4.5 m (set automatically)": "4.5 میٹر (خود بخود طے)",
 "⬇️ Download Zone Locations (opens in Excel)": "⬇️ زونز کے مقام ڈاؤن لوڈ کریں (ایکسل میں کھلے گی)",
 "🔍 Problem Areas": "🔍 مسئلے والے حصے",
 "Zone-by-zone details, shown on colour-coded cards.": "زون بہ زون تفصیل، رنگین کارڈز پر۔",
 "C001, C002, ... are small spray patches (little squares of the field) in each zone. Red = still to be sprayed. Hover over a patch to see its status.": "C001، C002، ... ہر زون کے سپرے والے چھوٹے حصے (کھیت کے چھوٹے چوکور ٹکڑے) ہیں۔ سرخ = ابھی سپرے ہونا باقی ہے۔ حالت دیکھنے کے لیے حصے پر ماؤس لے جائیں۔",
 "Pests Found:": "ملنے والے کیڑے:",
 "Crop Area Affected:": "متاثرہ فصل کا حصہ:",
 "Spray Patches": "سپرے کے حصے",
 "💦 Spray Advice & Medicine Amount": "💦 سپرے کا مشورہ اور دوا کی مقدار",
 "Zones Needing Spray or Care": "سپرے یا دیکھ بھال والے زونز",
 "Total Spray Amount (Estimate)": "سپرے کی کل مقدار (اندازاً)",
 "litres/hectare": "لیٹر فی ہیکٹر",
 "✅ All Field Areas are Healthy": "✅ کھیت کے تمام حصے ٹھیک ہیں",
 "No zone needs spraying or care.": "کسی زون کو سپرے یا دیکھ بھال کی ضرورت نہیں۔",
 "SPRAY NEEDED": "سپرے ضروری ہے",
 "KEEP WATCHING": "نظر رکھیں",
 "litres/min": "لیٹر فی منٹ",
 "🎯 Spray Patches by Zone": "🎯 زون کے حساب سے سپرے کے حصے",
 "Each small tag is one spray patch (C001, C002, ...) inside the spray area. Hover over it to see its map location.": "ہر چھوٹا ٹیگ سپرے کا ایک حصہ ہے (C001، C002، ...) جو سپرے کے علاقے کے اندر ہے۔ نقشے پر اس کی جگہ دیکھنے کے لیے اس پر ماؤس لے جائیں۔",
 "patches": "حصے",
 "Pending": "باقی",
 "Active": "جاری",
 "🐛 Crop Pest & Disease Guide": "🐛 فصل کے کیڑوں اور بیماریوں کی رہنمائی",
 "Simple explanations and advice for farmers.": "کسانوں کے لیے آسان وضاحت اور مشورے۔",
 "Pests Found in Your Video:": "آپ کی ویڈیو میں ملنے والے کیڑے:",
 "What To Do:": "کیا کرنا ہے:",
 "Select Theme Mode:": "تھیم منتخب کریں:",
 "Select Language:": "زبان منتخب کریں:",
 "Place search is not set up yet. Please ask your admin to add the place search key.": "جگہ کی تلاش ابھی سیٹ اپ نہیں ہے۔ براہ کرم اپنے ایڈمن سے تلاش کی چابی لگانے کو کہیں۔",
 "Place search gave an unclear reply. Please try again.": "جگہ کی تلاش کا جواب سمجھ نہیں آیا۔ براہ کرم دوبارہ کوشش کریں۔",
 "Place search was refused because its key is not valid. Please ask your admin to check it.": "جگہ کی تلاش اس لیے رد ہوئی کہ اس کی چابی درست نہیں۔ براہ کرم اپنے ایڈمن سے چیک کرائیں۔",
 "Place search has reached its free limit for now. Please try again later.": "جگہ کی تلاش اپنی مفت حد تک پہنچ گئی ہے۔ براہ کرم کچھ دیر بعد کوشش کریں۔",
 "Place search did not work. Please try again later.": "جگہ کی تلاش کام نہیں کر سکی۔ براہ کرم بعد میں کوشش کریں۔",
 "Several places matched. Pick the correct one:": "کئی جگہیں ملی ہیں۔ صحیح جگہ منتخب کریں:",
 "⚠️ The exact place '{q}' was not found, so the pin shows the approximate area of '{m}'. Tap the map at your field to set the exact point, or type the location numbers (for example 33.5673, 73.1015).": "⚠️ صحیح جگہ '{q}' نہیں ملی، اس لیے پن '{m}' کے تقریباً علاقے پر ہے۔ اپنے کھیت پر نقشے کو دبا کر درست نقطہ لگائیں، یا جگہ کے نمبر خود لکھیں (مثلاً 33.5673, 73.1015)۔",
 "ℹ️ This pin is the centre of the whole area (village / town), not your exact field. Tap the map at your field to set the exact point.": "ℹ️ یہ پن پورے علاقے (گاؤں / شہر) کے درمیان میں ہے، آپ کا درست کھیت نہیں۔ درست نقطہ لگانے کے لیے اپنے کھیت پر نقشے کو دبائیں۔"
}


def L(text):
    return UR.get(text, text) if lang == "ur" else text


UR.update({
    "Fixed 3x3 grid": "مقررہ 3x3 گرڈ", "Healthy zones": "صحت مند حصے", "Spray": "سپرے",
    "📋 Zone Details": "📋 حصوں کی تفصیل", "📈 Charts": "📈 چارٹ", "Crop affected": "متاثرہ فصل",
    "Spray amount": "سپرے کی مقدار", "Zone Details": "حصوں کی تفصیل",
})

UR.update({"⬅️ Previous": "⬅️ پچھلا", "Next ➡️": "اگلا ➡️", "Page": "صفحہ"})

UR.update({
    "🗺️ Map & Outline": "🗺️ نقشہ اور حد", "🎯 Problem Spots": "🎯 مسئلے والی جگہیں",
    "💦 Spray Patches": "💦 سپرے کے حصے", "📍 Zone Locations": "📍 حصوں کی لوکیشن",
    "⚙️ Grid settings": "⚙️ ترتیبات", "Corners": "کونے", "Problem spots": "مسئلے والی جگہیں",
    "Spray patches": "سپرے کے حصے", "Zones": "حصے", "Clear": "صاف کریں",
    "Overall Field Health": "کھیت کی مجموعی صحت", "{n} of {t} zones need attention": "{t} میں سے {n} حصوں پر توجہ کی ضرورت ہے",
    "Pests found": "پائے گئے کیڑے", "🎬 Marked Video": "🎬 نشان زدہ ویڈیو",
})


def get_outer_polygon():
    return boundary_polygon(st.session_state.field_boundary)


def boundary_signature():
    return json.dumps([(p["lat"], p["lon"]) for p in st.session_state.field_boundary])


def report_matches_boundary():
    rep = st.session_state.report
    return rep is not None and rep.get("boundary_sig") == boundary_signature()


def get_gis_state():
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
            "badge": L(badge),
            "color": color,
        })
    return out


def inject_custom_styles(theme_mode, lang_code):
    is_dark = (theme_mode == "dark")
    is_ur = (lang_code == "ur")

    if is_dark:
        bg_main = "#0F172A"
        top_bar_bg = "linear-gradient(135deg, #0F172A 0%, #12305A 55%, #064E3B 100%)"
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
        bg_main = "#EEF8EC"
        top_bar_bg = "linear-gradient(135deg, #013237 0%, #0A4A45 45%, #2E6B4E 100%)"
        page_gradient = "radial-gradient(1000px 560px at 0% -10%, #C0E6BA 0%, rgba(192,230,186,0) 62%), radial-gradient(900px 520px at 100% 0%, rgba(76,167,113,0.28) 0%, rgba(76,167,113,0) 58%), radial-gradient(900px 620px at 50% 115%, rgba(1,50,55,0.16) 0%, rgba(1,50,55,0) 60%), linear-gradient(180deg, #F2FAF0 0%, #E6F5E3 50%, #D6EDD2 100%)"
        sidebar_bg = "linear-gradient(180deg, #C0E6BA 0%, #A3D8A5 50%, #86C795 100%)"
        bg_card = "linear-gradient(145deg, #FFFFFF 0%, #F0F9EE 55%, #E1F2DE 100%)"
        bg_card_secondary = "#E6F5E3"
        text_primary = "#013237"
        text_secondary = "#2F5F58"
        border_color = "#A6D5A0"
        accent_green = "#2E6B4E"
        btn_gradient = "linear-gradient(135deg, #2E6B4E 0%, #1F7A5A 55%, #013237 100%)"
        btn_hover = "linear-gradient(135deg, #3E9163 0%, #17644F 55%, #01282C 100%)"
        table_bg = "#FFFFFF"
        table_head_gradient = "linear-gradient(135deg, #C0E6BA 0%, #8FCB98 100%)"
        table_row_alt = "#F0F9EE"
        nav_active_bg = "linear-gradient(135deg, #2E6B4E 0%, #1F7A5A 55%, #013237 100%)"
        widget_bg = "#FFFFFF"
        widget_border = "#A6D5A0"
        header_bg = "rgba(238, 248, 236, 0.88)"
        progress_track = "#CDE8C8"
        code_bg = "#E6F5E3"
        code_text = "#1F6B4A"
        alert_info = ("#DDF1EE", "#013237", "#8FCBC0")
        alert_success = ("#DDF3D8", "#013237", "#2E6B4E")
        alert_warning = ("#FEF3C7", "#92400E", "#FCD34D")
        alert_error = ("#FEE2E2", "#991B1B", "#FCA5A5")
        sev_styles = {
            "none":   ("linear-gradient(135deg, #C0E6BA 0%, #8FD19A 100%)", "#2E6B4E", "#013237"),
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


    [data-testid="stMainMenuPopover"] hr,
    [data-testid="stMainMenuPopover"] [role="separator"],
    ul[data-testid="main-menu-list"] hr {{
        display: none !important;
    }}

    .block-container {{
        padding-top: 0.5rem !important;
        padding-bottom: 2rem !important;
        max-width: 1400px;
    }}


    [data-testid="stSidebar"] {{
        background: {sidebar_bg} !important;
        border-right: 1px solid {border_color} !important;
    }}
    [data-testid="stSidebar"] [data-testid="stSidebarHeader"] {{
        background: transparent !important;
        height: 2rem !important;
        min-height: 0 !important;
        padding: 0 0.5rem !important;
        margin: 0 !important;
    }}
    [data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
        padding-top: 0 !important;
    }}
    header[data-testid="stHeader"] {{
        height: 0 !important;
        border-bottom: none !important;
        background: transparent !important;
        overflow: visible !important;
        min-height: 0 !important;
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


    .top-app-bar {{
        background: {top_bar_bg};
        color: #FFFFFF;
        padding: 28px 30px;
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
        padding-inline-start: 6px;
    }}
    .top-app-bar .top-bar-logo {{
        width: 58px;
        height: 58px;
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 34px;
        border-radius: 14px;
        background: rgba(255, 255, 255, 0.14);
        border: 1px solid rgba(255, 255, 255, 0.35);
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
    [data-testid="stPlotlyChart"] {{
        background: {bg_card};
        border: 1px solid {border_color};
        border-radius: 16px;
        padding: 8px 10px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03);
        margin-bottom: 16px;
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


    .hero-banner {{
        background: linear-gradient(135deg, #064E3B 0%, #047857 35%, #0F766E 65%, #12305A 100%);
        border-radius: 22px;
        padding: 32px 40px;
        min-height: 0;
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
    .hero-banner .hero-title-row {{
        display: flex;
        align-items: center;
        gap: 18px;
        margin-bottom: 16px;
    }}
    .hero-banner .hero-logo {{
        width: 68px;
        height: 68px;
        flex-shrink: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 40px;
        border-radius: 20px;
        background: rgba(255, 255, 255, 0.14);
        border: 1px solid rgba(255, 255, 255, 0.35);
        box-shadow: 0 8px 18px rgba(0, 0, 0, 0.25);
    }}
    .hero-banner .hero-title {{
        font-size: 38px;
        font-weight: 900;
        line-height: 1.2;
        margin-bottom: 0;
        color: #FFFFFF !important;
        -webkit-text-fill-color: #FFFFFF !important;
    }}
    .hero-banner .hero-desc {{
        font-size: 17px;
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
        padding: 16px 10px;
        text-align: center;
        min-width: 0;
        min-height: 104px;
        overflow: hidden;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }}
    .zone-tile > div {{ max-width: 100%; }}
    .zone-card:hover, .zone-tile:hover {{
        transform: translateY(-2px);
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.15);
    }}
    .zone-card *, .zone-tile * {{
        color: inherit !important;
    }}
    {sev_css}


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


    :root {{ --ease: cubic-bezier(.22, .9, .3, 1); }}
    html {{ scroll-behavior: smooth; }}
    ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    ::-webkit-scrollbar-thumb {{ background: {border_color}; border-radius: 10px; border: 2px solid transparent; background-clip: padding-box; }}
    ::-webkit-scrollbar-thumb:hover {{ background: #10B981; border: 2px solid transparent; background-clip: padding-box; }}
    ::selection {{ background: rgba(16, 185, 129, 0.3); }}
    :focus-visible {{ outline: 2px solid #10B981 !important; outline-offset: 2px; }}

    @keyframes fadeUp {{ from {{ opacity: 0; transform: translateY(14px); }} to {{ opacity: 1; transform: none; }} }}
    @keyframes pulseDot {{
        0% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }}
        70% {{ box-shadow: 0 0 0 9px rgba(16, 185, 129, 0); }}
        100% {{ box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }}
    }}
    @keyframes gradientShift {{ 0% {{ background-position: 0% 50%; }} 50% {{ background-position: 100% 50%; }} 100% {{ background-position: 0% 50%; }} }}
    @keyframes floatY {{ 0%, 100% {{ transform: translateY(0); }} 50% {{ transform: translateY(-8px); }} }}
    @keyframes shine {{ from {{ transform: translateX(-120%) skewX(-20deg); }} to {{ transform: translateX(380%) skewX(-20deg); }} }}


    .agri-card, .zone-card, .zone-tile, .empty-state-box, .top-app-bar, .hero-banner {{
        animation: fadeUp 0.55s var(--ease) backwards;
    }}
    [data-testid="stColumn"]:nth-child(2) .agri-card {{ animation-delay: 0.07s; }}
    [data-testid="stColumn"]:nth-child(3) .agri-card {{ animation-delay: 0.14s; }}
    [data-testid="stColumn"]:nth-child(4) .agri-card {{ animation-delay: 0.21s; }}
    .agri-card {{ transition: transform 0.25s var(--ease), box-shadow 0.25s var(--ease), border-color 0.25s ease; }}
    .agri-card:hover {{
        transform: translateY(-4px);
        border-color: #10B981;
        box-shadow: 0 14px 28px -8px rgba(5, 150, 105, 0.28);
    }}


    .hero-banner {{ background-size: 220% 220% !important; animation: fadeUp 0.55s var(--ease) backwards, gradientShift 16s ease infinite; }}
    .hero-banner::after {{
        content: "";
        position: absolute;
        top: -70px; right: -70px;
        width: 280px; height: 280px;
        border-radius: 50%;
        background: radial-gradient(circle, rgba(52, 211, 153, 0.35), transparent 65%);
        animation: floatY 6s ease-in-out infinite;
        pointer-events: none;
    }}
    .top-app-bar {{ background-size: 200% 200% !important; animation: fadeUp 0.5s var(--ease) backwards, gradientShift 20s ease infinite; }}
    .top-app-bar-page-badge {{ display: inline-flex; align-items: center; backdrop-filter: blur(6px); }}
    .live-dot {{
        display: inline-block;
        width: 8px; height: 8px;
        border-radius: 50%;
        background: #10B981;
        margin-inline-end: 8px;
        animation: pulseDot 1.8s infinite;
    }}
    @keyframes floatSm {{ 0%, 100% {{ transform: translateY(2px); }} 50% {{ transform: translateY(-2px); }} }}
    .brand-logo {{ display: inline-block; line-height: 1; animation: floatSm 3.5s ease-in-out infinite; }}


    [data-testid="stMain"] .stButton button,
    [data-testid="stMain"] .stDownloadButton button {{
        position: relative;
        overflow: hidden;
        min-height: 46px;
        letter-spacing: 0.2px;
        transition: transform 0.2s var(--ease), box-shadow 0.2s ease, background 0.2s ease, color 0.2s ease !important;
    }}
    [data-testid="stMain"] .stButton button::after,
    [data-testid="stMain"] .stDownloadButton button::after {{
        content: "";
        position: absolute;
        top: 0; left: 0;
        width: 30%; height: 100%;
        background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.4), transparent);
        transform: translateX(-120%) skewX(-20deg);
        pointer-events: none;
    }}
    [data-testid="stMain"] .stButton button:hover::after,
    [data-testid="stMain"] .stDownloadButton button:hover::after {{ animation: shine 0.8s ease; }}
    [data-testid="stMain"] .stButton button:active,
    [data-testid="stMain"] .stDownloadButton button:active {{ transform: translateY(0) scale(0.97) !important; }}
    [data-testid="stMain"] .stButton button:disabled {{
        opacity: 0.5 !important;
        filter: grayscale(0.4);
        cursor: not-allowed !important;
        box-shadow: none !important;
        transform: none !important;
    }}

    [data-testid="stMain"] .stButton button[data-testid="stBaseButton-secondary"],
    [data-testid="stMain"] .stButton button[kind="secondary"] {{
        background: transparent !important;
        color: {accent_green} !important;
        border: 1.5px solid #10B981 !important;
        box-shadow: none !important;
    }}
    [data-testid="stMain"] .stButton button[data-testid="stBaseButton-secondary"]:hover,
    [data-testid="stMain"] .stButton button[kind="secondary"]:hover {{
        background: {btn_gradient} !important;
        color: #FFFFFF !important;
        box-shadow: 0 6px 16px rgba(5, 150, 105, 0.4) !important;
    }}


    [data-testid="stSidebar"] .stButton button:hover {{
        background: rgba(16, 185, 129, 0.14) !important;
        border-color: rgba(16, 185, 129, 0.45) !important;
        transform: translateX({'-4px' if is_ur else '4px'}) !important;
    }}
    [data-testid="stSidebar"] .stButton button[kind="primary"],
    [data-testid="stSidebar"] .stButton button[data-testid="stBaseButton-primary"] {{
        background-size: 200% 200% !important;
        animation: gradientShift 6s ease infinite;
    }}
    [data-testid="stSidebar"] .stButton button[kind="primary"]:hover,
    [data-testid="stSidebar"] .stButton button[data-testid="stBaseButton-primary"]:hover {{
        background: {nav_active_bg} !important;
        transform: none !important;
    }}


    div[data-baseweb="input"], div[data-baseweb="textarea"] {{
        background: {widget_bg} !important;
        border: 1px solid {widget_border} !important;
        border-radius: 12px !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }}
    div[data-baseweb="base-input"] {{ background: transparent !important; border: none !important; }}
    div[data-baseweb="input"]:focus-within, div[data-baseweb="textarea"]:focus-within {{
        border-color: #10B981 !important;
        box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.22) !important;
    }}
    div[data-baseweb="select"] > div {{ transition: border-color 0.2s ease, box-shadow 0.2s ease; }}
    div[data-baseweb="select"] > div:hover {{ border-color: #10B981 !important; }}
    div[data-baseweb="select"] > div:focus-within {{
        border-color: #10B981 !important;
        box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.22) !important;
    }}
    li[role="option"]:hover {{ background-color: rgba(16, 185, 129, 0.15) !important; }}
    [data-testid="stNumberInput"] button {{ background: {bg_card_secondary} !important; color: {text_primary} !important; border-radius: 8px !important; }}
    [data-testid="stNumberInput"] button:hover {{ background: rgba(16, 185, 129, 0.2) !important; }}


    [data-testid="stRadio"] [role="radiogroup"] {{ gap: 8px; }}
    [data-testid="stRadio"] [role="radiogroup"] label {{
        background: {widget_bg};
        border: 1px solid {widget_border};
        border-radius: 12px;
        padding: 8px 14px !important;
        cursor: pointer;
        transition: all 0.2s var(--ease);
    }}
    [data-testid="stRadio"] [role="radiogroup"] label:hover {{ border-color: #10B981; transform: translateY(-1px); }}
    [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {{
        background: rgba(16, 185, 129, 0.16);
        border-color: #10B981;
        box-shadow: 0 0 0 2px rgba(16, 185, 129, 0.18);
    }}


    [data-testid="stSlider"] [role="slider"] {{
        background: #10B981 !important;
        border: 2px solid #FFFFFF !important;
        box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.25) !important;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }}
    [data-testid="stSlider"] [role="slider"]:hover {{ transform: scale(1.18); box-shadow: 0 0 0 7px rgba(16, 185, 129, 0.25) !important; }}
    [data-testid="stSliderThumbValue"], [data-testid="stSlider"] [data-testid="stThumbValue"] {{ color: #10B981 !important; font-weight: 700 !important; }}


    [data-baseweb="tab-list"] {{ gap: 6px; border-bottom: 1px solid {border_color}; }}
    button[data-baseweb="tab"] {{ border-radius: 10px 10px 0 0 !important; transition: background 0.2s ease; }}
    button[data-baseweb="tab"]:hover {{ background: rgba(16, 185, 129, 0.12) !important; }}
    [data-baseweb="tab-highlight"] {{ background: #10B981 !important; height: 3px !important; border-radius: 3px; }}


    [data-testid="stFileUploaderDropzone"] {{ transition: border-color 0.2s ease, background 0.2s ease, transform 0.2s ease; }}
    [data-testid="stFileUploaderDropzone"]:hover {{ border-color: #10B981 !important; transform: translateY(-2px); }}
    [data-testid="stVideo"] video, [data-testid="stImage"] img {{
        border-radius: 16px;
        box-shadow: 0 10px 25px -8px rgba(0, 0, 0, 0.3);
    }}
    [data-testid="stExpander"] {{ transition: box-shadow 0.2s ease; }}
    [data-testid="stExpander"]:hover {{ box-shadow: 0 6px 16px rgba(5, 150, 105, 0.18); }}
    [data-testid="stSpinner"] i, [data-testid="stSpinner"] svg {{ border-top-color: #10B981 !important; }}


    .spray-zone-card {{ background: {bg_card}; color: {text_primary}; border: 1px solid {border_color}; border-radius: 16px; padding: 16px 18px; margin-bottom: 14px; box-shadow: 0 4px 10px -4px rgba(0, 0, 0, 0.12); animation: fadeUp 0.5s var(--ease) backwards; transition: transform 0.25s var(--ease), border-color 0.25s ease; }}
    .spray-zone-card:hover {{ border-color: #10B981; transform: translateY(-2px); }}
    .spray-zone-head {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }}
    .spray-zone-title {{ font-size: 16px; font-weight: 800; display: flex; align-items: center; gap: 10px; }}
    .spray-sev {{ font-size: 11px; font-weight: 800; text-transform: uppercase; padding: 3px 10px; border-radius: 20px; border: 1px solid; }}
    .spray-zone-count {{ font-size: 12px; font-weight: 700; background: {bg_card_secondary}; padding: 4px 10px; border-radius: 20px; }}
    .spray-progress {{ height: 6px; background: {progress_track}; border-radius: 6px; overflow: hidden; margin-bottom: 10px; }}
    .spray-progress-fill {{ height: 100%; background: linear-gradient(90deg, #10B981, #34D399); border-radius: 6px; }}
    .spray-stats {{ display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; font-weight: 700; margin-bottom: 12px; color: {text_secondary}; }}
    .spray-stats .stat::before {{ content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--c); margin-inline-end: 6px; }}
    .cell-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 8px; }}
    .cell-chip {{ display: flex; align-items: center; justify-content: center; gap: 6px; padding: 6px 4px; border-radius: 10px; border: 1px solid var(--c); background: rgba(128, 128, 128, 0.08); background: color-mix(in srgb, var(--c) 14%, transparent); font-size: 12px; font-weight: 700; cursor: default; transition: transform 0.15s ease, box-shadow 0.15s ease; }}
    .cell-chip::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--c); flex: none; }}
    .cell-chip:hover {{ transform: translateY(-2px) scale(1.04); box-shadow: 0 6px 14px -4px var(--c); }}

    .metric-card {{ min-height: 150px; display: flex; flex-direction: column; justify-content: flex-start; }}


    [data-testid="stElementContainer"]:has(.search-bar-anchor) {{ display: none; }}
    [data-testid="stHorizontalBlock"]:has(.search-bar-anchor) {{
        background: linear-gradient(135deg, rgba(16, 185, 129, 0.20), rgba(59, 130, 246, 0.16));
        border: 1px solid rgba(16, 185, 129, 0.5);
        border-radius: 18px;
        padding: 14px 16px;
        align-items: center;
        margin-bottom: 14px;
        box-shadow: 0 10px 26px -12px rgba(5, 150, 105, 0.55);
    }}
    [data-testid="stHorizontalBlock"]:has(.search-bar-anchor) div[data-baseweb="input"] {{ min-height: 52px; }}
    [data-testid="stHorizontalBlock"]:has(.search-bar-anchor) input {{ font-size: 16px !important; }}
    [data-testid="stHorizontalBlock"]:has(.search-bar-anchor) button {{ min-height: 52px; font-size: 18px; }}


    @media (max-width: 768px) {{
        .block-container {{ padding: 1.5rem 1rem 2rem 1rem !important; }}
        .hero-banner {{ padding: 24px; }}
        .hero-banner .hero-title {{ font-size: 24px; }}
        .hero-banner {{ min-height: 0; }}
        .hero-banner .hero-logo {{ width: 48px; height: 48px; font-size: 28px; border-radius: 14px; }}
        .hero-banner .hero-title-row {{ gap: 12px; }}
        .hero-banner .hero-desc {{ font-size: 14px; }}
        .top-app-bar {{ flex-direction: column; align-items: flex-start; gap: 10px; padding: 14px 16px; }}
        .top-app-bar .top-app-bar-title {{ font-size: 19px; }}
        .metric-value {{ font-size: 26px; }}
        .agri-card:hover {{ transform: none; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
        *, *::before, *::after {{ animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }}
    }}
    </style>
    """
    grad_brand = ("linear-gradient(135deg, #059669 0%, #0D9488 55%, #0E7490 100%)" if is_dark
                  else "linear-gradient(135deg, #2E6B4E 0%, #1F7A5A 55%, #013237 100%)")
    grad_soft = ("linear-gradient(135deg, rgba(52, 211, 153, 0.22) 0%, rgba(14, 116, 144, 0.22) 100%)" if is_dark
                 else "linear-gradient(135deg, rgba(192, 230, 186, 0.55) 0%, rgba(76, 167, 113, 0.16) 100%)")
    shadow_card = ("0 10px 28px -14px rgba(0, 0, 0, 0.55)" if is_dark
                   else "0 10px 28px -14px rgba(1, 50, 55, 0.38), 0 2px 6px -2px rgba(1, 50, 55, 0.14)")
    shadow_hover = ("0 16px 34px -14px rgba(0, 0, 0, 0.65)" if is_dark
                    else "0 16px 34px -14px rgba(1, 50, 55, 0.50), 0 4px 10px -4px rgba(1, 50, 55, 0.22)")
    grad_bar = "#10B981" if is_dark else "#2E6B4E"
    card_bg = "#1E293B" if is_dark else "#F1F9EF"

    common_css = """

    .block-container { padding-top: 1rem !important; }
    [data-testid="stVerticalBlock"] { gap: 0.9rem; }
    [data-testid="stMain"] p, [data-testid="stMain"] li { line-height: 1.6; }


    .kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin: 4px 0 14px; }
    .kpi { background: @@bg_card@@; border: 1px solid @@border_color@@; border-radius: 16px; padding: 14px 16px 12px 20px;
           box-shadow: @@shadow_card@@; position: relative; overflow: hidden; }
    .kpi::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 5px; background: var(--kc, #10B981); }
    .kpi .num { font-size: 28px; font-weight: 800; line-height: 1.1; color: @@text_primary@@; }
    .kpi .lbl { font-size: 11.5px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: @@text_secondary@@; margin-top: 4px; }


    .vr-banner { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
                 border: 1px solid; border-radius: 18px; padding: 16px 22px; margin: 4px 0 12px; box-shadow: @@shadow_card@@; }
    .vr-b-lbl { font-size: 12px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; opacity: 0.9; }
    .vr-b-val { font-size: 26px; font-weight: 800; line-height: 1.2; }
    .vr-b-sub { font-size: 14px; font-weight: 700; }
    [data-testid="stElementContainer"]:has(.vr-card-anchor), .element-container:has(.vr-card-anchor) { display: none; }
    [data-testid="stColumn"]:has(.vr-card-anchor), div[data-testid="column"]:has(.vr-card-anchor) {
        background: @@bg_card@@; border: 1px solid @@border_color@@; border-radius: 20px; padding: 18px 18px 14px; box-shadow: @@shadow_card@@;
    }
    .vr-card-title { font-size: 16px; font-weight: 800; margin: 0 0 4px; color: @@text_primary@@; }
    .vr-chips-title { font-size: 12px; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; color: @@text_secondary@@; margin: 12px 0 6px; }
    .vr-chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .vr-chip { background: @@grad_soft@@; border: 1px solid @@border_color@@; border-radius: 999px; padding: 4px 12px; font-size: 12.5px; font-weight: 600; color: @@text_primary@@; }


    .vr-hero { display: grid; grid-template-columns: 1.5fr 1fr; gap: 24px; align-items: center; border: 1px solid; border-radius: 22px;
               padding: 22px 28px; margin: 6px 0 16px; box-shadow: @@shadow_card@@; position: relative; overflow: hidden; }
    .vr-hero .vr-b-val { font-size: 34px; font-weight: 800; line-height: 1.15; margin: 2px 0 4px; }
    .vr-hero .vr-b-sub { font-size: 14px; font-weight: 700; opacity: 0.92; }
    .vr-hero .vr-b-lbl { font-size: 12px; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; opacity: 0.88; }
    .vr-meter { height: 12px; border-radius: 999px; background: rgba(255, 255, 255, 0.5); overflow: hidden; margin: 8px 0 6px; }
    .vr-meter > span { display: block; height: 100%; border-radius: 999px; background: currentColor; opacity: 0.85; }
    @media (max-width: 760px) { .vr-hero { grid-template-columns: 1fr; padding: 18px 20px; } .vr-hero .vr-b-val { font-size: 28px; } }

    .vr-card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 12px; flex-wrap: wrap; }
    .vr-card-head .vr-card-title { margin: 0; font-size: 17px; }
    .vr-pill { display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: 11.5px; font-weight: 700; letter-spacing: 0.02em;
               background: @@grad_soft@@; border: 1px solid @@border_color@@; color: @@text_secondary@@; white-space: nowrap; }
    .vr-legend { display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 12px 2px 10px; font-size: 12.5px; font-weight: 600; color: @@text_secondary@@; }
    .vr-legend span { display: inline-flex; align-items: center; gap: 6px; }
    .vr-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--c); display: inline-block; }

    .vr-matrix { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .zone-tile.vr-tile { padding: 12px 8px 10px; min-height: 96px; gap: 2px; }
    .vr-tile .zt-id { font-size: 11.5px; font-weight: 800; letter-spacing: 0.08em; opacity: 0.9; }
    .vr-tile .zt-sev { font-size: 15px; font-weight: 800; text-transform: uppercase; line-height: 1.15; }
    .vr-tile .zt-dose { font-size: 11.5px; font-weight: 700; opacity: 0.95; }
    .vr-tile .zt-pest { font-size: 10.5px; opacity: 0.9; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    .vr-rows { display: flex; flex-direction: column; gap: 10px; margin-top: 6px; }
    .vr-row { display: grid; grid-template-columns: 52px minmax(0, 1fr) auto; gap: 14px; align-items: center; background: @@bg_card@@;
              border: 1px solid @@border_color@@; border-left: 6px solid var(--c); border-radius: 16px; padding: 12px 16px; box-shadow: @@shadow_card@@; }
    .vr-row .vr-zid { width: 44px; height: 44px; border-radius: 14px; display: flex; align-items: center; justify-content: center;
                      font-weight: 800; font-size: 15px; color: #FFFFFF; background: var(--c); }
    .vr-row .vr-zt { font-size: 14.5px; font-weight: 800; color: @@text_primary@@; }
    .vr-row .vr-zd { font-size: 13px; color: @@text_secondary@@; line-height: 1.45; margin-top: 2px; }
    .vr-row .vr-zs { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    @media (max-width: 760px) { .vr-row { grid-template-columns: 44px minmax(0, 1fr); } .vr-row .vr-zs { grid-column: 1 / -1; justify-content: flex-start; } }


    @media (min-width: 769px) {
        section[data-testid="stSidebar"][aria-expanded="false"] {
            position: fixed !important; left: 0 !important; top: 0 !important; height: 100vh !important;
            transform: none !important; margin: 0 !important; visibility: visible !important; opacity: 1 !important;
            display: block !important; width: 72px !important; min-width: 72px !important; max-width: 72px !important;
            overflow: hidden !important; z-index: 999 !important;
        }
        section[data-testid="stSidebar"][aria-expanded="false"] > div, section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarContent"] { width: 72px !important; min-width: 72px !important; transform: none !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarHeader"],
        section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarResizeHandle"],
        section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"],
        section[data-testid="stSidebar"][aria-expanded="false"] .sidebar-brand-text,
        section[data-testid="stSidebar"][aria-expanded="false"] .sidebar-status,
        section[data-testid="stSidebar"][aria-expanded="false"] hr { display: none !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stElementContainer"]:has(.sidebar-brand) {
            position: fixed !important; left: 16px !important; top: 16px !important; width: 40px !important; height: 40px !important;
            display: flex !important; align-items: center !important; justify-content: center !important; margin: 0 !important; z-index: 1000;
        }
        section[data-testid="stSidebar"][aria-expanded="false"] .sidebar-brand {
            width: 40px !important; height: 40px !important; justify-content: center !important; align-items: center !important;
            border-bottom: none !important; margin: 0 !important; padding: 0 !important; cursor: pointer;
        }
        section[data-testid="stSidebar"][aria-expanded="false"] .sidebar-brand .brand-logo { font-size: 24px !important; line-height: 1 !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] [class*="st-key-nav_btn_"] {
            position: fixed !important; left: 16px !important; width: 40px !important; height: 40px !important;
            margin: 0 !important; padding: 0 !important; z-index: 1000;
        }
        section[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_btn_home { top: 72px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_btn_visual_report { top: 120px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_btn_gps { top: 168px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_btn_problems { top: 216px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_btn_spray { top: 264px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_btn_pests { top: 312px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .st-key-nav_btn_settings { top: 360px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] [class*="st-key-nav_btn_"] [data-testid="stButton"] { width: 40px !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .stButton button {
            width: 40px !important; min-width: 40px !important; height: 40px !important; min-height: 40px !important; padding: 0 !important;
            display: flex !important; align-items: center !important; justify-content: center !important;
            margin: 0 !important; border-radius: 12px !important;
        }
        section[data-testid="stSidebar"][aria-expanded="false"] .stButton button [data-testid="stMarkdownContainer"] { display: none !important; }
        section[data-testid="stSidebar"][aria-expanded="false"] .stButton button [data-testid="stIconEmoji"],
        section[data-testid="stSidebar"][aria-expanded="false"] .stButton button span { margin: 0 !important; font-size: 18px !important; line-height: 1 !important; }
        .stApp:has(section[data-testid="stSidebar"][aria-expanded="false"]) .block-container { padding-left: 92px !important; }
        [data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"],
        button[data-testid="stExpandSidebarButton"], [data-testid="stExpandSidebarButton"] button {
            position: fixed !important; left: -200px !important; top: 0 !important; opacity: 0 !important; pointer-events: none !important;
        }
    }

    @media (max-width: 768px) {
        [data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"] {
            position: fixed !important; top: 3.6rem !important; left: 0.8rem !important; z-index: 1000002 !important;
            display: flex !important; visibility: visible !important; opacity: 1 !important; width: auto !important; height: auto !important;
            background: transparent !important; margin: 0 !important; padding: 0 !important;
        }
        [data-testid="stExpandSidebarButton"] button, [data-testid="stSidebarCollapsedControl"] button, [data-testid="collapsedControl"] button,
        button[data-testid="stExpandSidebarButton"] {
            width: 42px !important; height: 42px !important; min-width: 42px !important; border-radius: 14px !important;
            background: @@grad_brand@@ !important; border: 2px solid rgba(255, 255, 255, 0.85) !important;
            box-shadow: 0 8px 20px -6px rgba(5, 150, 105, 0.7) !important; display: flex !important; align-items: center !important;
            justify-content: center !important; opacity: 1 !important; visibility: visible !important; cursor: pointer;
        }
        [data-testid="stExpandSidebarButton"] *, [data-testid="stSidebarCollapsedControl"] *, [data-testid="collapsedControl"] * {
            color: #FFFFFF !important; fill: #FFFFFF !important; stroke: #FFFFFF !important;
        }
    }


    [data-baseweb="tab-list"] { gap: 8px; border-bottom: none !important; flex-wrap: wrap; }
    [data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display: none !important; }
    button[data-baseweb="tab"] {
        border-radius: 12px !important; padding: 8px 16px !important; font-weight: 700 !important;
        background: @@bg_card@@ !important; border: 1px solid @@border_color@@ !important; color: @@text_primary@@ !important;
        box-shadow: @@shadow_card@@;
    }
    button[data-baseweb="tab"][aria-selected="true"] { background: @@grad_brand@@ !important; color: #FFFFFF !important; border-color: transparent !important; }
    button[data-baseweb="tab"][aria-selected="true"] p { color: #FFFFFF !important; }


    [data-testid="stPlotlyChart"] {
        background: @@bg_card@@ !important; border: 1px solid @@border_color@@ !important; border-radius: 18px !important;
        padding: 12px 14px !important; box-shadow: @@shadow_card@@ !important; position: relative; overflow: hidden;
    }
    [data-testid="stPlotlyChart"]::before { content: ""; position: absolute; top: 0; left: 0; right: 0; height: 4px; background: @@grad_brand@@; }


    [data-testid="stColumn"]:has(.search-bar-anchor), div[data-testid="column"]:has(.search-bar-anchor) { position: relative; }
    [data-testid="stColumn"]:has(.search-bar-anchor) input, div[data-testid="column"]:has(.search-bar-anchor) input { padding-right: 52px !important; }
    [data-testid="stColumn"]:has(.search-bar-anchor) [data-testid="stElementContainer"]:has(button),
    div[data-testid="column"]:has(.search-bar-anchor) .element-container:has(button) {
        position: absolute !important; right: 12px; top: 50%; transform: translateY(-50%);
        width: auto !important; z-index: 5; margin: 0 !important;
    }
    [data-testid="stHorizontalBlock"]:has(.search-bar-anchor) [data-testid="stColumn"]:has(.search-bar-anchor) button,
    [data-testid="stHorizontalBlock"]:has(.search-bar-anchor) div[data-testid="column"]:has(.search-bar-anchor) button {
        min-height: 30px !important; height: 30px !important; width: 30px !important; min-width: 30px !important;
        padding: 0 !important; border-radius: 50% !important; font-size: 15px !important; line-height: 1 !important;
        background: transparent !important; border: none !important; box-shadow: none !important;
        color: @@text_secondary@@ !important; opacity: 0.85;
    }
    [data-testid="stColumn"]:has(.search-bar-anchor) button:hover, div[data-testid="column"]:has(.search-bar-anchor) button:hover {
        background: rgba(16, 185, 129, 0.25) !important; opacity: 1; transform: none !important;
    }
    """

    light_css = """

    [data-testid="stMain"] h1, [data-testid="stMain"] h2, [data-testid="stMain"] h3, [data-testid="stMain"] h4 {
        letter-spacing: -0.01em; font-weight: 800 !important; line-height: 1.25 !important;
    }
    [data-testid="stMain"] h3 { padding-inline-start: 14px; border-inline-start: 5px solid transparent;
        border-image: linear-gradient(180deg, #2E6B4E, #013237) 1; }
    header[data-testid="stHeader"], [data-testid="stHeader"] { border-bottom: 3px solid transparent !important; border-image: @@grad_brand@@ 1 !important; }
    [data-testid="stMain"] .stButton > button, [data-testid="stMain"] .stDownloadButton > button {
        border-radius: 14px !important; font-weight: 700 !important; letter-spacing: 0.01em;
        transition: transform .18s ease, box-shadow .18s ease !important;
    }
    [data-testid="stMain"] .stButton > button[kind="primary"], [data-testid="stMain"] .stButton > button[data-testid="stBaseButton-primary"],
    [data-testid="stMain"] .stDownloadButton > button {
        background: @@grad_brand@@ !important; color: #FFFFFF !important; border: none !important;
        box-shadow: 0 8px 20px -8px rgba(1, 50, 55, 0.55) !important;
    }
    [data-testid="stMain"] .stButton > button[kind="primary"]:hover, [data-testid="stMain"] .stDownloadButton > button:hover {
        background: @@btn_hover@@ !important; transform: translateY(-2px);
    }
    [data-testid="stMain"] .stButton > button[kind="secondary"], [data-testid="stMain"] .stButton > button[data-testid="stBaseButton-secondary"] {
        background: @@bg_card@@ !important; color: @@text_primary@@ !important; border: 1px solid @@border_color@@ !important; box-shadow: @@shadow_card@@;
    }
    [data-testid="stMain"] .stButton > button[kind="secondary"]:hover { border-color: #2E6B4E !important; transform: translateY(-2px); }
    .agri-card, .spray-zone-card, .zone-tile { border-radius: 18px !important; box-shadow: @@shadow_card@@ !important; transition: transform .2s ease, box-shadow .2s ease; }
    .agri-card:hover, .spray-zone-card:hover { box-shadow: @@shadow_hover@@ !important; }
    [data-testid="stExpander"] summary { background: @@grad_soft@@ !important; }
    [data-testid="stExpander"] { box-shadow: @@shadow_card@@; border-radius: 18px !important; }
    [data-testid="stAlert"] { border-radius: 14px !important; }
    [data-testid="stSidebar"] { box-shadow: 6px 0 28px -16px rgba(1, 50, 55, 0.40); }
    video { background: @@bg_card_secondary@@ !important; }
    [data-testid="stCustomComponentV1"], [data-testid="stIFrame"], .folium-map, .leaflet-container { background: @@bg_card_secondary@@ !important; }
    [data-testid="stCustomComponentV1"] { border-radius: 16px; overflow: hidden; }
    [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, textarea { border-radius: 12px !important; }

    [data-testid="stSidebar"] .stButton button:hover {
        background: linear-gradient(135deg, rgba(255,255,255,0.85) 0%, rgba(192,230,186,0.95) 100%) !important;
        border-color: #2E6B4E !important; box-shadow: 0 6px 16px -8px rgba(1, 50, 55, 0.45) !important;
    }
    [data-testid="stMain"] .stButton > button[kind="secondary"]:hover, [data-testid="stMain"] .stButton > button[data-testid="stBaseButton-secondary"]:hover {
        background: linear-gradient(135deg, #F0F9EE 0%, #C0E6BA 100%) !important;
    }
    [data-testid="stSidebar"]::after { content: ""; position: absolute; top: 0; right: 0; width: 3px; height: 100%;
        background: linear-gradient(180deg, #2E6B4E, #013237); opacity: 0.6; pointer-events: none; }
    button[data-baseweb="tab"] { background: linear-gradient(145deg, #FFFFFF 0%, #E1F2DE 100%) !important; }
    button[data-baseweb="tab"][aria-selected="true"] { background: @@grad_brand@@ !important; }

    .hero-banner { background: linear-gradient(135deg, #013237 0%, #0B4F48 38%, #2E8A62 72%, #2E6B4E 100%) !important;
        border-color: rgba(192, 230, 186, 0.55) !important; box-shadow: 0 20px 28px -8px rgba(1, 50, 55, 0.42) !important; }
    .top-app-bar { border-left-color: #C0E6BA !important; box-shadow: 0 10px 25px -5px rgba(1, 50, 55, 0.30) !important; }
    .agri-card::before { background: #2E6B4E !important; }
    [data-testid="stPlotlyChart"]::before { background: #2E6B4E !important; }
    [data-testid="stSidebar"] .stButton button[kind="primary"], [data-testid="stSidebar"] .stButton button[data-testid="stBaseButton-primary"] {
        box-shadow: 0 4px 14px rgba(1, 50, 55, 0.35) !important; }
    """
    unify_css = """
    .agri-card, .spray-zone-card, .kpi, .vr-row, .empty-state-box,
    [data-testid="stPlotlyChart"],
    [data-testid="stColumn"]:has(.vr-card-anchor), div[data-testid="column"]:has(.vr-card-anchor),
    [data-testid="stFileUploader"], [data-testid="stExpander"], details[data-testid="stExpander"] {
        background: @@card_bg@@ !important;
        border: 1px solid @@border_color@@ !important;
        border-radius: 18px !important;
        box-shadow: @@shadow_card@@ !important;
    }
    .agri-card:hover, .spray-zone-card:hover, .kpi:hover { box-shadow: @@shadow_hover@@ !important; }
    .agri-card, .spray-zone-card, .kpi, .empty-state-box, [data-testid="stFileUploader"], [data-testid="stExpander"],
    [data-testid="stColumn"]:has(.vr-card-anchor), div[data-testid="column"]:has(.vr-card-anchor) { position: relative; overflow: hidden; }
    .agri-card::before, .spray-zone-card::before, [data-testid="stPlotlyChart"]::before, .kpi::before, .empty-state-box::before,
    [data-testid="stFileUploader"]::before, [data-testid="stExpander"]::before,
    [data-testid="stColumn"]:has(.vr-card-anchor)::before, div[data-testid="column"]:has(.vr-card-anchor)::before {
        content: ""; position: absolute; top: 0; left: 0; right: 0; bottom: auto; width: auto; height: 4px;
        background: @@grad_bar@@ !important;
    }
    .kpi .num { color: var(--kc, #10B981) !important; }
    .vr-hero::before, .vr-hero::after { content: none !important; display: none !important; }
    .agri-card { padding: 18px 20px !important; }
    [data-testid="stFileUploader"] { padding: 12px 14px !important; }
    [data-testid="stExpander"] summary, details[data-testid="stExpander"] > summary,
    [data-testid="stFileUploaderDropzone"] {
        background: @@bg_card_secondary@@ !important;
    }
    button[data-baseweb="tab"] {
        background: @@card_bg@@ !important;
        border: 1px solid @@border_color@@ !important;
        box-shadow: @@shadow_card@@ !important;
    }
    button[data-baseweb="tab"][aria-selected="true"] { background: @@grad_brand@@ !important; }
    """
    extra_css = "<style>" + common_css + ("" if is_dark else light_css) + unify_css + "</style>"
    for _k, _v in {"grad_brand": grad_brand, "grad_soft": grad_soft, "btn_hover": btn_hover, "bg_card": bg_card,
                   "text_primary": text_primary, "text_secondary": text_secondary, "border_color": border_color,
                   "shadow_card": shadow_card, "shadow_hover": shadow_hover, "bg_card_secondary": bg_card_secondary,
                   "grad_bar": grad_bar, "card_bg": card_bg}.items():
        extra_css = extra_css.replace("@@" + _k + "@@", _v)
    st.markdown(css, unsafe_allow_html=True)
    st.markdown(flat(extra_css), unsafe_allow_html=True)

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

        doc.querySelectorAll('.metric-value').forEach(el => {
            if (el.dataset.counted) return;
            el.dataset.counted = "1";
            const m = (el.textContent || "").trim().match(/^(\\d+(?:\\.\\d+)?)(.*)$/);
            if (!m) return;
            const target = parseFloat(m[1]);
            const dec = (m[1].split('.')[1] || "").length;
            const rest = m[2];
            if (!isFinite(target) || target === 0) return;
            const raf = window.parent.requestAnimationFrame.bind(window.parent);
            const t0 = window.parent.performance.now();
            const step = now => {
                const p = Math.min(1, (now - t0) / 700);
                const e = 1 - Math.pow(1 - p, 3);
                el.textContent = (target * e).toFixed(dec) + rest;
                if (p < 1) raf(step);
            };
            raf(step);
        });
    } catch(e) {}
}

function bindLogoOpensSidebar() {
    try {
        const pw = window.parent;
        const pdoc = pw.document;
        if (pw.__agriLogoBound) return;
        pw.__agriLogoBound = true;
        pdoc.addEventListener('click', function(e) {
            const brand = e.target && e.target.closest ? e.target.closest('.sidebar-brand') : null;
            if (!brand) return;
            const sb = pdoc.querySelector('section[data-testid="stSidebar"]');
            if (!sb || sb.getAttribute('aria-expanded') !== 'false') return;
            const el = pdoc.querySelector('[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"]');
            if (!el) return;
            const inner = el.tagName === 'BUTTON' ? el : (el.querySelector('button') || el);
            inner.click();
        }, true);
    } catch(e) {}
}
bindLogoOpensSidebar();

setInterval(applyVideoAndMenuRules, 250);
document.addEventListener("DOMContentLoaded", applyVideoAndMenuRules);
</script>
"""
components.html(flat(video_and_menu_handler_js), height=0)


PAGE_SIZE = 10


def _go_page(state_key, delta, pages):
    st.session_state[state_key] = min(max(st.session_state.get(state_key, 1) + delta, 1), pages)


def paginate_items(items, key, per_page=PAGE_SIZE):
    items = list(items)
    pages = max(1, -(-len(items) // per_page))
    state_key = f"page_{key}"
    current = min(max(st.session_state.get(state_key, 1), 1), pages)
    st.session_state[state_key] = current
    return items[(current - 1) * per_page: current * per_page]


def render_page_controls(total, key, per_page=PAGE_SIZE):
    pages = max(1, -(-total // per_page))
    if pages <= 1:
        return
    state_key = f"page_{key}"
    current = min(max(st.session_state.get(state_key, 1), 1), pages)
    first = (current - 1) * per_page + 1
    last = min(current * per_page, total)
    c_prev, c_info, c_next = st.columns([1, 2, 1])
    with c_prev:
        st.button(L("⬅️ Previous"), key=f"{state_key}_prev", use_container_width=True,
                  disabled=(current <= 1), on_click=_go_page, args=(state_key, -1, pages))
    with c_info:
        st.markdown(
            flat(f'<div style="text-align: center; font-weight: 700; padding-top: 8px;">'
                 f'{first}-{last} / {total} &middot; {L("Page")} {current} / {pages}</div>'),
            unsafe_allow_html=True,
        )
    with c_next:
        st.button(L("Next ➡️"), key=f"{state_key}_next", use_container_width=True,
                  disabled=(current >= pages), on_click=_go_page, args=(state_key, 1, pages))


def render_custom_table(headers, rows, page_key=None):
    page_key = page_key or ("tbl_" + "_".join(re.sub(r"\W+", "", str(h)) for h in headers))
    total_rows = len(rows)
    rows = paginate_items(rows, page_key)
    header_html = "".join([f"<th>{h}</th>" for h in headers])
    rows_html = ""
    for row in rows:
        cells = "".join([f"<td>{cell}</td>" for cell in row])
        rows_html += f"<tr>{cells}</tr>"

    table_code = f"""
    <div class="scroll-hint">{L('Swipe sideways to see more')} <span class="scroll-arrow">➜</span></div>
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
    render_page_controls(total_rows, page_key)


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


def describe_problem(z):
    sev = z.get("severity", "none")
    if sev == "none":
        return t["no_issue"]
    zdata = z.get("raw", {}) or {}
    symptoms = []
    detected = z.get("detected_classes", [])
    if detected:
        symptoms.append(L("Pests found: {p}").format(p=", ".join(friendly_pest_list(detected))))
    if zdata.get("p95_chlorosis_ratio", 0.0) > 0.05:
        symptoms.append(L("Yellow leaves ({p:.1f}%)").format(p=zdata.get("p95_chlorosis_ratio", 0.0) * 100))
    if zdata.get("p95_necrosis_ratio", 0.0) > 0.05:
        symptoms.append(L("Dead brown spots ({p:.1f}%)").format(p=zdata.get("p95_necrosis_ratio", 0.0) * 100))
    if zdata.get("p95_wilting_score", 0.0) > 0.05:
        symptoms.append(L("Plants are wilting"))
    if not symptoms:
        symptoms.append(L("Crop looks stressed"))
    return L("Problem level: {lvl}. {s}.").format(lvl=severity_label(sev), s="; ".join(symptoms))


def zone_chemical(z):
    dom = z.get("dominant_pest")
    key = pest_catalog_key(dom) if dom else None
    if key:
        info = PEST_ACCESSIBLE_INFO[key]
        return info["treatment_ur"] if lang == "ur" else info["treatment"]
    if z.get("detected_classes"):
        return L("General crop protection spray")
    if z.get("severity", "none") != "none":
        return L("Leaf feeding spray and fungus medicine")
    return "N/A"


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

            if dom and pest_catalog_key(dom):
                chemical = PEST_ACCESSIBLE_INFO[pest_catalog_key(dom)].get("treatment", "Pest-control medicine")
            elif detected_classes:
                chemical = "General crop protection spray"
            elif sev != "none":
                chemical = "Leaf feeding spray and fungus medicine"
            else:
                chemical = "N/A"

            if sev == "none":
                problem_desc = t["no_issue"]
            else:
                symptoms = []
                if detected_classes:
                    symptoms.append(f"Pests found: {', '.join(friendly_pest_list(detected_classes))}")
                if zdata.get("p95_chlorosis_ratio", 0.0) > 0.05:
                    symptoms.append(f"Yellow leaves ({zdata.get('p95_chlorosis_ratio', 0.0)*100:.1f}%)")
                if zdata.get("p95_necrosis_ratio", 0.0) > 0.05:
                    symptoms.append(f"Dead brown spots ({zdata.get('p95_necrosis_ratio', 0.0)*100:.1f}%)")
                if zdata.get("p95_wilting_score", 0.0) > 0.05:
                    symptoms.append("Plants are wilting")
                if not symptoms:
                    symptoms.append("Crop looks stressed")
                problem_desc = f"Problem level: {severity_label(sev)}. " + "; ".join(symptoms) + "."

            zones.append({
                "zone_id": zone_index,
                "row": r,
                "col": c,
                "severity": sev,
                "spray_needed": spray_needed,
                "spray_dose_percent": spray_pct,
                "dose_liters_per_ha": dose_l_ha,
                "recommended_chemical": chemical,
                "dominant_pest": dom,
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


try:
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    go = None
    HAS_PLOTLY = False

import inspect as _inspect

SEV_ORDER = ["none", "low", "medium", "high"]
SEV_CHART_COLORS = {"none": "#16A34A", "low": "#EAB308", "medium": "#EA580C", "high": "#DC2626"}
font_family_css = ("Urdu Typesetting, Nastaliq, Segoe UI, Tahoma, sans-serif" if lang == "ur"
                   else "Inter, Segoe UI, Roboto, sans-serif")

UR.update({
    "Field Health Overview": "کھیت کی صحت کا جائزہ",
    "How the 9 zones are doing": "9 حصوں کی موجودہ حالت",
    "healthy zones": "صحت مند حصے",
    "Crop Area Affected by Zone": "ہر حصے میں متاثرہ فصل",
    "Share of crop affected in each zone": "ہر حصے میں متاثرہ فصل کا فیصد",
    "Pests Detected in This Video": "اس ویڈیو میں ملے کیڑے",
    "How many times each pest was seen": "ہر کیڑا کتنی بار نظر آیا",
    "No pests detected in this video": "اس ویڈیو میں کوئی کیڑا نہیں ملا",
    "Spray Amount by Zone": "ہر حصے کے لیے سپرے کی مقدار",
    "Medicine needed in each zone (litres/hectare)": "ہر حصے میں درکار دوا (لیٹر فی ہیکٹر)",
    "zones": "حصے",
    "times seen": "بار دیکھا گیا",
    "Seen in": "ملا",
})


def _chart_colors():
    dark = (theme == "dark")
    return {
        "text":   "#F8FAFC" if dark else "#013237",
        "muted":  "#E2E8F0" if dark else "#1F4D4A",
        "axis":   "#E2E8F0" if dark else "#2E7D5B",
        "grid":   "rgba(226,232,240,0.20)" if dark else "rgba(1,50,55,0.12)",
        "gap":    "#1E293B" if dark else "#FFFFFF",
        "tip_bg": "#0F172A" if dark else "#FFFFFF",
        "border": "#334155" if dark else "#BFE0B8",
    }


def style_chart(fig, title, subtitle=None, height=320, legend=False):
    c = _chart_colors()
    title_html = f"<b>{title}</b>"
    if subtitle:
        title_html += f"<br><span style='font-size:12px; color:{c['muted']}'>{subtitle}</span>"
    fig.update_layout(
        title=dict(text=title_html, x=0.03, xanchor="left", y=0.96, yanchor="top", yref="container",
                   font=dict(size=16, color=c["text"])),
        height=height,
        margin=dict(l=20, r=20, t=88 if subtitle else 60, b=72 if legend else 24),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=font_family_css, color=c["muted"], size=12),
        hoverlabel=dict(bgcolor=c["tip_bg"], bordercolor=c["border"],
                        font=dict(color=c["text"], size=13, family=font_family_css)),
        showlegend=legend,
        legend=dict(orientation="h", yref="paper", y=-0.30, yanchor="top", x=0.5, xanchor="center",
                    font=dict(color=c["muted"], size=12), bgcolor="rgba(0,0,0,0)"),
        bargap=0.45,
        colorway=["#10B981", "#0EA5E9", "#14B8A6", "#6366F1", "#F59E0B", "#34D399"],
    )
    axis_style = dict(tickfont=dict(color=c["text"], size=12), title_font=dict(color=c["muted"], size=12),
                      linecolor=c["axis"], linewidth=1.5, tickcolor=c["axis"], automargin=True)
    fig.update_xaxes(**axis_style)
    fig.update_yaxes(**axis_style)
    return fig


def _round_bars(fig, radius=6):
    try:
        fig.update_traces(marker_cornerradius=radius, selector=dict(type="bar"))
    except ValueError:
        pass


def show_chart(fig, key):
    cfg = {"displayModeBar": False}
    if "width" in _inspect.signature(st.plotly_chart).parameters:
        st.plotly_chart(fig, width="stretch", theme=None, config=cfg, key=key)
    else:
        st.plotly_chart(fig, use_container_width=True, theme=None, config=cfg, key=key)


def _severity_bar_fig(zones, values, value_fmt, hover_unit, axis_suffix="", axis_title=None):
    c = _chart_colors()
    fig = go.Figure()
    ids = [str(z["zone_id"]) for z in zones]
    for sev in SEV_ORDER:
        idx = [i for i, z in enumerate(zones) if severity_class(z["severity"]) == sev]
        if not idx:
            continue
        fig.add_trace(go.Bar(
            x=[ids[i] for i in idx],
            y=[values[i] for i in idx],
            name=severity_label(sev),
            marker=dict(color=SEV_CHART_COLORS[sev], line=dict(width=0)),
            text=[value_fmt.format(values[i]) for i in idx],
            textposition="outside",
            textfont=dict(color=c["text"], size=12),
            cliponaxis=False,
            hovertemplate=(f"<b>{L('Zone')} %{{x}}</b><br>{severity_label(sev)}<br>"
                           f"%{{y:.1f}} {hover_unit}<extra></extra>"),
        ))
    top = max(values) if values else 0
    fig.update_layout(barmode="overlay")
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=ids,
                     title=dict(text=L("Zone"), standoff=10),
                     showgrid=False, showline=True, ticks="outside", ticklen=4)
    fig.update_yaxes(range=[0, max(top * 1.25, 10)], ticksuffix=axis_suffix,
                     title=dict(text=axis_title, standoff=14) if axis_title else None,
                     gridcolor=c["grid"], zeroline=False, showline=True, ticks="outside", ticklen=4)
    _round_bars(fig)
    return fig


def chart_severity_donut(zones):
    c = _chart_colors()
    counts = {s: sum(1 for z in zones if severity_class(z["severity"]) == s) for s in SEV_ORDER}
    present = [s for s in SEV_ORDER if counts[s] > 0]
    fig = go.Figure(go.Pie(
        labels=[severity_label(s) for s in present],
        values=[counts[s] for s in present],
        marker=dict(colors=[SEV_CHART_COLORS[s] for s in present], line=dict(color=c["gap"], width=3)),
        hole=0.68, sort=False, direction="clockwise", textinfo="none",
        domain=dict(x=[0.05, 0.58], y=[0.0, 0.88]),
        hovertemplate=f"<b>%{{label}}</b><br>%{{value}} {L('zones')} (%{{percent}})<extra></extra>",
        title=dict(text=f"<b style='font-size:30px'>{counts['none']}/{len(zones)}</b><br>"
                        f"<span style='font-size:12px'>{L('healthy zones')}</span>",
                   position="middle center", font=dict(color=c["text"], size=14)),
    ))
    style_chart(fig, L("Field Health Overview"), L("How the 9 zones are doing"), legend=True)
    fig.update_layout(legend=dict(orientation="v", yref="paper", y=0.42, yanchor="middle",
                                  xref="paper", x=0.68, xanchor="left",
                                  font=dict(color=c["text"], size=14), bgcolor="rgba(0,0,0,0)"))
    return fig


def chart_canopy_by_zone(zones):
    values = [float(z.get("affected_canopy_pct", 0.0)) for z in zones]
    fig = _severity_bar_fig(zones, values, "{:.1f}%", "%", axis_suffix="%")
    return style_chart(fig, L("Crop Area Affected by Zone"), L("Share of crop affected in each zone"),
                       height=360, legend=True)


def chart_spray_dose(zones):
    values = [float(z.get("dose_liters_per_ha", 0.0)) for z in zones]
    fig = _severity_bar_fig(zones, values, "{:.1f}", L("litres/hectare"))
    return style_chart(fig, L("Spray Amount by Zone"), L("Medicine needed in each zone (litres/hectare)"),
                       height=360, legend=True)


def pest_detection_counts(rep):
    def valid(raw):
        r = str(raw or "").strip().lower()
        return r not in ("", "none", "null") and "healthy" not in r

    stats = {}
    for z in rep.get("zones", []):
        seen = {}
        breakdown = (z.get("raw") or {}).get("zone_class_breakdown") or {}
        for cls, cnt in breakdown.items():
            try:
                cnt = float(cnt)
            except (TypeError, ValueError):
                continue
            if cnt > 0 and valid(cls):
                name = friendly_pest_name(cls)
                seen[name] = seen.get(name, 0) + cnt
        for cls in z.get("detected_classes", []) or []:
            if valid(cls):
                seen.setdefault(friendly_pest_name(cls), 1)
        for name, cnt in seen.items():
            s = stats.setdefault(name, {"count": 0, "zones": 0})
            s["count"] += cnt
            s["zones"] += 1
    ranked = [(n, s["count"], s["zones"]) for n, s in stats.items()]
    return sorted(ranked, key=lambda r: (r[1], r[2]), reverse=True)


def chart_pest_counts(ranked, top_n=8):
    c = _chart_colors()
    rows = ranked[:top_n]
    if not rows:
        fig = go.Figure()
        fig.add_annotation(text=f"🌿<br><b style='font-size:16px'>{L('No pests detected in this video')}</b>",
                           x=0.5, y=0.45, xref="paper", yref="paper", showarrow=False,
                           font=dict(color=c["text"], size=26), align="center")
        fig.update_xaxes(visible=False)
        fig.update_yaxes(visible=False)
        return style_chart(fig, L("Pests Detected in This Video"), L("How many times each pest was seen"))
    names = [r[0] for r in rows]
    vals = [r[1] for r in rows]
    fig = go.Figure(go.Bar(
        x=names, y=vals, width=[0.42] * len(names),
        marker=dict(color="#10B981", line=dict(width=0)),
        text=[f"{int(v):,}" for v in vals], textposition="outside",
        textfont=dict(color=c["text"], size=13), cliponaxis=False,
        customdata=[r[2] for r in rows],
        hovertemplate=(f"<b>%{{x}}</b><br>%{{y:,.0f}} {L('times seen')}"
                       f"<br>{L('Seen in')} %{{customdata}} {L('zones')}<extra></extra>"),
    ))
    fig.update_xaxes(type="category", showgrid=False, showline=True, ticks="outside", ticklen=4,
                     tickfont=dict(color=c["text"], size=13))
    fig.update_yaxes(range=[0, max(vals) * 1.25], rangemode="tozero", dtick=1 if max(vals) <= 8 else None,
                     gridcolor=c["grid"], zeroline=False, showline=True, ticks="outside", ticklen=4)
    _round_bars(fig)
    return style_chart(fig, L("Pests Detected in This Video"), L("How many times each pest was seen"))


with st.sidebar:
    st.markdown(
        f"""
        <div class="sidebar-brand" style="display: flex; align-items: center; gap: 12px; margin-bottom: 20px; padding-top: 0; padding-bottom: 14px; border-bottom: 1px solid {'#334155' if theme == 'dark' else '#E2E8F0'};">
            <span class="brand-logo" style="font-size: 32px;">🌾</span>
            <div class="sidebar-brand-text">
                <div class="sidebar-brand-title">AgriVision</div>
                <div class="sidebar-brand-subtitle">{L('SMART FARMING HELPER')}</div>
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
        _icon, _, _label = page_name.partition(" ")
        if st.button(
            _label,
            icon=_icon,
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
            <div class="sidebar-status" style="background: {'rgba(16,185,129,0.1)' if problem_count == 0 else 'rgba(239,68,68,0.1)'};
                        border: 1px solid {'#10B981' if problem_count == 0 else '#EF4444'};
                        border-radius: 12px; padding: 12px; font-size: 12px;">
                <strong style="color: {'#10B981' if problem_count == 0 else '#EF4444'};">
                    {L('✅ Field Healthy') if problem_count == 0 else L('⚠️ {n} Problem Zones').format(n=problem_count)}
                </strong>
                <p style="margin: 4px 0 0 0; font-size: 11px; opacity: 0.8;">
                    {L('Video checked:')} {rep.get('metadata', {}).get('filename', 'Video')}
                </p>
            </div>
            """,
            unsafe_allow_html=True
        )


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
            <span class="top-bar-logo"><span class="brand-logo">🌾</span></span>
            <div>
                <div class="top-app-bar-title">{t["app_title"]}</div>
                <div class="top-app-bar-subtitle">{t["app_subtitle"]}</div>
            </div>
        </div>
        <div class="top-app-bar-page-badge">
            <span class="live-dot"></span>{current_label}
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


if st.session_state.current_page == "home":
    st.markdown(
        f"""
        <div class="hero-banner">
            <div>
                <span class="hero-tag-pill">{L('🛰️ DRONE CROP WATCH')}</span>
                <span class="hero-tag-pill">{L('🌱 SMART PEST & CROP HEALTH CHECK')}</span>
                <span class="hero-tag-pill">{L('💦 RIGHT AMOUNT OF SPRAY')}</span>
            </div>
            <div class="hero-title-row">
                <div class="hero-title">{t["hero_title"]}</div>
            </div>
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
                <div class="agri-card metric-card">
                    <div class="metric-label">{t["metric_total_zones"]}</div>
                    <div class="metric-value" style="color: #3B82F6;">{total_zones}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with mcol2:
            st.markdown(
                f"""
                <div class="agri-card metric-card">
                    <div class="metric-label">{t["metric_problem_zones"]}</div>
                    <div class="metric-value" style="color: {'#10B981' if problem_zones == 0 else '#EF4444'};">{problem_zones}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with mcol3:
            st.markdown(
                f"""
                <div class="agri-card metric-card">
                    <div class="metric-label">{t["metric_spray_zones"]}</div>
                    <div class="metric-value" style="color: {'#10B981' if spray_zones == 0 else '#F59E0B'};">{spray_zones}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with mcol4:
            st.markdown(
                f"""
                <div class="agri-card metric-card">
                    <div class="metric-label">{t["metric_health_status"]}</div>
                    <div style="font-size: 26px; font-weight: 800; line-height: 1.2; margin-top: 6px; color: {'#10B981' if problem_zones == 0 else '#EF4444'};">{health_str}</div>
                </div>
                """,
                unsafe_allow_html=True
            )

    if st.session_state.report is not None and HAS_PLOTLY:
        g1, g2 = st.columns(2)
        with g1:
            show_chart(chart_severity_donut(st.session_state.report["zones"]), "chart_home_donut")
        with g2:
            show_chart(chart_pest_counts(pest_detection_counts(st.session_state.report)), "chart_home_pests")

    st.markdown(f"### {t['upload_heading']}")

    project_dir = Path(__file__).resolve().parent
    demo_files = []
    if project_dir.exists():
        for f in project_dir.glob("*.mp4"):
            if "annotated" not in f.name.lower() and "output" not in f.name.lower():
                demo_files.append(f.name)

    st.session_state.setdefault("active_video", None)
    st.session_state.setdefault("active_video_sig", None)
    st.session_state.setdefault("home_video_key", 0)
    vkey = st.session_state.home_video_key

    if demo_files:
        col_up, col_demo = st.columns([2, 1])
    else:
        col_up, col_demo = st.container(), None

    with col_up:
        uploaded_file = st.file_uploader(t["upload_label"], type=["mp4", "avi", "mov"], key=f"home_uploader_{vkey}")

    selected_demo = None
    if col_demo is not None:
        with col_demo:
            selected_demo = st.selectbox(t["demo_select_label"], ["-- Select Demo --"] + demo_files, key=f"home_demo_{vkey}", format_func=lambda x: L("-- Select Demo --") if x == "-- Select Demo --" else x)

    if uploaded_file is not None:
        sig = ("upload", uploaded_file.name, uploaded_file.size)
        if st.session_state.active_video_sig != sig:
            tfile = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            tfile.write(uploaded_file.getvalue())
            tfile.close()
            st.session_state.active_video = {"path": tfile.name, "name": uploaded_file.name}
            st.session_state.active_video_sig = sig
    elif selected_demo and selected_demo != "-- Select Demo --":
        sig = ("demo", selected_demo)
        if st.session_state.active_video_sig != sig:
            st.session_state.active_video = {"path": str(project_dir / selected_demo), "name": selected_demo}
            st.session_state.active_video_sig = sig

    active = st.session_state.active_video
    if active and not os.path.exists(active["path"]):
        active = None
        st.session_state.active_video = None
        st.session_state.active_video_sig = None

    target_video_path = active["path"] if active else None
    if active:
        vc1, vc2 = st.columns([5, 1])
        with vc1:
            st.caption(f"🎞️ {L('Selected video:')} **{active['name']}**")
        with vc2:
            if st.button(L("🗑️ Remove"), key="btn_remove_video", use_container_width=True):
                st.session_state.active_video = None
                st.session_state.active_video_sig = None
                st.session_state.home_video_key += 1
                st.rerun()
        st.video(target_video_path)

    st.markdown("<br>", unsafe_allow_html=True)

    if target_video_path:
        if get_outer_polygon() is None:
            st.warning(L("Please mark your field on the map first (Field Map & Location page). The check is done only inside your field."))
        elif (st.session_state.report is not None
              and st.session_state.get("report_video_sig") == st.session_state.active_video_sig
              and report_matches_boundary()):
            if st.button(t["view_full_report"], type="primary", use_container_width=True, key="btn_view_full_report"):
                st.session_state.current_page = "home"
                st.session_state.scroll_home_top = True
                st.rerun()
        elif st.button(t["btn_run_pipeline"], type="primary", use_container_width=True):
            pipeline_done = False
            progress_bar = st.progress(0)
            status_text = st.empty()
            status_text.info(t["processing_msg"])

            def progress_cb(current, total):
                if total > 0:
                    pct = min(1.0, current / total)
                    progress_bar.progress(pct)
                    status_text.text(L("Checking picture {c} of {n} ({p}%)...").format(c=current, n=total, p=int(pct*100)))

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
                st.session_state.report_video_sig = st.session_state.active_video_sig
                pipeline_done = True

                progress_bar.progress(1.0)
                status_text.success(L("✅ Field check finished successfully."))
                progress_bar.empty()
            except Exception as e:
                status_text.empty()
                progress_bar.empty()
                st.error(L("Something went wrong while checking your video. Please try again, or try a different video."))
                with st.expander(L("Details for the support team")):
                    st.code(str(e))

            if pipeline_done:
                st.rerun()

    if st.session_state.report is not None:
        st.markdown("---")
        st.markdown(
            f"""
            <div style="background: rgba(16,185,129,0.1); border: 1px solid #10B981; border-radius: 16px; padding: 20px; display: flex; align-items: center; justify-content: space-between;">
                <div>
                    <h3 style="margin: 0; color: #10B981; font-weight: 700;">{L('Your Crop Report Is Ready')}</h3>
                    <p style="margin: 4px 0 0 0; opacity: 0.8;">{L('See the marked video, field map, problem areas and spray advice from the menu on the left.')}</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )
        if st.session_state.pop("scroll_home_top", False):
            components.html("""<script>
            (function () {
              var p = window.parent, d = p.document;
              function go() {
                ['[data-testid="stMain"]', 'section.main', '[data-testid="stAppViewContainer"]', '.main'].forEach(function (s) {
                  var el = d.querySelector(s);
                  if (el) { el.scrollTop = 0; try { el.scrollTo({top: 0, left: 0, behavior: 'instant'}); } catch (e) {} }
                });
                p.scrollTo(0, 0);
              }
              go(); setTimeout(go, 100); setTimeout(go, 350); setTimeout(go, 800);
            })();
            </script>""", height=0)


elif st.session_state.current_page == "visual_report":
    if st.session_state.report is None:
        render_empty_state()
    else:
        rep = st.session_state.report
        zones = rep["zones"]
        st.markdown(f"### {L('📊 Marked Video & Field Summary')}")

        _kc = {"none": "#16A34A", "low": "#EAB308", "medium": "#EA580C", "high": "#DC2626"}

        _worst = "none"
        _cnt = {"none": 0, "low": 0, "medium": 0, "high": 0}
        for _z in zones:
            _c = severity_class(_z["severity"])
            _cnt[_c] = _cnt.get(_c, 0) + 1
            if _c in SEV_ORDER and SEV_ORDER.index(_c) > SEV_ORDER.index(_worst):
                _worst = _c
        _total = len(zones)
        _need = sum(1 for _z in zones if severity_class(_z["severity"]) != "none")
        _healthy = _total - _need
        _pct = int(round(100.0 * _healthy / _total)) if _total else 0

        st.markdown(flat(
            f'<div class="vr-hero severity-{_worst}">'
            f'<div><div class="vr-b-lbl">{esc(L("Overall Field Health"))}</div>'
            f'<div class="vr-b-val">{esc(t["sev_" + _worst])}</div>'
            f'<div class="vr-b-sub">{esc(L("{n} of {t} zones need attention").format(n=_need, t=_total))}</div></div>'
            f'<div><div class="vr-b-lbl">{esc(L("Healthy zones"))} &middot; {_healthy}/{_total}</div>'
            f'<div class="vr-meter"><span style="width:{_pct}%"></span></div>'
            f'<div class="vr-b-sub">{_pct}%</div></div></div>'
        ), unsafe_allow_html=True)

        _frames = rep.get("metadata", {}).get("total_frames_analyzed", "N/A")
        _cards = "".join(
            f'<div class="kpi" style="--kc:{_kc[k]}"><div class="num">{_cnt[k]}</div><div class="lbl">{esc(t["sev_" + k])}</div></div>'
            for k in ("none", "low", "medium", "high")
        )
        _cards += (f'<div class="kpi" style="--kc:#0EA5E9"><div class="num">{esc(_frames)}</div>'
                   f'<div class="lbl">{esc(L("Pictures Checked from Video:").rstrip(":"))}</div></div>')
        st.markdown(flat(f'<div class="kpi-strip">{_cards}</div>'), unsafe_allow_html=True)

        c_vid, c_grid = st.columns([3, 2], gap="medium")

        with c_vid:
            ann_path = rep.get("annotated_video_path")
            if ann_path and os.path.exists(ann_path):
                st.markdown('<div class="vr-card-anchor"></div>', unsafe_allow_html=True)
                st.markdown(flat(
                    f'<div class="vr-card-head"><div class="vr-card-title">{esc(L("🎬 Marked Video"))}</div>'
                    f'<span class="vr-pill">{esc(L("Fixed 3x3 grid"))}</span></div>'
                ), unsafe_allow_html=True)
                st.markdown(
                    "<style>[data-testid='stElementContainer']:has(video), .element-container:has(video) "
                    "{ border-radius: 16px; overflow: hidden; } video { display: block; width: 100%; height: auto; }</style>",
                    unsafe_allow_html=True,
                )
                st.video(ann_path)
                _legend = "".join(
                    f'<span><i class="vr-dot" style="--c:{_kc[k]}"></i>{esc(t["sev_" + k])}</span>'
                    for k in ("none", "low", "medium", "high")
                )
                st.markdown(flat(f'<div class="vr-legend">{_legend}</div>'), unsafe_allow_html=True)
                with open(ann_path, "rb") as f:
                    st.download_button(
                        label=L("⬇️ Download Marked Video"),
                        data=f.read(),
                        file_name=os.path.basename(ann_path),
                        mime="video/mp4",
                        use_container_width=True
                    )
            else:
                st.warning(L("The marked video could not be found. Please run the Field Check again."))

        with c_grid:
            st.markdown('<div class="vr-card-anchor"></div>', unsafe_allow_html=True)
            st.markdown(flat(
                f'<div class="vr-card-head"><div class="vr-card-title">{esc(L("Field Health at a Glance (9 Zones)"))}</div></div>'
            ), unsafe_allow_html=True)

            tiles = []
            for z in zones:
                zid = z["zone_id"]
                sev = z["severity"]
                pest_list = ", ".join(friendly_pest_list(z.get("detected_classes", []))) or t["no_pests"]
                dose = z.get("spray_dose_percent", 0) or 0
                dose_html = f'<div class="zt-dose">{esc(L("Spray"))} {esc(dose)}%</div>' if dose else ""
                tiles.append(
                    f'<div class="zone-tile vr-tile severity-{severity_class(sev)}">'
                    f'<div class="zt-id">{esc(L("ZONE"))} {esc(zid)}</div>'
                    f'<div class="zt-sev">{esc(severity_label(sev))}</div>'
                    f'{dose_html}'
                    f'<div class="zt-pest" title="{esc(pest_list)}">{esc(pest_list)}</div>'
                    f'</div>'
                )
            st.markdown(flat('<div class="vr-matrix">' + "".join(tiles) + "</div>"), unsafe_allow_html=True)

            _pests = {}
            for _z in zones:
                for _n in friendly_pest_list(_z.get("detected_classes", [])):
                    _pests[_n] = _pests.get(_n, 0) + 1
            if _pests:
                _chips = "".join(f'<span class="vr-chip">{esc(_n)} <b>x{_c}</b></span>'
                                 for _n, _c in sorted(_pests.items(), key=lambda kv: -kv[1])[:8])
                st.markdown(flat(f'<div class="vr-chips-title">{esc(L("Pests found"))}</div><div class="vr-chips">{_chips}</div>'),
                            unsafe_allow_html=True)

            meta_info = rep.get('metadata', {})
            if meta_info.get("ground_fixed_zones"):
                st.caption(L("Zone 1 to Zone 9 always mean the same part of the field, even when the drone moves."))
            if meta_info.get("motion_tracking_failures", 0) > 0:
                st.warning(L("The video was too shaky or blurry in {n} place(s), so those parts may be a little less accurate. A clearer, steadier video gives better results.").format(n=meta_info['motion_tracking_failures']))

        tab_rows, tab_chart = st.tabs([L("📋 Zone Details"), L("📈 Charts")])

        with tab_rows:
            _ordered = sorted(zones, key=lambda z: (-SEV_ORDER.index(severity_class(z["severity"])), z["zone_id"]))
            _rows = []
            for z in paginate_items(_ordered, "vr_rows"):
                _sc = severity_class(z["severity"])
                _canopy = z.get("affected_canopy_pct", 0.0) or 0.0
                _dose = z.get("spray_dose_percent", 0) or 0
                _rows.append(
                    f'<div class="vr-row" style="--c:{_kc[_sc]}">'
                    f'<div class="vr-zid">Z{esc(z["zone_id"])}</div>'
                    f'<div><div class="vr-zt">{esc(severity_label(z["severity"]))}</div>'
                    f'<div class="vr-zd">{esc(z.get("problem_description", ""))}</div></div>'
                    f'<div class="vr-zs"><span class="vr-pill">{esc(L("Crop affected"))} {esc(_canopy)}%</span>'
                    f'<span class="vr-pill">{esc(L("Spray"))} {esc(_dose)}%</span></div>'
                    f'</div>'
                )
            st.markdown(flat('<div class="vr-rows">' + "".join(_rows) + "</div>"), unsafe_allow_html=True)
            render_page_controls(len(_ordered), "vr_rows")

        with tab_chart:
            if HAS_PLOTLY:
                show_chart(chart_canopy_by_zone(zones), "chart_visual_canopy")


elif st.session_state.current_page == "gps":
    st.markdown(f"### {L('📍 Field Map & Location')}")
    st.caption(L("Mark your field on the map. After the field check, the problem spots, the spray area and the spray patches are shown automatically."))

    with st.expander(L("⚙️ Grid settings"), expanded=False):
        pc1, pc2 = st.columns(2)
        with pc1:
            st.session_state.inner_margin_m = st.number_input(
                L("Edge Gap (metres)"), min_value=0.0, max_value=500.0, step=1.0,
                help=L("How far the spray area stays away from the edge of your field."),
                value=float(st.session_state.inner_margin_m), key="w_inner_margin"
            )
        with pc2:
            st.session_state.grid_cell_m = st.number_input(
                L("Spray Patch Size (metres)"), min_value=1.0, max_value=500.0, step=1.0,
                help=L("Size of each small square patch that gets sprayed."),
                value=float(st.session_state.grid_cell_m), key="w_grid_cell"
            )

    outer_poly = get_outer_polygon()
    gis = get_gis_state()
    if st.session_state.report is not None and outer_poly is not None and not report_matches_boundary():
        st.warning(L("You changed your field outline after the last check. Please run the Field Check again on the Home page so the results match your new outline."))

    _gis_spots = len(gis["infected"]) if gis is not None else 0
    _gis_patches = len(gis["grid"]) if gis is not None else 0
    _n_zones = len(st.session_state.report["zones"]) if st.session_state.report is not None else 0
    st.markdown(flat(
        '<div class="kpi-strip">'
        f'<div class="kpi" style="--kc:#10B981"><div class="num">{len(st.session_state.field_boundary)}</div><div class="lbl">{esc(L("Corners"))}</div></div>'
        f'<div class="kpi" style="--kc:#EA580C"><div class="num">{_gis_spots}</div><div class="lbl">{esc(L("Problem spots"))}</div></div>'
        f'<div class="kpi" style="--kc:#EAB308"><div class="num">{_gis_patches}</div><div class="lbl">{esc(L("Spray patches"))}</div></div>'
        f'<div class="kpi" style="--kc:#0EA5E9"><div class="num">{_n_zones}</div><div class="lbl">{esc(L("Zones"))}</div></div>'
        '</div>'
    ), unsafe_allow_html=True)

    tab_map, tab_spots, tab_spray, tab_zones = st.tabs([
        L("🗺️ Map & Outline"), L("🎯 Problem Spots"), L("💦 Spray Patches"), L("📍 Zone Locations")
    ])

    with tab_map:
        scol1, scol2 = st.columns([0.88, 0.12])
        with scol1:
            st.markdown('<div class="search-bar-anchor"></div>', unsafe_allow_html=True)
            search_query = st.text_input(
                L("Search Location"),
                placeholder=L("Search address or location..."),
                label_visibility="collapsed",
                key="map_search_input"
            )
            st.button("✕", key="search_clear_btn", help=L("Clear"), on_click=_clear_map_search)
        with scol2:
            do_search = st.button("🔍", key="search_btn_icon", use_container_width=True, help=L("Search Location"))

        trigger_search = do_search or (search_query and search_query.strip() != st.session_state.get("last_search_query", ""))

        if trigger_search and search_query and search_query.strip():
            st.session_state["last_search_query"] = search_query.strip()
            geo_cands, status, geo_msg = geocode_candidates(search_query.strip())

            if status == "success" and geo_cands:
                st.session_state.geo_candidates = geo_cands
                st.session_state.geo_query = search_query.strip()
                st.session_state.geo_applied = None
                st.session_state.pop("geo_pick", None)
            else:
                st.session_state.geo_candidates = []
                if status == "error":
                    st.error(f"⚠️ {L(geo_msg)}")
                elif status == "network_error":
                    st.error(L("⚠️ Place search is not working right now. Please check your internet connection, or type the location numbers yourself (for example 33.5673, 73.1015)."))
                else:
                    st.error(L("❌ Location '{q}' could not be found. Please type only the place name (for example Sahiwal, Pakistan), or type the location numbers yourself (for example 33.5673, 73.1015).").format(q=search_query.strip()))

        geo_cands = st.session_state.get("geo_candidates") or []
        if geo_cands:
            pick = 0
            if len(geo_cands) > 1:
                pick = st.selectbox(
                    L("Several places matched. Pick the correct one:"),
                    list(range(len(geo_cands))),
                    format_func=lambda i: geo_cands[i]["label"],
                    key="geo_pick",
                )
            chosen = geo_cands[pick]
            pick_sig = (st.session_state.get("geo_query"), pick)
            if st.session_state.get("geo_applied") != pick_sig:
                apply_geo_candidate(chosen)
                st.session_state["geo_applied"] = pick_sig
            st.success(f"📍 {L('Map moved to:')} {chosen['name']}")
            if chosen["precision"] != "exact":
                st.warning(L("⚠️ The exact place '{q}' was not found, so the pin shows the approximate area of '{m}'. Tap the map at your field to set the exact point, or type the location numbers (for example 33.5673, 73.1015).").format(
                    q=st.session_state.get("geo_query", ""), m=chosen["matched_query"]))
            elif chosen["is_area"]:
                st.info(L("ℹ️ This pin is the centre of the whole area (village / town), not your exact field. Tap the map at your field to set the exact point."))

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
            st.warning(L("Satellite view is not switched on, so the normal map is shown. (Ask your admin to add the satellite map key.)"))

        poly_coords = [[pt["lat"], pt["lon"]] for pt in st.session_state.field_boundary]
        if len(poly_coords) >= 3:
            folium.Polygon(
                locations=poly_coords,
                color="#10B981",
                fill=True,
                fill_color="#34D399",
                fill_opacity=0.2,
                weight=3,
                popup=L("🌿 Your Field (drone flying area)")
            ).add_to(m)

        for idx, pt in enumerate(st.session_state.field_boundary):
            folium.CircleMarker(
                location=[pt["lat"], pt["lon"]],
                radius=6,
                color="#047857",
                fill=True,
                fill_color="#6EE7B7",
                fill_opacity=1.0,
                popup=f"{L('Corner')} #{idx+1}: {esc(pt.get('name', 'Point'))} ({pt['lat']:.6f}, {pt['lon']:.6f})"
            ).add_to(m)

        if gis is not None:
            _zone_sev = {z["zone_id"]: severity_class(z["severity"]) for z in st.session_state.report["zones"]}
            _sev_fill = {"none": "#16A34A", "low": "#EAB308", "medium": "#EA580C", "high": "#DC2626"}
            for zid, zg in gis["zone_geo"].items():
                zsev = _zone_sev.get(zid, "none")
                for ring in zg["rings"]:
                    folium.Polygon(
                        locations=ring, color="#FFFFFF", weight=3, dash_array="8, 6",
                        fill=True, fill_color=_sev_fill[zsev], fill_opacity=0.18,
                        tooltip=f"{L('Zone')} {zid} - {severity_label(zsev)}"
                    ).add_to(m)
                if zg.get("has_area"):
                    folium.Marker(
                        location=[zg["lat"], zg["lon"]],
                        icon=folium.DivIcon(
                            icon_size=(40, 22), icon_anchor=(20, 11),
                            html=f'<div style="font-weight:800;font-size:12px;color:#fff;background:{_sev_fill[zsev]};'
                                 f'border-radius:10px;padding:1px 8px;text-align:center;border:1px solid #fff;">Z{zid}</div>'
                        )
                    ).add_to(m)
            for ring in gis["inner_rings"]:
                folium.Polygon(
                    locations=ring, color="#F59E0B", weight=3, dash_array="8, 6",
                    fill=True, fill_color="#FBBF24", fill_opacity=0.12,
                    tooltip=L("Spray area (made automatically)")
                ).add_to(m)
            for cell in gis["grid"]:
                cell_status = st.session_state.grid_cell_status.get(cell["cell_id"], "Spray Required")
                cell_color = SPRAY_STATUS_STYLES[cell_status][1]
                cell_sev = _zone_sev.get(cell["zone_id"], "none")
                for ring in cell["rings"]:
                    folium.Polygon(
                        locations=ring, color=cell_color, weight=2,
                        fill=True, fill_color=_sev_fill[cell_sev], fill_opacity=0.5,
                        tooltip=f"{cell['cell_id']} - {severity_label(cell_sev)} - {L(cell_status)}"
                    ).add_to(m)
            for ip in gis["infected"]:
                folium.Marker(
                    location=[ip["lat"], ip["lon"]],
                    popup=f"<b>{L('Problem spot')} - {L('Zone')} {ip['zone_id']}</b><br>{esc(friendly_pest_text(ip['pests']) or t['no_pests'])}<br><code>{ip['lat']:.6f}, {ip['lon']:.6f}</code>",
                    icon=folium.Icon(color="orange", icon="info-sign")
                ).add_to(m)

        if st.session_state.get("search_marker"):
            slat, slon, saddr = st.session_state.search_marker
            folium.Marker(
                location=[slat, slon],
                popup=f"<b>{L('🔍 Searched Location:')}</b><br>{esc(saddr)}<br><code>{slat:.6f}, {slon:.6f}</code>",
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
            height=480,
            key=f"folium_field_map_{st.session_state.get('map_nonce', 0)}",
            returned_objects=["last_clicked", "last_active_drawing", "all_drawings"]
        )

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
            st.info(f"📍 **{L('Location picked from the map:')}** {L('North-South')} `{st.session_state.last_captured_lat:.6f}`, {L('East-West')} `{st.session_state.last_captured_lon:.6f}`")

        st.markdown(f"#### {L('📐 1. Your Field Outline')}")
        st.caption(L("Mark the outline of the field the drone flies over. Results are shown only inside this outline. Add corner points one after another around the field."))

        boundary_notice = st.session_state.pop("boundary_notice", None)
        if boundary_notice:
            st.success("✅ " + boundary_notice)

        bcol1, bcol2 = st.columns([2, 1])
        with bcol1:
            st.markdown(f"##### {L('Corner Points of Your Field:')}")
            if st.session_state.field_boundary:
                boundary_rows = []
                for idx, pt in enumerate(st.session_state.field_boundary):
                    boundary_rows.append([
                        f"{L('Corner')} #{idx + 1}",
                        esc(pt.get("name", f"Point {idx+1}")),
                        f"<code>{pt['lat']:.6f}</code>",
                        f"<code>{pt['lon']:.6f}</code>"
                    ])
                render_custom_table([L("Corner"), L("Name"), t["table_lat"], t["table_lon"]], boundary_rows, page_key="tbl_boundary")
            else:
                st.info(L("No field outline yet. Draw a box or shape on the map and press the green button, or add corner points yourself."))

            drawn_poly = st.session_state.get("drawn_polygon")
            if drawn_poly:
                if st.button(L("🟩 Use drawn shape as my field ({n} corners)").format(n=len(drawn_poly)), type="primary", use_container_width=True, key="use_drawn_boundary"):
                    new_boundary = [
                        {"lat": lat, "lon": lon, "name": f"{L('Corner')} {i + 1}"}
                        for i, (lat, lon) in enumerate(drawn_poly)
                    ]
                    st.session_state.field_boundary = new_boundary
                    new_poly = boundary_polygon(new_boundary)
                    if new_poly is not None:
                        st.session_state.map_center, st.session_state.map_zoom = view_for_polygon(new_poly)
                    st.session_state["boundary_notice"] = L("Field outline updated from the shape you drew.")
                    st.rerun()
            else:
                st.caption(L("Draw a box or shape on the map above to use it as your field outline."))

        with bcol2:
            st.markdown(f"##### {L('Add a Corner Point:')}")
            v_name = st.text_input(L("Corner Name"), value=f"{L('Corner')} #{len(st.session_state.field_boundary)+1}")

            def coord_input(label, key, lo, hi):
                if st.session_state.get(key) is not None:
                    return st.number_input(label, min_value=lo, max_value=hi, format="%.6f", key=key)
                try:
                    return st.number_input(label, min_value=lo, max_value=hi, format="%.6f", value=None,
                                           placeholder=L("Click on the map"), key=key)
                except Exception:
                    return st.number_input(label, min_value=lo, max_value=hi, format="%.6f", value=0.0, key=key)

            v_lat = coord_input(L("Position (North-South)"), "bound_v_lat", -90.0, 90.0)
            v_lon = coord_input(L("Position (East-West)"), "bound_v_lon", -180.0, 180.0)

            btn_c1, btn_c2 = st.columns(2)
            with btn_c1:
                if st.button(L("➕ Add Corner"), type="primary", use_container_width=True):
                    if v_lat is None or v_lon is None:
                        st.error(L("Click a point on the map, or type both location numbers first."))
                    else:
                        st.session_state.field_boundary.append({"lat": float(v_lat), "lon": float(v_lon), "name": v_name})
                        st.rerun()
            with btn_c2:
                if st.button(L("🗑️ Clear Outline"), use_container_width=True):
                    st.session_state.field_boundary = []
                    st.session_state.pop("gis_cache", None)
                    st.session_state.grid_cell_status = {}
                    st.rerun()

    with tab_spots:
        st.markdown(f"#### {L('🎯 2. Problem Spots Found Automatically')}")
        st.caption(L("Marked automatically from the video results inside your field. Nothing to mark by hand."))

        if st.session_state.report is None:
            st.info(L("Run the Field Check on the Home page to see problem spots."))
        elif gis is None:
            st.info(L("Mark your field outline above to see the results on the map."))
        elif not gis["infected"]:
            st.success(L("No problem spots found inside your field."))
        else:
            inf_rows = []
            for i, ip in enumerate(gis["infected"], start=1):
                inf_rows.append([
                    f"{L('Spot')} #{i}",
                    f"{L('Zone')} {ip['zone_id']}",
                    severity_label(ip["severity"]),
                    esc(friendly_pest_text(ip["pests"]) or t["no_pests"]),
                    f"<code>{ip['lat']:.6f}</code>",
                    f"<code>{ip['lon']:.6f}</code>",
                ])
            render_custom_table([L("Spot"), L("Zone"), t["table_severity"], L("Pests Found"), t["table_lat"], t["table_lon"]], inf_rows, page_key="tbl_spots")

    with tab_spray:
        st.markdown(f"#### {L('💦 3. Spray Area & Spray Patches')}")
        st.caption(L("A spray area is drawn around the problem spots and divided into small square patches. Only these patches are sprayed."))

        if gis is None:
            st.info(L("Mark your field outline and run the Field Check to see the spray area and patches."))
        elif not gis["grid"]:
            st.success(L("No problem area was found, so no spray area is needed."))
        else:
            grid_note = L("💡 **Spray area:** {a:.0f} out of {b:.0f} square metres | **Spray patches:** {n} | **Patch size:** {c} metres").format(a=gis['inner_area_m2'], b=gis['field_area_m2'], n=len(gis['grid']), c=gis['cell_m_used'])
            if gis["cell_m_used"] != gis["cell_m_requested"]:
                grid_note += L(" (made bigger than the {r:.0f} metres you chose, to keep the number of patches reasonable)").format(r=gis['cell_m_requested'])
            st.info(grid_note)

            grid_rows = []
            for cell in gis["grid"]:
                cell_status = st.session_state.grid_cell_status.get(cell["cell_id"], "Spray Required")
                badge, color = SPRAY_STATUS_STYLES[cell_status]
                grid_rows.append([
                    f"<strong>{cell['cell_id']}</strong>",
                    f"{L('Zone')} {cell['zone_id']}",
                    f"<code>{cell['lat']:.6f}</code>",
                    f"<code>{cell['lon']:.6f}</code>",
                    severity_label(cell["severity"]),
                    f"{cell['spray_dose_percent']}%",
                    f"<span style='color: {color}; font-weight: 700;'>{L(badge)}</span>",
                ])
            render_custom_table([L("Patch"), L("Zone"), t["table_lat"], t["table_lon"], t["table_severity"], L("Spray Amount"), L("Spray Status")], grid_rows, page_key="tbl_patches")

            st.markdown(f"##### {L('Update Patch Spray Status:')}")
            sel_cell = st.selectbox(L("Select Patch:"), [c["cell_id"] for c in gis["grid"]])
            new_status = st.selectbox(
                L("Set Spray Status:"), list(SPRAY_STATUS_STYLES.keys()),
                format_func=lambda s: L(SPRAY_STATUS_STYLES[s][0])
            )
            if st.button(L("💾 Save Patch Status"), type="primary", use_container_width=True):
                st.session_state.grid_cell_status[sel_cell] = new_status
                st.rerun()

    with tab_zones:
        st.markdown(f"#### {L('📍 4. Zone Locations')}")
        if st.session_state.report is None:
            st.info(L("Run the Field Check on the Home page to see the location of every zone."))
        elif gis is None:
            st.info(L("Mark your field outline above to see the location of each zone."))
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
                alt_str = L("4.5 m (set automatically)")

                rows.append([
                    f"<strong>{L('Zone')} {zid}</strong>",
                    f"<code>{z_lat:.6f}</code>",
                    f"<code>{z_lon:.6f}</code>",
                    f"{dist_m} m",
                    fence_badge,
                    alt_str
                ])
                gps_export_data.append({"Zone": zid, "Latitude": z_lat, "Longitude": z_lon, "Distance_Meters": dist_m, "Inside_Field_Boundary": is_inside, "Severity": z["severity"]})

            render_custom_table(headers, rows, page_key="tbl_zone_gps")

            df_gps = pd.DataFrame(gps_export_data)
            csv_data = df_gps.to_csv(index=False).encode('utf-8')
            st.download_button(
                label=L("⬇️ Download Zone Locations (opens in Excel)"),
                data=csv_data,
                file_name="AgriVision_Zone_GPS.csv",
                mime="text/csv"
            )


elif st.session_state.current_page == "problems":
    if st.session_state.report is None:
        render_empty_state()
    else:
        rep = st.session_state.report
        st.markdown(f"### {L('🔍 Problem Areas')}")
        st.write(L("Zone-by-zone details, shown on colour-coded cards."))
        st.caption(L("C001, C002, ... are small spray patches (little squares of the field) in each zone. Red = still to be sprayed. Hover over a patch to see its status."))

        zones = rep["zones"]

        cols = st.columns(3)

        for idx, z in enumerate(paginate_items(zones, "problems")):
            zid = z["zone_id"]
            sev = z["severity"]
            raw_desc = describe_problem(z)
            clean_desc = sanitize_problem_description(raw_desc, sev)
            pests = z.get("detected_classes", [])
            pests_str = ", ".join(friendly_pest_list(pests)) if pests else t["no_pests"]
            canopy_pct = z.get("affected_canopy_pct", 0.0)

            card_class = f"severity-{severity_class(sev)}"

            sub_list = get_sub_areas_for_zone(zid, sev, z["spray_needed"])
            sub_badges_html = " ".join([
                f"<span title='{esc(L(sa['status']))}' style='font-size: 10px; padding: 2px 6px; border-radius: 8px; background: {sa['color']}; color: #FFFFFF; font-weight: 700;'>{esc(sa['sub_id'])}</span>"
                for sa in sub_list
            ])

            col_target = cols[idx % 3]
            with col_target:
                col_target.markdown(
                    flat(f"""
                    <div class="zone-card {card_class}">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <strong style="font-size: 16px;">📍 {L('ZONE')} {esc(zid)}</strong>
                            <span style="text-transform: uppercase; font-weight: 800; font-size: 12px; padding: 4px 10px; border-radius: 12px; background: rgba(0,0,0,0.15);">
                                {severity_label(sev)}
                            </span>
                        </div>
                        <p style="margin: 8px 0; font-size: 14px; line-height: 1.5; font-weight: 500;">
                            {esc(clean_desc)}
                        </p>
                        <div style="font-size: 12px; opacity: 0.9; margin-top: 12px; border-top: 1px solid rgba(0,0,0,0.15); padding-top: 8px;">
                            <div>🐛 <strong>{L('Pests Found:')}</strong> {esc(pests_str)}</div>
                            <div>🍂 <strong>{L('Crop Area Affected:')}</strong> {canopy_pct:.1f}%</div>
                            <div style="margin-top: 6px; font-size: 11px;">💦 <strong>{L('Spray Patches')} ({len(sub_list)}):</strong></div>
                            <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px;">{sub_badges_html}</div>
                        </div>
                    </div>
                    """),
                    unsafe_allow_html=True
                )

        render_page_controls(len(zones), "problems")


elif st.session_state.current_page == "spray":
    if st.session_state.report is None:
        render_empty_state()
    else:
        rep = st.session_state.report
        st.markdown(f"### {L('💦 Spray Advice & Medicine Amount')}")

        zones = rep["zones"]

        spray_attention_zones = [z for z in zones if z.get("spray_needed") or z.get("severity", "none") != "none"]

        spray_zones_count = len(spray_attention_zones)
        total_dose = sum(z.get("dose_liters_per_ha", 0.0) for z in spray_attention_zones)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                f"""
                <div class="agri-card">
                    <div class="metric-label">{L('Zones Needing Spray or Care')}</div>
                    <div class="metric-value" style="color: #F59E0B;">{spray_zones_count} / {len(zones)}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
        with c2:
            st.markdown(
                f"""
                <div class="agri-card">
                    <div class="metric-label">{L('Total Spray Amount (Estimate)')}</div>
                    <div class="metric-value" style="color: #10B981;">{total_dose:.1f} {L('litres/hectare')}</div>
                </div>
                """,
                unsafe_allow_html=True
            )

        if spray_attention_zones and HAS_PLOTLY:
            show_chart(chart_spray_dose(spray_attention_zones), "chart_spray_dose")

        if not spray_attention_zones:
            st.markdown(
                f"""
                <div class="agri-card" style="padding: 24px; text-align: center;">
                    <h3 style="color: #10B981; margin: 0;">{L('✅ All Field Areas are Healthy')}</h3>
                    <p style="margin: 8px 0 0 0; opacity: 0.8;">{L('No zone needs spraying or care.')}</p>
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
                chem = zone_chemical(z)

                action_badge = f"<span style='color: #EF4444; font-weight: 800;'>{L('SPRAY NEEDED')}</span>" if spray else f"<span style='color: #F59E0B; font-weight: 700;'>{L('KEEP WATCHING')}</span>"
                flow_rate = f"{round(dose * 0.4, 2)} {L('litres/min')}"

                rows.append([
                    f"<strong>{L('Zone')} {zid}</strong>",
                    f"<span style='text-transform: uppercase; font-weight: 700;'>{severity_label(sev)}</span>",
                    action_badge,
                    f"{dose:.1f} {L('litres/hectare')}",
                    chem,
                    flow_rate
                ])

            render_custom_table(headers, rows, page_key="tbl_spray_zones")

            st.markdown(f"#### {L('🎯 Spray Patches by Zone')}")
            st.caption(L("Each small tag is one spray patch (C001, C002, ...) inside the spray area. Hover over it to see its map location."))
            for z in paginate_items(spray_attention_zones, "spray_zones"):
                zid = z["zone_id"]
                sev = z["severity"]
                sub_list = [sa for sa in get_sub_areas_for_zone(zid, sev, z["spray_needed"])]
                if not sub_list:
                    continue

                total_c = len(sub_list)
                n_pending = sum(1 for sa in sub_list if sa["status"] == "Spray Required")
                n_active = sum(1 for sa in sub_list if sa["status"] == "Spraying In Progress")
                n_done = sum(1 for sa in sub_list if sa["status"] == "Sprayed")
                pct_done = int(round(100 * n_done / total_c))

                chips = "".join(
                    f'<span class="cell-chip" style="--c: {sa["color"]};" '
                    f'title="{esc(sa["sub_id"])} | {esc(L(sa["status"]))} | {esc(sa["quadrant"])}">{esc(sa["sub_id"])}</span>'
                    for sa in sub_list
                )

                st.markdown(
                    flat(f"""
                    <div class="spray-zone-card">
                        <div class="spray-zone-head">
                            <div class="spray-zone-title">📍 {L('Zone')} {esc(zid)}
                                <span class="spray-sev severity-{severity_class(sev)}">{esc(severity_label(sev))}</span>
                            </div>
                            <span class="spray-zone-count">{total_c} {L('patches')}</span>
                        </div>
                        <div class="spray-progress"><div class="spray-progress-fill" style="width: {pct_done}%;"></div></div>
                        <div class="spray-stats">
                            <span class="stat" style="--c: #EF4444;">{n_pending} {L('Pending')}</span>
                            <span class="stat" style="--c: #F59E0B;">{n_active} {L('Active')}</span>
                            <span class="stat" style="--c: #10B981;">{n_done} {L('Sprayed')} ({pct_done}%)</span>
                        </div>
                        <div class="cell-grid">{chips}</div>
                    </div>
                    """),
                    unsafe_allow_html=True
                )

            render_page_controls(len(spray_attention_zones), "spray_zones")


elif st.session_state.current_page == "pests":
    st.markdown(f"### {L('🐛 Crop Pest & Disease Guide')}")
    st.write(L("Simple explanations and advice for farmers."))

    if st.session_state.report is not None:
        rep = st.session_state.report
        detected_set = set()
        for z in rep["zones"]:
            for p in z.get("detected_classes", []):
                detected_set.add(friendly_pest_name(p))

        if detected_set:
            st.markdown(
                f"""
                <div style="background: rgba(245, 158, 11, 0.15); border: 1px solid #F59E0B; border-radius: 16px; padding: 16px; margin-bottom: 24px;">
                    <h4 style="margin: 0 0 8px 0; color: #D97706; font-size: 15px;">⚠️ {L('Pests Found in Your Video:')}</h4>
                    <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                        {"".join([f'<span style="background: #F59E0B; color: #FFFFFF; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 700;">{esc(p)}</span>' for p in sorted(detected_set)])}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

    pcols = st.columns(2)
    items = list(PEST_ACCESSIBLE_INFO.items())

    for idx, (key, info) in enumerate(paginate_items(items, "pests")):
        name = info["name_ur"] if lang == "ur" else info["name"]
        desc = info["desc_ur"] if lang == "ur" else info["desc"]

        target_col = pcols[idx % 2]
        with target_col:
            target_col.markdown(
                f"""
                <div class="agri-card">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span style="font-size: 28px;">{info['icon']}</span>
                            <strong style="font-size: 17px;">{name}</strong>
                        </div>
                        <span style="font-size: 11px; font-weight: 700; background: rgba(0,0,0,0.06); padding: 4px 10px; border-radius: 10px;">
                            {L(info['risk_badge'])}
                        </span>
                    </div>
                    <p style="font-size: 13.5px; color: { '#CBD5E1' if theme == 'dark' else '#2F5F58' }; line-height: 1.5; margin-bottom: 12px;">
                        {desc}
                    </p>
                    <div style="font-size: 12px; background: { '#334155' if theme == 'dark' else '#E6F5E3' }; padding: 10px; border-radius: 10px;">
                        💊 <strong>{L('What To Do:')}</strong> {info['treatment_ur'] if lang == 'ur' else info['treatment']}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

    render_page_controls(len(items), "pests")


elif st.session_state.current_page == "settings":
    st.markdown(f"### {t['settings_title']}")

    c1, c2 = st.columns(2)

    with c1:
        st.markdown(f"#### {t['theme_toggle_label']}")
        theme_choice = st.radio(
            L("Select Theme Mode:"),
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
            L("Select Language:"),
            [t["lang_en"], t["lang_ur"]],
            index=0 if st.session_state.language == "en" else 1
        )
        new_lang = "en" if lang_choice == t["lang_en"] else "ur"
        if new_lang != st.session_state.language:
            st.session_state.language = new_lang
            st.rerun()