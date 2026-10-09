"""
Agriculture-Vision 2021 Mask to YOLO Bounding Box Converter (Optimized for Fast CPU Execution)
Converts 9 anomaly mask categories into YOLO dataset format for aerial crop stress model training.
"""

import os
import cv2
import glob
import numpy as np
from pathlib import Path
import random

AGRI_BASE = Path("datasets/archive (5)")
OUTPUT_DIR = Path("datasets/agri_vision_yolo")

CATEGORIES = [
    "weed_cluster",        # 0
    "nutrient_deficiency", # 1
    "drydown",             # 2
    "water",               # 3
    "storm_damage",        # 4
    "planter_skip",        # 5
    "double_plant",        # 6
    "endrow",              # 7
    "waterway"             # 8
]

CAT_TO_ID = {cat: idx for idx, cat in enumerate(CATEGORIES)}

def convert_masks_to_yolo(max_images=2000):
    rgb_dir = AGRI_BASE / "images" / "rgb"
    lbl_dir = AGRI_BASE / "labels"

    if not rgb_dir.exists():
        print(f"Error: {rgb_dir} does not exist.")
        return

    images_output_train = OUTPUT_DIR / "images" / "train"
    images_output_val = OUTPUT_DIR / "images" / "val"
    labels_output_train = OUTPUT_DIR / "labels" / "train"
    labels_output_val = OUTPUT_DIR / "labels" / "val"

    for d in [images_output_train, images_output_val, labels_output_train, labels_output_val]:
        d.mkdir(parents=True, exist_ok=True)

    rgb_files = list(rgb_dir.glob("*.jpg"))
    random.seed(42)
    random.shuffle(rgb_files)
    rgb_files = rgb_files[:max_images]
    print(f"Scanning sample of {len(rgb_files)} Agriculture-Vision RGB images...")

    valid_samples = []
    
    for rgb_path in rgb_files:
        stem = rgb_path.stem
        boxes = []
        img = cv2.imread(str(rgb_path))
        if img is None:
            continue
        h, w = img.shape[:2]

        for cat in CATEGORIES:
            cat_mask_path = lbl_dir / cat / f"{stem}.png"
            if not cat_mask_path.exists():
                continue

            mask = cv2.imread(str(cat_mask_path), cv2.IMREAD_GRAYSCALE)
            if mask is None or not np.any(mask > 0):
                continue

            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            class_id = CAT_TO_ID[cat]

            for cnt in contours:
                x, y, bw, bh = cv2.boundingRect(cnt)
                if bw < 15 or bh < 15:
                    continue

                cx = (x + bw / 2.0) / w
                cy = (y + bh / 2.0) / h
                nw = bw / float(w)
                nh = bh / float(h)
                boxes.append(f"{class_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}")

        if boxes:
            valid_samples.append((rgb_path, boxes))

    print(f"Found {len(valid_samples)} images with valid crop anomaly annotations.")

    random.shuffle(valid_samples)
    val_count = int(len(valid_samples) * 0.15)
    val_set = valid_samples[:val_count]
    train_set = valid_samples[val_count:]

    def save_split(samples, img_out, lbl_out):
        for rgb_path, boxes in samples:
            stem = rgb_path.stem
            dst_img = img_out / f"{stem}.jpg"
            if not dst_img.exists():
                cv2.imwrite(str(dst_img), cv2.imread(str(rgb_path)))
            
            lbl_file = lbl_out / f"{stem}.txt"
            with open(lbl_file, "w") as f:
                f.write("\n".join(boxes) + "\n")

    print(f"Saving {len(train_set)} train samples and {len(val_set)} val samples...")
    save_split(train_set, images_output_train, labels_output_train)
    save_split(val_set, images_output_val, labels_output_val)

    yaml_content = f"""path: {OUTPUT_DIR.resolve()}
train: images/train
val: images/val

names:
"""
    for idx, cat in enumerate(CATEGORIES):
        yaml_content += f"  {idx}: '{cat}'\n"

    with open(OUTPUT_DIR / "agri_vision.yaml", "w") as f:
        f.write(yaml_content)

    print(f"Agriculture-Vision YOLO dataset successfully created at: {OUTPUT_DIR}")

if __name__ == "__main__":
    convert_masks_to_yolo()
