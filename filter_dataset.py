import os
import shutil
from collections import Counter

SRC = r"datasets\unified_ground"
DST = r"datasets\unified_ground_v2"
THRESHOLD = 10

names = {
  0: 'rice leaf roller', 1: 'rice leaf caterpillar', 2: 'paddy stem maggot',
  3: 'asiatic rice borer', 4: 'yellow rice borer', 5: 'rice gall midge',
  6: 'Rice Stemfly', 7: 'brown plant hopper', 8: 'white backed plant hopper',
  9: 'small brown plant hopper', 10: 'rice water weevil', 11: 'rice leafhopper',
  12: 'grain spreader thrips', 13: 'rice shell pest', 14: 'grub',
  15: 'mole cricket', 16: 'wireworm', 17: 'white margined moth',
  18: 'black cutworm', 19: 'large cutworm', 20: 'yellow cutworm',
  21: 'red spider', 22: 'corn borer', 23: 'army worm', 24: 'aphids',
  25: 'Potosiabre vitarsis', 26: 'peach borer', 27: 'english grain aphid',
  28: 'green bug', 29: 'bird cherry-oataphid', 30: 'wheat blossom midge',
  31: 'penthaleus major', 32: 'longlegged spider mite', 33: 'wheat phloeothrips',
  34: 'wheat sawfly', 35: 'cerodonta denticornis', 36: 'beet fly',
  37: 'flea beetle', 38: 'cabbage army worm', 39: 'beet army worm',
  40: 'Beet spot flies', 41: 'meadow moth', 42: 'beet weevil',
  43: 'sericaorient alismots chulsky', 44: 'alfalfa weevil', 45: 'flax budworm',
  46: 'alfalfa plant bug', 47: 'tarnished plant bug', 48: 'Locustoidea',
  49: 'lytta polita', 50: 'legume blister beetle', 51: 'blister beetle',
  52: 'therioaphis maculata Buckton', 53: 'odontothrips loti', 54: 'Thrips',
  55: 'alfalfa seed chalcid', 56: 'Pieris canidia', 57: 'Apolygus lucorum',
  58: 'Limacodidae', 59: 'Viteus vitifoliae', 60: 'Colomerus vitis',
  61: 'Brevipoalpus lewisi McGregor', 62: 'oides decempunctata',
  63: 'Polyphagotars onemus latus', 64: 'Pseudococcus comstocki Kuwana',
  65: 'parathrene regalis', 66: 'Ampelophaga', 67: 'Lycorma delicatula',
  68: 'Xylotrechus', 69: 'Cicadella viridis', 70: 'Miridae',
  71: 'Trialeurodes vaporariorum', 72: 'Erythroneura apicalis',
  73: 'Papilio xuthus', 74: 'Panonchus citri McGregor',
  75: 'Phyllocoptes oleiverus ashmead', 76: 'Icerya purchasi Maskell',
  77: 'Unaspis yanonensis', 78: 'Ceroplastes rubens', 79: 'Chrysomphalus aonidum',
  80: 'Parlatoria zizyphus Lucus', 81: 'Nipaecoccus vastalor',
  82: 'Aleurocanthus spiniferus', 83: 'Tetradacus c Bactrocera minax ',
  84: 'Dacus dorsalis(Hendel)', 85: 'Bactrocera tsuneonis', 86: 'Prodenia litura',
  87: 'Adristyrannus', 88: 'Phyllocnistis citrella Stainton',
  89: 'Toxoptera citricidus', 90: 'Toxoptera aurantii',
  91: 'Aphis citricola Vander Goot', 92: 'Scirtothrips dorsalis Hood',
  93: 'Dasineura sp', 94: 'Lawana imitata Melichar', 95: 'Salurnis marginella Guerr',
  96: 'Deporaus marginatus Pascoe', 97: 'Chlumetia transversa',
  98: 'Mango flat beak leafhopper', 99: 'Rhytidodera bowrinii white',
  100: 'Sternochetus frigidus', 101: 'Cicadellidae',
  102: 'PlantDoc_Tomato mold leaf', 103: 'PlantDoc_Tomato Early blight leaf',
  104: 'PlantDoc_Apple rust leaf', 105: 'PlantDoc_Tomato leaf yellow virus',
  106: 'PlantDoc_Potato leaf early blight', 107: 'PlantDoc_Raspberry leaf',
  108: 'PlantDoc_Squash Powdery mildew leaf', 109: 'PlantDoc_grape leaf black rot',
  110: 'PlantDoc_Tomato leaf mosaic virus', 111: 'PlantDoc_Corn Gray leaf spot',
  112: 'PlantDoc_Blueberry leaf', 113: 'PlantDoc_Strawberry leaf',
  114: 'PlantDoc_Peach leaf', 115: 'PlantDoc_Tomato leaf bacterial spot',
  116: 'PlantDoc_Soyabean leaf', 117: 'PlantDoc_Tomato leaf',
  118: 'PlantDoc_Tomato Septoria leaf spot', 119: 'PlantDoc_Potato leaf late blight',
  120: 'PlantDoc_Corn leaf blight', 121: 'PlantDoc_Apple Scab Leaf',
  122: 'PlantDoc_grape leaf', 123: 'PlantDoc_Corn rust leaf',
  124: 'PlantDoc_Bell_pepper leaf', 125: 'PlantDoc_Tomato leaf late blight',
  126: 'PlantDoc_Bell_pepper leaf spot', 127: 'PlantDoc_Apple leaf',
  128: 'PlantDoc_Cherry leaf', 129: 'PlantDoc_Potato leaf',
  130: 'PlantDoc_Tomato two spotted spider mites leaf'
}

