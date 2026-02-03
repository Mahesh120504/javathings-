import cv2
import numpy as np
import time
import os
import json
from ultralytics import YOLO
from collections import deque
from dotenv import load_dotenv
from db_async import db

load_dotenv()

def iou(boxA, boxB):
    xA = max(boxA[0], boxB[0])
    yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2])
    yB = min(boxA[3], boxB[3])

    inter = max(0, xB - xA) * max(0, yB - yA)
    if inter == 0:
        return 0.0

    areaA = (boxA[2]-boxA[0])*(boxA[3]-boxA[1])
    areaB = (boxB[2]-boxB[0])*(boxB[3]-boxB[1])
    return inter / float(areaA + areaB - inter)


# ================= CONFIG & SHARED STATE =================
VIOLATION_DIR = "violations"
os.makedirs(VIOLATION_DIR, exist_ok=True)

camera_states = {}
frame_buffer = {}
active_cameras_info = {}

PPE_CLASSES = {
    0: "Hardhat", 1: "Mask", 2: "NO-Hardhat", 3: "NO-Mask",
    4: "NO-Safety Vest", 5: "Person", 6: "Safety Cone",
    7: "Safety Vest", 8: "Machinery", 9: "Vehicle"
}
VIOLATION_LABELS = ["NO-Hardhat", "NO-Mask", "NO-Safety Vest"]

# Load Model
model_path = os.getenv("YOLO_MODEL_PATH", "webest.pt")
print(f"[*] Loading YOLO Model from {model_path}...")

# GLOBAL MODEL INSTANCE
model = YOLO(model_path)
conf_threshold = float(os.getenv("CONFIDENCE_THRESHOLD", 0.35))

class CameraMemory:
    def __init__(self):
        self.frame_cache = deque(maxlen=60)
        self.saved_person_ids = set()

        self.person_registry = {}
        self.next_person_id = 1
        self.last_seen = {}

        # Removed per-instance model loading


# ================= DETECTION LOGIC =================
async def process_frame(frame, cam_id):
    if cam_id not in camera_states:
        camera_states[cam_id] = CameraMemory()
    mem = camera_states[cam_id]

    # Run Tracking using global model
    results = model.track(frame, persist=True, verbose=False, conf=conf_threshold, tracker="botsort.yaml")

    current_frame_data = {
        'persons': [],
        'ppe': [],
        'flags': {"hardhat": 0, "mask": 0, "vest": 0, "vehicle": 0, "machinery": 0}
    }

    if results[0].boxes.id is not None:
        boxes = results[0].boxes.xyxy.cpu().numpy().astype(int)
        ids = results[0].boxes.id.cpu().numpy().astype(int)
        clss = results[0].boxes.cls.cpu().numpy().astype(int)

        for box, p_id, cls in zip(boxes, ids, clss):
            label = PPE_CLASSES.get(cls)
            if label == "Person":
                area = (box[2] - box[0]) * (box[3] - box[1])
                # Ensure ID is sent as standard int for DB
                current_frame_data['persons'].append({'box': box, 'id': int(p_id), 'area': area})
            else:
                current_frame_data['ppe'].append((cls, box))
                if label == "Vehicle": current_frame_data['flags']["vehicle"] = 1
                if label == "Machinery": current_frame_data['flags']["machinery"] = 1

        for p in current_frame_data['persons']:
            matched_id = None
            for pid, prev_box in mem.person_registry.items():
                if iou(p['box'], prev_box) > 0.4:
                    matched_id = pid
                    break

            if matched_id is None:
                matched_id = mem.next_person_id
                mem.next_person_id += 1

            mem.person_registry[matched_id] = p['box']
            mem.last_seen[matched_id] = time.time()
            p['person_id'] = matched_id

        # ================= PERSISTENT PERSON ID ASSIGNMENT =================
        # (Redundant block removed in cleanup)


    mem.frame_cache.append({'image': frame.copy(), 'data': current_frame_data})

    violators_in_current_frame = {}

    for p_info in current_frame_data['persons']:
        p_id = p_info.get('person_id', p_info['id'])
        p_box = p_info['box']
        px1, py1, px2, py2 = p_box

        person_violations = []

        for ppe_id, ppe_box in current_frame_data['ppe']:
            ox1, oy1, ox2, oy2 = ppe_box
            cx, cy = (ox1 + ox2) / 2, (oy1 + oy2) / 2

            if px1 <= cx <= px2 and py1 <= cy <= py2:
                label = PPE_CLASSES.get(ppe_id)
                if label in VIOLATION_LABELS:
                    person_violations.append(label)

        if person_violations:
            color = (0, 0, 255)
            text_label = f"ID: {p_id} | {', '.join(person_violations)}"
            violators_in_current_frame[p_id] = person_violations
        else:
            color = (255, 0, 0)
            text_label = f"ID: {p_id}"

        cv2.rectangle(frame, (px1, py1), (px2, py2), color, 2)
        cv2.putText(frame, text_label, (px1, py1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    # 🔹 Cleanup stale persons (not seen for 10s)
    now = time.time()
    mem.person_registry = {
        pid: box for pid, box in mem.person_registry.items()
        if now - mem.last_seen.get(pid, now) < 10
    }


    # 5. Save Logic
    for p_id, violation_list in violators_in_current_frame.items():
        if p_id not in mem.saved_person_ids:
            best_frame = frame
            best_data = current_frame_data
            best_flags = best_data['flags']

            date_str = time.strftime("%d%m%Y")
            vid_ts = time.strftime("%I%M%S%p")

            # --- CORRECT FILE NAMING (Matches Row 12 format) ---
            # e.g., 000001 (flags + count)
            c = f"{best_flags.get('hardhat',0)}{best_flags.get('mask',0)}{best_flags.get('vest',0)}{best_flags.get('vehicle',0)}{best_flags.get('machinery',0)}"
            fname = f"{date_str}_{vid_ts}_{c}{len(best_data['persons'])}__ID{p_id}_{cam_id}_BEST.jpg"
            path = os.path.join(VIOLATION_DIR, fname)

            cv2.imwrite(path, best_frame)
            mem.saved_person_ids.add(p_id)
            print(f"[{cam_id}] Saved annotated proof: {fname}")

            # --- CORRECT DB INSERTION (No Nulls) ---
            v_types_str = ", ".join(violation_list)
            # Serialize flags to JSON string for the DB
            json_flags = json.dumps(best_flags)

            await db.log_violation(cam_id, p_id, v_types_str, path, json_flags)

    frame_buffer[cam_id] = frame
