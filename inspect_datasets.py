"""
Quick sanity check: prints image counts, label counts, and class names
found in the prepared dataset folders, so we know what's ACTUALLY there
before spending hours training on it.

Run this on your machine:
    python inspect_datasets.py
"""

from pathlib import Path
import yaml

DATASETS_ROOT = Path("datasets")

def count_images(folder):
    if not folder.exists():
        return 0
    exts = {".jpg", ".jpeg", ".png", ".bmp"}
    return sum(1 for f in folder.rglob("*") if f.suffix.lower() in exts)

def count_labels(folder):
    if not folder.exists():
        return 0
    return sum(1 for f in folder.rglob("*.txt"))

def inspect_yolo_dataset(name, root_folder):
    print(f"\n{'='*60}")
    print(f"Dataset: {name}  ->  {root_folder}")
    print(f"{'='*60}")

    if not root_folder.exists():
        print("  !! FOLDER DOES NOT EXIST")
        return

    # find yaml file
    yaml_files = list(root_folder.glob("*.yaml")) + list(root_folder.glob("*.yml"))
    if yaml_files:
        yf = yaml_files[0]
        print(f"  Found config: {yf.name}")
        with open(yf, "r") as f:
            data = yaml.safe_load(f)
        names = data.get("names", None)
        if isinstance(names, dict):
            print(f"  Number of classes in yaml: {len(names)}")
        elif isinstance(names, list):
            print(f"  Number of classes in yaml: {len(names)}")
        print(f"  train path in yaml: {data.get('train')}")
        print(f"  val path in yaml:   {data.get('val')}")
    else:
        print("  !! NO .yaml FOUND - training script will need one")

    for split in ["train", "val", "valid", "test"]:
        img_dir = root_folder / split / "images"
        lbl_dir = root_folder / split / "labels"
        if img_dir.exists():
            n_img = count_images(img_dir)
            n_lbl = count_labels(lbl_dir)
            print(f"  {split}: {n_img} images, {n_lbl} label files")

    # Try to spot which source datasets contributed, based on filename patterns
    all_images = list(root_folder.rglob("*.jpg")) + list(root_folder.rglob("*.jpeg")) + list(root_folder.rglob("*.png"))
    sample_names = [f.stem for f in all_images[:2000]]
    plantvillage_markers = sum(1 for n in sample_names if any(k in n for k in ["___", "healthy", "leaf"]))
    print(f"  Sample check (first 2000 filenames): {plantvillage_markers} look PlantVillage-style")


if __name__ == "__main__":
    inspect_yolo_dataset("Unified Ground (IP102+PlantDoc+PlantVillage?)", DATASETS_ROOT / "unified_ground")
    inspect_yolo_dataset("Aerial (Agriculture-Vision)", DATASETS_ROOT / "agri_vision_yolo")

    print(f"\n{'='*60}")
    print("Raw source folders (for reference):")
    print(f"{'='*60}")
    for name in ["IP102", "PlantDoc", "PlantVillage", "Agriculture-Vision"]:
        folder = DATASETS_ROOT / name
        n = count_images(folder)
        print(f"  {name}: {n} images found in raw folder")
