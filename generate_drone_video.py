"""
Ultra-Clear 1080p Full HD Drone Video Generator for Agricultural Crop Fields
-----------------------------------------------------------------------------
Features:
- Output Resolution: 1920x1080 (Full HD 1080p) @ 30 FPS
- High-contrast foliage detail with unsharp masking for crystal clear leaf and row definition
- Highly visible, vivid pest infestation patches (bright yellow/orange chlorosis + dark necrosis spots)
- Rock-solid 3-axis motorized gimbal stabilization (zero roll wobble/jitter)
- Sub-pixel floating-point camera tracking across a 5760x3240 ultra-HD canvas
"""

import cv2
import numpy as np
import math
import os

# Paths
IMG1_PATH = r"C:\Users\HP\.gemini\antigravity\brain\eb04b16c-180c-48c6-8555-173649b7d7a7\crop_field_ultra_clear_1788344366231.png"
IMG2_PATH = r"C:\Users\HP\.gemini\antigravity\brain\eb04b16c-180c-48c6-8555-173649b7d7a7\crop_field_ultra_clear_2_1788344380965.png"

OUTPUT_VIDEO_30S = "crop_field_drone_30s.mp4"
OUTPUT_VIDEO_FIELD = "field_footage.mp4"

# Video Configuration - FULL HD 1080p
WIDTH = 1920
HEIGHT = 1080
FPS = 30
DURATION = 30  # seconds
TOTAL_FRAMES = FPS * DURATION  # 900 frames

print(f"Initializing Ultra-Clear 1080p Render: {WIDTH}x{HEIGHT} @ {FPS}fps, {TOTAL_FRAMES} frames ({DURATION}s)")

# Load Ultra-Sharp Base Images
img1 = cv2.imread(IMG1_PATH)
img2 = cv2.imread(IMG2_PATH)

if img1 is None or img2 is None:
    raise ValueError("Error loading ultra-clear base images.")

# Construct Massive 5760x3240 Canvas for 1080p drone trajectory
CANVAS_W = 5760
CANVAS_H = 3240

print("Building 5760x3240 high-resolution agricultural landscape canvas...")

# Resize base tiles to 2000x2000
t1 = cv2.resize(img1, (2000, 2000), interpolation=cv2.INTER_CUBIC)
t2 = cv2.resize(img2, (2000, 2000), interpolation=cv2.INTER_CUBIC)

# Apply Unsharp Masking to boost foliage edge contrast and plant detail
def sharpen_image(img, strength=1.2):
    blurred = cv2.GaussianBlur(img, (0, 0), 3.0)
    sharpened = cv2.addWeighted(img, 1.0 + strength, blurred, -strength, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)

t1 = sharpen_image(t1, strength=0.8)
t2 = sharpen_image(t2, strength=0.8)

canvas = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.float32)
weight_map = np.zeros((CANVAS_H, CANVAS_W), dtype=np.float32)

# Tile grid: 3x2 (covering 5760x3240)
grid = [
    [t1, t2, t1],
    [t2, t1, t2]
]

# Cosine blending weight kernel
tw, th = 2000, 2000
x_w = np.sin(np.pi * np.linspace(0, 1, tw)).astype(np.float32)
y_w = np.sin(np.pi * np.linspace(0, 1, th)).astype(np.float32)
tile_weight = np.outer(y_w, x_w)

for row in range(2):
    for col in range(3):
        x0 = col * 1700
        y0 = row * 1400
        tile = grid[row][col].astype(np.float32)
        
        h_part = min(th, CANVAS_H - y0)
        w_part = min(tw, CANVAS_W - x0)
        
        canvas[y0:y0+h_part, x0:x0+w_part] += tile[:h_part, :w_part] * tile_weight[:h_part, :w_part, None]
        weight_map[y0:y0+h_part, x0:x0+w_part] += tile_weight[:h_part, :w_part]

weight_map = np.maximum(weight_map, 1e-5)
canvas = (canvas / weight_map[:, :, None]).astype(np.uint8)

# Embed High-Contrast, Crystal Clear Pest Infestation Patches in Central Region
print("Embedding high-contrast, crystal clear pest-infected patches in middle section...")
middle_mask = np.zeros((CANVAS_H, CANVAS_W), dtype=np.float32)

# Pest infestation centers in the direct drone flight path (X ~ 2880, Y ~ 1620)
pest_spots = [
    (2880, 1620, 420), # Primary center spot (x, y, radius)
    (3250, 1950, 460), # Secondary right spot
    (2450, 1350, 380), # Secondary left spot
    (3100, 1300, 320)  # Top spot
]

for (px, py, pr) in pest_spots:
    y_idx, x_idx = np.ogrid[:CANVAS_H, :CANVAS_W]
    dist_sq = (x_idx - px)**2 + (y_idx - py)**2
    mask_patch = np.clip(1.0 - (dist_sq / (pr**2)), 0.0, 1.0)
    mask_patch = 0.5 * (1.0 + np.cos(np.pi * (1.0 - mask_patch)))
    middle_mask = np.maximum(middle_mask, mask_patch)

