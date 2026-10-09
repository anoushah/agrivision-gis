"""
Better test video generator — preserves image aspect ratio (no stretching/
distortion) and outputs at a clean, larger resolution for presentable demos.
"""

import cv2
import glob
import numpy as np

TARGET_W, TARGET_H = 960, 720   # output video resolution (bigger = clearer)
SECONDS_PER_IMAGE = 2            # how long each image shows (higher = easier to see in demo)
FPS = 1

images = (glob.glob("test_images/*.jpg") +
          glob.glob("test_images/*.jpeg") +
          glob.glob("test_images/*.png") +
          glob.glob("test_images/*.JPG") +
          glob.glob("test_images/*.PNG"))

print(f"Found {len(images)} images")

if len(images) == 0:
    print("ERROR: no images found in test_images/ folder.")
else:
    out = cv2.VideoWriter(
        "field_footage.mp4",
        cv2.VideoWriter_fourcc(*"mp4v"),
        FPS,
        (TARGET_W, TARGET_H),
    )

    for img_path in images:
        img = cv2.imread(img_path)
        if img is None:
            print(f"Skipping unreadable file: {img_path}")
            continue

        h, w = img.shape[:2]
        scale = min(TARGET_W / w, TARGET_H / h)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(img, (new_w, new_h))

        # letterbox: paste onto a black canvas, centered, so no distortion
        canvas = np.zeros((TARGET_H, TARGET_W, 3), dtype=np.uint8)
        x_off = (TARGET_W - new_w) // 2
        y_off = (TARGET_H - new_h) // 2
        canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized

        # repeat the frame so each image is visible for SECONDS_PER_IMAGE seconds
        for _ in range(FPS * SECONDS_PER_IMAGE):
            out.write(canvas)

    out.release()
    print(f"field_footage.mp4 created: {TARGET_W}x{TARGET_H}, "
          f"{len(images)} images x {SECONDS_PER_IMAGE}s each")
