
from pathlib import Path
from ultralytics import YOLO

# Project folder
BASE_DIR = Path(__file__).resolve().parent

# Load trained pest detection model
model = YOLO(str(BASE_DIR / "best.pt"))

# Test image
image_path = BASE_DIR / "pest_test" / "test image.jpeg"

if not image_path.exists():
    raise FileNotFoundError(
        f"Test image not found: {image_path}"
    )

# Detect pests without retraining
results = model.predict(
    source=str(image_path),
    imgsz=640,
    conf=0.25,
    save=True
)

# Print pest names and confidence scores
for result in results:
    if result.boxes is None or len(result.boxes) == 0:
        print("No pests detected.")
        continue

    for box in result.boxes:
        class_id = int(box.cls[0])
        confidence = float(box.conf[0])
        pest_name = result.names[class_id]

        print(f"Pest: {pest_name}")
        print(f"Confidence: {confidence:.2%}")

print("Detection complete. Check the runs/detect folder.")
