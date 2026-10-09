"""
Unified Dataset Downloader and Preprocessor for AgriVision YOLO Models
---------------------------------------------------------------------
1. Downloads PlantDoc Object Detection dataset (real leaf disease bounding boxes).
2. Merges PlantDoc (27 classes) with IP102 (102 pest classes) into a unified 129-class ground detection dataset.
3. Prepares Agriculture-Vision dataset for aerial crop stress model training.
"""

import os
import shutil
import urllib.request
import zipfile
import random
import yaml
import xml.etree.ElementTree as ET
from pathlib import Path

DATASETS_DIR = Path("datasets")
GROUND_UNIFIED_DIR = DATASETS_DIR / "unified_ground"
PLANTDOC_DIR = DATASETS_DIR / "plantdoc_raw"

def get_win_path(p):
    """Helper to wrap Path object with Windows \\?\ extended prefix for open/copy operations."""
    return "\\\\?\\" + str(Path(p).resolve())

def download_file(url, target_path):
    print(f"Downloading {url} to {target_path}...")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response, open(get_win_path(target_path), 'wb') as out_file:
        shutil.copyfileobj(response, out_file)
    print("Download complete!")

def prep_plantdoc():
    zip_path = DATASETS_DIR / "plantdoc.zip"
    url = "https://github.com/pratikkayal/PlantDoc-Object-Detection-Dataset/archive/refs/heads/master.zip"
    
    if not PLANTDOC_DIR.exists():
        if not zip_path.exists():
            download_file(url, zip_path)
        print("Extracting PlantDoc dataset safely for Windows path limits...")
        base_out = DATASETS_DIR.resolve()
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.infolist():
                target_p = base_out / member.filename
                abs_str = "\\\\?\\" + str(target_p.resolve())
                if member.is_dir():
                    os.makedirs(abs_str, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(abs_str), exist_ok=True)
                    try:
                        with zf.open(member) as source, open(abs_str, "wb") as target:
                            shutil.copyfileobj(source, target)
                    except Exception as e:
                        print(f"Warning: Skipping problematic file {member.filename}: {e}")
        
        extracted_folder = DATASETS_DIR / "PlantDoc-Object-Detection-Dataset-master"
        if extracted_folder.exists():
            extracted_folder.rename(PLANTDOC_DIR)
        print("PlantDoc extracted successfully!")

def parse_voc_xml(xml_path, class_mapping):
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        
        size = root.find("size")
        if size is None:
            return []
        w_elem = size.find("width")
        h_elem = size.find("height")
        if w_elem is None or h_elem is None:
            return []
            
        width = float(w_elem.text)
        height = float(h_elem.text)
        
        if width <= 0 or height <= 0:
            return []
            
        yolo_boxes = []
        for obj in root.findall("object"):
            cname_elem = obj.find("name")
            if cname_elem is None or not cname_elem.text:
                continue
            cname = cname_elem.text.strip()
            if cname not in class_mapping:
                class_mapping[cname] = len(class_mapping)
            cid = class_mapping[cname]
            
            bndbox = obj.find("bndbox")
            if bndbox is None:
                continue
            xmin = float(bndbox.find("xmin").text)
            ymin = float(bndbox.find("ymin").text)
            xmax = float(bndbox.find("xmax").text)
            ymax = float(bndbox.find("ymax").text)
            
            bw = xmax - xmin
            bh = ymax - ymin
            cx = xmin + bw / 2.0
            cy = ymin + bh / 2.0
            
            norm_cx = max(0.0, min(1.0, cx / width))
            norm_cy = max(0.0, min(1.0, cy / height))
            norm_bw = max(0.0, min(1.0, bw / width))
            norm_bh = max(0.0, min(1.0, bh / height))
            
            yolo_boxes.append(f"{cid} {norm_cx:.6f} {norm_cy:.6f} {norm_bw:.6f} {norm_bh:.6f}")
            
        return yolo_boxes
    except Exception as e:
        print(f"Error parsing XML {xml_path}: {e}")
        return []

