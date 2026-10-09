import os
from collections import Counter

labels_dir = r"datasets\unified_ground\labels\train"
counts = Counter()

for fname in os.listdir(labels_dir):
    if fname.endswith(".txt"):
        with open(os.path.join(labels_dir, fname)) as f:
            for line in f:
                if line.strip():
                    cls_id = int(line.split()[0])
                    counts[cls_id] += 1

for cls_id, count in sorted(counts.items(), key=lambda x: x[1]):
    print(f"{cls_id}: {count}")

print(f"\nTotal classes with data: {len(counts)}")
print(f"Classes with <10 instances: {sum(1 for c in counts.values() if c < 10)}")
print(f"Classes with <5 instances: {sum(1 for c in counts.values() if c < 5)}")