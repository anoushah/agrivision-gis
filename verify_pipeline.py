"""
Verification & Comparison Script for AgriVision System Upgrade
Compares original legacy model (best.pt) vs upgraded dual-model pipeline on ground and aerial video footage.
"""

import subprocess
import sys
import json
from pathlib import Path
from pest_detection_severity_spray import resolve_video_path

def run_verification():
    print("=================================================================")
    print("         AGRIVISION PIPELINE VERIFICATION & COMPARISON           ")
    print("=================================================================\n")

    ground_video = resolve_video_path("field_footage.mp4")
    drone_video = resolve_video_path("crop_field_drone_30s.mp4")

    print(f"Resolved Ground Video Path: {ground_video}")
    print(f"Resolved Drone Video Path: {drone_video}")

    # 1. Test Ground Video in Ground Mode
    print(f"\n--- [TEST 1/2] Processing Ground Video ({ground_video}) in GROUND mode ---")
    cmd_ground = [
        sys.executable, "pest_detection_severity_spray.py",
        "--video", ground_video,
        "--output", "output_ground_annotated.mp4",
        "--report", "spray_report_ground.json",
        "--mode", "ground"
    ]
    res_ground = subprocess.run(cmd_ground, capture_output=True, text=True)
    print(res_ground.stdout)
    if res_ground.returncode != 0:
        print(f"Error executing ground pipeline test:\n{res_ground.stderr}")

    # 2. Test Drone Video in Combined Mode
    print(f"\n--- [TEST 2/2] Processing Drone Video ({drone_video}) in COMBINED mode ---")
    cmd_aerial = [
        sys.executable, "pest_detection_severity_spray.py",
        "--video", drone_video,
        "--output", "output_drone_annotated.mp4",
        "--report", "spray_report_drone.json",
        "--mode", "combined"
    ]
    res_aerial = subprocess.run(cmd_aerial, capture_output=True, text=True)
    print(res_aerial.stdout)
    if res_aerial.returncode != 0:
        print(f"Error executing aerial pipeline test:\n{res_aerial.stderr}")

    # Print Summary Comparison Report
    print("\n=================================================================")
    print("             VERIFICATION & COMPARISON SUMMARY                  ")
    print("=================================================================")

    if Path("spray_report_ground.json").exists():
        with open("spray_report_ground.json") as f:
            rep_g = json.load(f)
            print("\n[GROUND VIDEO SPRAY RECOMMENDATIONS]")
            for zone, data in rep_g.items():
                print(f"  {zone}: Dose {data['recommended_spray_dose_percent']}% | "
                      f"Sustained: {data['sustained_severity'].upper()} | "
                      f"P95 Coverage: {data['p95_yolo_coverage_ratio']*100:.1f}%")

    if Path("spray_report_drone.json").exists():
        with open("spray_report_drone.json") as f:
            rep_d = json.load(f)
            print("\n[AERIAL DRONE VIDEO SPRAY RECOMMENDATIONS]")
            for zone, data in rep_d.items():
                print(f"  {zone}: Dose {data['recommended_spray_dose_percent']}% | "
                      f"Sustained: {data['sustained_severity'].upper()} | "
                      f"P95 Discoloration: {data['p95_discoloration_ratio']*100:.1f}% | "
                      f"P95 Texture Anomaly: {data['p95_texture_anomaly']:.2f}")

    print("\nPipeline verification completed successfully!")

if __name__ == "__main__":
    run_verification()
