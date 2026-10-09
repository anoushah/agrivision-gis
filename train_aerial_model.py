"""
Train the aerial crop-stress detection model on Agriculture-Vision data.

This is a separate, lightweight model (not the same classes as ground model),
since drone imagery shows large-area patterns, not individual pests/leaves.

Run on your machine:
    python train_aerial_model.py
"""

from ultralytics import YOLO
from pathlib import Path
import yaml
import sys

DATA_YAML = "datasets/agri_vision_yolo/agri_vision.yaml"   # adjust if your yaml has a different name
STARTING_WEIGHTS = "yolov8n.pt"     # start from a small general pretrained YOLO (not best.pt, since classes are totally different)
OUTPUT_NAME = "aerial_best"

EPOCHS = 25
PATIENCE = 7
IMG_SIZE = 416
BATCH_SIZE = 16
WORKERS = 4


def main():
    yaml_path = Path(DATA_YAML)
    if not yaml_path.exists():
        print(f"!! Could not find {DATA_YAML}")
        print("   Run inspect_datasets.py first and fix the DATA_YAML path above to match your actual file.")
        sys.exit(1)

    with open(yaml_path) as f:
        data_cfg = yaml.safe_load(f)
    n_classes = len(data_cfg.get("names", []))
    print(f"Training aerial model on {n_classes} classes, config: {yaml_path}")
    print(f"Starting from weights: {STARTING_WEIGHTS} (general pretrained, not ground best.pt - different task)")
    print(f"Epochs: {EPOCHS}, patience: {PATIENCE}, imgsz: {IMG_SIZE}, batch: {BATCH_SIZE}")
    print("This will take a while on CPU - progress will print after each epoch.\n")

    model = YOLO(STARTING_WEIGHTS)   # ultralytics auto-downloads yolov8n.pt if not present locally

    results = model.train(
        data=str(yaml_path),
        epochs=EPOCHS,
        patience=PATIENCE,
        imgsz=IMG_SIZE,
        batch=BATCH_SIZE,
        workers=WORKERS,
        device="cpu",
        project="runs/train",
        name=OUTPUT_NAME,
        exist_ok=True,
        verbose=True,
    )

    print("\n================ TRAINING COMPLETE ================")
    print(f"Best weights saved at: runs/train/{OUTPUT_NAME}/weights/best.pt")
    print("Copy that file to your project root as aerial_best.pt to use it:")
    print(f'  copy "runs\\train\\{OUTPUT_NAME}\\weights\\best.pt" "aerial_best.pt"   (Windows)')
    print("\nCheck runs/train/%s/results.csv for exact mAP50 and mAP50-95 per epoch." % OUTPUT_NAME)


if __name__ == "__main__":
    main()
