import glob
import sys
import cv2
from ultralytics import YOLO
from pathlib import Path
from pest_detection_severity_spray import (
    analyze_zone_color,
    analyze_zone_texture,
    analyze_zone_wilting,
    CLASS_NAME_MAP,
    MODEL_PATH
)

def is_valid_close_up_box(box, img_area):
    """
    Adaptive filter for close-up test images:
    - Genuine close-up disease photos feature large infected areas (20% - 85% of image area).
    - Accepts large boxes if confidence is strong (conf >= 0.35).
    - Rejects only extreme full-image background hallucinations (>90% of entire image with low confidence).
    """
    x1, y1, x2, y2 = box.xyxy[0].tolist()
    box_area = (x2 - x1) * (y2 - y1)
    area_ratio = box_area / img_area
    conf = float(box.conf[0])

    if area_ratio > 0.90 and conf < 0.60:
        return False, f"Oversized full-background box ({area_ratio*100:.1f}% frame area, conf={conf:.2f})"
    return True, f"{area_ratio*100:.1f}% frame area, conf={conf:.2f}"

def test_image(img_path, model):
    if not Path(img_path).exists():
        print(f"Image '{img_path}' not found.")
        return

    img = cv2.imread(str(img_path))
    if img is None:
        print(f"Could not read image '{img_path}'.")
        return
    h, w = img.shape[:2]
    img_area = h * w

    results = model.predict(str(img_path), conf=0.25, save=True, verbose=False)[0]
    boxes = results.boxes

    color_res = analyze_zone_color(img)
    tex_res = analyze_zone_texture(img)
    wilt_res = analyze_zone_wilting(img)

    print(f"\n--- Testing Image: {img_path} ({w}x{h}) ---")
    print(f"  [AERIAL METRICS] Discoloration: {color_res['unhealthy_ratio']*100:.1f}% "
          f"(Chlorosis: {color_res['chlorosis_ratio']*100:.1f}%, Necrosis: {color_res['necrosis_ratio']*100:.1f}%) | "
          f"Texture Anomaly: {tex_res['texture_anomaly']:.2f} | Wilt Score: {wilt_res['wilt_score']:.2f}")

    if boxes is not None and len(boxes) > 0:
        valid_count = 0
        for box in boxes:
            class_id = int(box.cls[0])
            label = CLASS_NAME_MAP.get(class_id, f"class_{class_id}")
            valid, info = is_valid_close_up_box(box, img_area)
            if valid:
                valid_count += 1
                print(f"  [VALID YOLO] {label}: {info}")
            else:
                print(f"  [REJECTED YOLO] {label}: {info}")
        print(f"Total valid YOLO detections accepted: {valid_count}")
    else:
        print("  No YOLO detections above confidence threshold 0.25.")

def main():
    if not Path(MODEL_PATH).exists():
        raise FileNotFoundError(f"Model file '{MODEL_PATH}' not found.")

    model = YOLO(MODEL_PATH)
    model.model.names = CLASS_NAME_MAP

    if len(sys.argv) > 1:
        test_image(sys.argv[1], model)
    else:
        targets = []
        if Path("test_image.jpg").exists():
            targets.append("test_image.jpg")
        targets.extend(sorted(glob.glob("test_images/*.*")))
        
        if not targets:
            print("No test images found.")
        else:
            for t in targets:
                test_image(t, model)

if __name__ == "__main__":
    main()