def build_unified_ground_dataset():
    print("\n--- Building Unified Ground Dataset (IP102 + PlantDoc Real Bounding Boxes) ---")
    prep_plantdoc()
    
    train_img_out = GROUND_UNIFIED_DIR / "images" / "train"
    val_img_out = GROUND_UNIFIED_DIR / "images" / "val"
    train_lbl_out = GROUND_UNIFIED_DIR / "labels" / "train"
    val_lbl_out = GROUND_UNIFIED_DIR / "labels" / "val"
    
    for d in [train_img_out, val_img_out, train_lbl_out, val_lbl_out]:
        os.makedirs(get_win_path(d), exist_ok=True)
        
    ip102_src = DATASETS_DIR / "archive (3)" / "IP102_YOLOv5"
    ip102_train_imgs = list((ip102_src / "images" / "train").glob("*.jpg"))
    ip102_val_imgs = list((ip102_src / "images" / "val").glob("*.jpg"))
    
    random.seed(42)
    random.shuffle(ip102_train_imgs)
    random.shuffle(ip102_val_imgs)
    
    ip102_train_sub = ip102_train_imgs[:3500]
    ip102_val_sub = ip102_val_imgs[:600]
    
    print(f"Copying IP102 samples: {len(ip102_train_sub)} train, {len(ip102_val_sub)} val...")
    for img_p in ip102_train_sub:
        stem = img_p.stem
        lbl_p = ip102_src / "labels" / "train" / f"{stem}.txt"
        shutil.copyfile(get_win_path(img_p), get_win_path(train_img_out / img_p.name))
        if lbl_p.exists():
            shutil.copyfile(get_win_path(lbl_p), get_win_path(train_lbl_out / lbl_p.name))
            
    for img_p in ip102_val_sub:
        stem = img_p.stem
        lbl_p = ip102_src / "labels" / "val" / f"{stem}.txt"
        shutil.copyfile(get_win_path(img_p), get_win_path(val_img_out / img_p.name))
        if lbl_p.exists():
            shutil.copyfile(get_win_path(lbl_p), get_win_path(val_lbl_out / lbl_p.name))
            
    ip102_yaml = ip102_src / "ip102.yaml"
    class_names = {}
    with open(get_win_path(ip102_yaml), "r", encoding="utf-8") as f:
        data_cfg = yaml.safe_load(f)
        names_raw = data_cfg.get("names", [])
        if isinstance(names_raw, list):
            for idx, cname in enumerate(names_raw):
                class_names[idx] = cname
        elif isinstance(names_raw, dict):
            for idx, cname in names_raw.items():
                class_names[int(idx)] = cname

    plantdoc_class_map = {}
    plantdoc_train_dir = PLANTDOC_DIR / "TRAIN"
    plantdoc_test_dir = PLANTDOC_DIR / "TEST"
    
    plantdoc_samples = []
    xml_files = []
    if plantdoc_train_dir.exists():
        xml_files.extend(list(plantdoc_train_dir.glob("*.xml")))
    if plantdoc_test_dir.exists():
        xml_files.extend(list(plantdoc_test_dir.glob("*.xml")))
        
    for xml_p in xml_files:
        img_p = xml_p.with_suffix(".jpg")
        if not img_p.exists():
            img_p = xml_p.with_suffix(".png")
        if not img_p.exists():
            continue
        plantdoc_samples.append((img_p, xml_p))
        
    print(f"Processing PlantDoc real bounding box samples: {len(plantdoc_samples)} images...")
    random.shuffle(plantdoc_samples)
    pd_val_count = int(len(plantdoc_samples) * 0.20)
    pd_val_set = plantdoc_samples[:pd_val_count]
    pd_train_set = plantdoc_samples[pd_val_count:]
    
    ip102_class_offset = len(class_names)  # 102
    
    def process_pd_split(samples, img_out, lbl_out):
        for idx, (img_p, xml_p) in enumerate(samples):
            boxes = parse_voc_xml(xml_p, plantdoc_class_map)
            if not boxes:
                continue
            
            offset_boxes = []
            for b in boxes:
                parts = b.split()
                cid = int(parts[0]) + ip102_class_offset
                offset_boxes.append(f"{cid} {' '.join(parts[1:])}")
                
            dst_img = img_out / f"pd_{idx}_{img_p.name[-30:]}"
            dst_lbl = lbl_out / f"pd_{idx}_{img_p.stem[-30:]}.txt"
            shutil.copyfile(get_win_path(img_p), get_win_path(dst_img))
            with open(get_win_path(dst_lbl), "w", encoding="utf-8") as f:
                f.write("\n".join(offset_boxes) + "\n")

    process_pd_split(pd_train_set, train_img_out, train_lbl_out)
    process_pd_split(pd_val_set, val_img_out, val_lbl_out)
    
    for pd_name, pd_id in plantdoc_class_map.items():
        full_cid = pd_id + ip102_class_offset
        class_names[full_cid] = f"PlantDoc_{pd_name}"
        
    yaml_path = GROUND_UNIFIED_DIR / "ground_unified.yaml"
    with open(get_win_path(yaml_path), "w", encoding="utf-8") as f:
        f.write(f"path: {GROUND_UNIFIED_DIR.resolve()}\n")
        f.write("train: images/train\n")
        f.write("val: images/val\n\n")
        f.write("names:\n")
        for cid in sorted(class_names.keys()):
            f.write(f"  {cid}: '{class_names[cid]}'\n")
            
    print(f"Unified Ground Dataset created successfully with {len(class_names)} total classes at: {yaml_path}")

if __name__ == "__main__":
    build_unified_ground_dataset()
