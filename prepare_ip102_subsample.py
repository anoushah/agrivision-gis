"""
IP102 Fast CPU Subsampler
Subsamples 2,000 balanced images from IP102 dataset for rapid CPU training.
"""

import os
import shutil
import random
from pathlib import Path

SRC_DIR = Path("datasets/archive (3)/IP102_YOLOv5")
OUT_DIR = Path("datasets/ip102_subsample")

def prepare_subsample(max_train=1700, max_val=300):
    train_img_dir = SRC_DIR / "images" / "train"
    val_img_dir = SRC_DIR / "images" / "val"
    train_lbl_dir = SRC_DIR / "labels" / "train"
    val_lbl_dir = SRC_DIR / "labels" / "val"

    out_train_img = OUT_DIR / "images" / "train"
    out_val_img = OUT_DIR / "images" / "val"
    out_train_lbl = OUT_DIR / "labels" / "train"
    out_val_lbl = OUT_DIR / "labels" / "val"

    for d in [out_train_img, out_val_img, out_train_lbl, out_val_lbl]:
        d.mkdir(parents=True, exist_ok=True)

    train_imgs = list(train_img_dir.glob("*.jpg"))
    val_imgs = list(val_img_dir.glob("*.jpg"))

    random.seed(42)
    random.shuffle(train_imgs)
    random.shuffle(val_imgs)

    train_sub = train_imgs[:max_train]
    val_sub = val_imgs[:max_val]

    print(f"Copying {len(train_sub)} train images and {len(val_sub)} val images for fast CPU fine-tuning...")

    for img_path in train_sub:
        stem = img_path.stem
        lbl_path = train_lbl_dir / f"{stem}.txt"
        shutil.copy(img_path, out_train_img / img_path.name)
        if lbl_path.exists():
            shutil.copy(lbl_path, out_train_lbl / lbl_path.name)

    for img_path in val_sub:
        stem = img_path.stem
        lbl_path = val_lbl_dir / f"{stem}.txt"
        shutil.copy(img_path, out_val_img / img_path.name)
        if lbl_path.exists():
            shutil.copy(lbl_path, out_val_lbl / lbl_path.name)

    # Copy yaml
    yaml_src = SRC_DIR / "ip102.yaml"
    with open(yaml_src) as f:
        content = f.read()

    new_yaml = f"path: {OUT_DIR.resolve()}\ntrain: images/train\nval: images/val\n"
    # Append class list
    if "names:" in content:
        names_part = content[content.index("names:"):]
        new_yaml += "\n" + names_part

    with open(OUT_DIR / "ip102.yaml", "w") as f:
        f.write(new_yaml)

    print(f"IP102 subsampled dataset created at: {OUT_DIR}")

if __name__ == "__main__":
    prepare_subsample()
