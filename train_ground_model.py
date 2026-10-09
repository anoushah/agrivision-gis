"""
Fine-tune the ground-level pest/disease detection model.

Starts from your existing best.pt (transfer learning, not from scratch),
trains on the unified dataset (IP102 + PlantDoc + PlantVillage if present).

Run on your machine:
    python train_ground_model.py

This will take several hours on CPU. Let it run in the background,
check back periodically using the printed progress log.
"""

from ultralytics import YOLO
from pathlib import Path
import yaml
import sys

DATA_YAML = "datasets/unified_ground/ground_unified.yaml"   # adjust if your yaml has a different name
STARTING_WEIGHTS = "best.pt"                                 # transfer learning from your existing model
OUTPUT_NAME = "ground_best"                                  # final weights saved as runs/train/ground_best/weights/best.pt

EPOCHS = 25
PATIENCE = 7          # early stopping: stop if val mAP doesn't improve for 7 epochs
IMG_SIZE = 416
BATCH_SIZE = 16        # lower than 32 to be safer on CPU RAM; increase if you have 16GB+ free RAM
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
    print(f"Training ground model on {n_classes} classes, config: {yaml_path}")
    print(f"Starting from weights: {STARTING_WEIGHTS}")
    print(f"Epochs: {EPOCHS}, patience: {PATIENCE}, imgsz: {IMG_SIZE}, batch: {BATCH_SIZE}")
    print("This will take a while on CPU - progress will print after each epoch.\n")

    model = YOLO(STARTING_WEIGHTS)

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
    print("Copy that file to your project root as ground_best.pt to use it:")
    print(f'  copy "runs\\train\\{OUTPUT_NAME}\\weights\\best.pt" "ground_best.pt"   (Windows)')
    print("\nCheck runs/train/%s/results.csv for exact mAP50 and mAP50-95 per epoch." % OUTPUT_NAME)


if __name__ == "__main__":
    main()