middle_mask = cv2.GaussianBlur(middle_mask, (81, 81), 0)

# Generate Vivid, Unmistakable Pest Damage Color Transformation
pest_canvas = canvas.copy()
hsv = cv2.cvtColor(pest_canvas, cv2.COLOR_BGR2HSV).astype(np.float32)

# Convert green crop areas into bright yellow chlorosis + brown necrotic disease spots
green_mask = (hsv[:, :, 0] >= 25) & (hsv[:, :, 0] <= 85)

# Vivid Yellow/Gold Chlorosis (Hue ~ 16-22 in OpenCV 0-180 scale)
hsv[:, :, 0] = np.where(green_mask, np.random.choice([16.0, 18.0, 22.0], size=hsv.shape[:2]), hsv[:, :, 0])
hsv[:, :, 1] = np.where(green_mask, np.clip(hsv[:, :, 1] * 1.5, 0, 255), hsv[:, :, 1]) # Ultra bright saturation
hsv[:, :, 2] = np.where(green_mask, np.clip(hsv[:, :, 2] * 0.95, 0, 255), hsv[:, :, 2])

pest_transformed = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

# Add necrotic brown spots & defoliated soil texture contrast inside the pest mask
brown_spots = np.zeros_like(canvas, dtype=np.uint8)
brown_spots[:, :] = (30, 60, 120) # BGR dark brownish necrotic tone

for c in range(3):
    # Blend bright yellow chlorosis + brown spots onto middle canvas
    blended_pest = (pest_transformed[:, :, c] * 0.75 + brown_spots[:, :, c] * 0.25).astype(np.uint8)
    canvas[:, :, c] = (canvas[:, :, c] * (1.0 - middle_mask * 0.95) + blended_pest * (middle_mask * 0.95)).astype(np.uint8)

# Final Sharpening Pass on Full Canvas for Crisp Leaves and Edges
print("Applying final unsharp mask to landscape canvas...")
canvas = sharpen_image(canvas, strength=0.5)

print("Landscape canvas ready. Setting up 1080p Video Writer...")

# Soft Vignette Mask
vignette = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
cy, cx = HEIGHT / 2.0, WIDTH / 2.0
max_dist = math.sqrt(cx**2 + cy**2)

for r in range(HEIGHT):
    for c in range(WIDTH):
        d = math.sqrt((c - cx)**2 + (r - cy)**2)
        vignette[r, c] = 1.0 - 0.08 * (d / max_dist)**2

vignette_3ch = np.stack([vignette]*3, axis=-1)

fourcc = cv2.VideoWriter_fourcc(*"mp4v")
out_30s = cv2.VideoWriter(OUTPUT_VIDEO_30S, fourcc, FPS, (WIDTH, HEIGHT))
out_field = cv2.VideoWriter(OUTPUT_VIDEO_FIELD, fourcc, FPS, (WIDTH, HEIGHT))

# Flight Path Coordinates (Smooth traversal over healthy section -> central pest patches -> healthy section)
start_x, start_y = 1100.0, 800.0
end_x, end_y = 4660.0, 2440.0

print("Rendering 900 ultra-clear 1080p drone frames...")

for frame_idx in range(TOTAL_FRAMES):
    progress = frame_idx / float(TOTAL_FRAMES - 1)
    
    # Smooth flight easing
    smooth_p = 0.5 * (1.0 - math.cos(math.pi * progress))
    
    cam_x = start_x + (end_x - start_x) * smooth_p
    cam_y = start_y + (end_y - start_y) * smooth_p
    
    # Bounding Box
    fx1 = cam_x - WIDTH / 2.0
    fy1 = cam_y - HEIGHT / 2.0
    
    fx1 = max(0.0, min(CANVAS_W - WIDTH - 1.0, fx1))
    fy1 = max(0.0, min(CANVAS_H - HEIGHT - 1.0, fy1))
    
    center_pt = (fx1 + WIDTH / 2.0, fy1 + HEIGHT / 2.0)
    frame = cv2.getRectSubPix(canvas, (WIDTH, HEIGHT), center_pt)
    
    frame_final = np.clip(frame.astype(np.float32) * vignette_3ch, 0, 255).astype(np.uint8)
    
    out_30s.write(frame_final)
    out_field.write(frame_final)
    
    if (frame_idx + 1) % 150 == 0 or frame_idx == TOTAL_FRAMES - 1:
        print(f"Render Progress: {frame_idx + 1}/{TOTAL_FRAMES} frames ({((frame_idx + 1)/TOTAL_FRAMES)*100:.1f}%)")

out_30s.release()
out_field.release()

print("Ultra-clear 1080p drone video render completed!")
print(f"Saved: {OUTPUT_VIDEO_30S}")
print(f"Saved: {OUTPUT_VIDEO_FIELD}")