counts = Counter()
for split in ["train", "val"]:
    labels_dir = os.path.join(SRC, "labels", split)
    for fname in os.listdir(labels_dir):
        if fname.endswith(".txt"):
            with open(os.path.join(labels_dir, fname)) as f:
                for line in f:
                    if line.strip():
                        counts[int(line.split()[0])] += 1

keep_classes = sorted([c for c in names if counts.get(c, 0) >= THRESHOLD])
old_to_new = {old: new for new, old in enumerate(keep_classes)}
print(f"Keeping {len(keep_classes)} of {len(names)} classes")

for split in ["train", "val"]:
    img_src = os.path.join(SRC, "images", split)
    lbl_src = os.path.join(SRC, "labels", split)
    img_dst = os.path.join(DST, "images", split)
    lbl_dst = os.path.join(DST, "labels", split)
    os.makedirs(img_dst, exist_ok=True)
    os.makedirs(lbl_dst, exist_ok=True)

    for fname in os.listdir(lbl_src):
        if not fname.endswith(".txt"):
            continue
        new_lines = []
        with open(os.path.join(lbl_src, fname)) as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                cls_id = int(parts[0])
                if cls_id in old_to_new:
                    parts[0] = str(old_to_new[cls_id])
                    new_lines.append(" ".join(parts))
        with open(os.path.join(lbl_dst, fname), "w") as f:
            f.write("\n".join(new_lines))
        base = os.path.splitext(fname)[0]
        for ext in [".jpg", ".jpeg", ".png"]:
            img_path = os.path.join(img_src, base + ext)
            if os.path.exists(img_path):
                shutil.copy(img_path, os.path.join(img_dst, base + ext))
                break

yaml_path = os.path.join(DST, "ground_unified_v2.yaml")
with open(yaml_path, "w") as f:
    f.write(f"path: {os.path.abspath(DST)}\n")
    f.write("train: images/train\n")
    f.write("val: images/val\n\n")
    f.write("names:\n")
    for new_id, old_id in enumerate(keep_classes):
        f.write(f"  {new_id}: '{names[old_id]}'\n")

print("Done! New dataset at:", DST)