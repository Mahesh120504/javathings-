# ======================================================
# IMPORTS
# ======================================================
import os
import shutil
import time
import asyncio
import cv2
import numpy as np
import bcrypt
import uvicorn
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text
from dotenv import load_dotenv

# Local Imports
import detect
import config_sc2
from helper import router as helper_router
from db_async import db as db_async
from db_sync import get_db

# Load merged env
load_dotenv()

# ======================================================
# LIFECYCLE
# ======================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Connect SC2 DB
    await db_async.connect()
    yield
    # Disconnect SC2 DB
    await db_async.disconnect()

# ======================================================
# APP SETUP
# ======================================================
app = FastAPI(title="PPE Safety Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# serve uploaded videos
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Include helper router
app.include_router(helper_router)

# ======================================================
# SC2 CONFIG
# ======================================================
API_KEY = os.getenv("API_KEY")
SSL_KEY = os.getenv("SSL_KEY_PATH")
SSL_CERT = os.getenv("SSL_CERT_PATH")
HOST_IP = os.getenv("HOST_IP", "0.0.0.0")
PORT = int(os.getenv("PORT", 8443))

# ======================================================
# WEBSOCKET & STREAM LOGIC
# ======================================================

@app.websocket("/ws/stream/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    try:
        auth = await websocket.receive_text()
        if auth != API_KEY:
            print(f"[-] {client_id} Authentication Failed")
            await websocket.close()
            return

        print(f"[+] {client_id} Connected.")
        detect.active_cameras_info[client_id] = {"status": "Secure", "ip": websocket.client.host}

        while True:
            data = await websocket.receive_bytes()
            nparr = np.frombuffer(data, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if frame is not None:
                await detect.process_frame(frame, client_id)

    except WebSocketDisconnect:
        print(f"[-] {client_id} Disconnected")
        if client_id in detect.frame_buffer: del detect.frame_buffer[client_id]
        if client_id in detect.active_cameras_info: del detect.active_cameras_info[client_id]
        if client_id in detect.camera_states: del detect.camera_states[client_id]

def generate_mjpeg_stream(cam_id):
    while True:
        if cam_id in detect.frame_buffer:
            frame = detect.frame_buffer[cam_id]
            ret, buffer = cv2.imencode('.jpg', frame)
            if ret:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        else:
            time.sleep(0.01)
            continue
        time.sleep(0.05)

@app.get("/video_feed/{cam_id}")
def video_feed(cam_id: str):
    return StreamingResponse(generate_mjpeg_stream(cam_id), media_type="multipart/x-mixed-replace; boundary=frame")

# Replacement for backend's stream proxy
@app.get("/stream/{camera_id}")
def stream_proxy(camera_id: str):
    # Directly stream from local buffer instead of proxying via HTTP
    return StreamingResponse(generate_mjpeg_stream(camera_id), media_type="multipart/x-mixed-replace; boundary=frame")

# ======================================================
# BACKEND API ENDPOINTS
# ======================================================

@app.get("/dashboard-stats")
async def dashboard_stats(db: Session = Depends(get_db)):
    # 1. Fetch active camera count from local memory
    active_camera_count = len(detect.active_cameras_info)

    # 2. Total Violations Today (Live Data)
    try:
        total_violations_today = db.execute(text("""
            SELECT COUNT(*)
            FROM safe.violations
            WHERE DATE(timestamp) = CURRENT_DATE
        """)).scalar()
    except:
        total_violations_today = 0

    # 3. Violations by Type (Live Data)
    try:
        rows = db.execute(text("""
            SELECT violation_type, COUNT(*)
            FROM safe.violations
            GROUP BY violation_type
            ORDER BY COUNT(*) DESC
        """)).fetchall()

        # Simple mapping to clean up names (e.g. "NO-Hardhat" -> "Helmet")
        type_map = {
            "NO-Hardhat": "Helmet",
            "NO-Mask": "Mask",
            "NO-Safety Vest": "Vest",
            "NO-Gloves": "Gloves"
        }

        by_type = []
        for r in rows:
            raw_type = r[0]
            # Handle comma-separated multiple violations if any
            for v in raw_type.split(','):
                v = v.strip()
                clean_name = type_map.get(v, v.replace('NO-', ''))
                # Aggregate if needed (simple append for now)
                by_type.append({"type": clean_name, "count": r[1]})

    except:
        by_type = []

    return {
        "total_cameras": active_camera_count,
        "total_violations_today": total_violations_today,
        "by_type": by_type
    }

@app.get("/cameras")
async def get_cameras(db: Session = Depends(get_db)):
    # 1. Fetch active cameras from local memory
    live_cams = detect.active_cameras_info

    # 2. Fetch configured cameras from DB (optional, if we want names)
    # The original code fetched from DB too.
    # db_rows = db.execute(text("""
    #     SELECT camera_id, camera_name
    #     FROM safe.cameras
    #     ORDER BY camera_id
    # """)).fetchall()

    results = []

    # 3. Process Live Cameras Only
    # Original code filtered by live_cams keys.
    for cam_id, info in live_cams.items():
        # Construct stream URL pointing to THIS server (which handles /stream or /video_feed)
        # Using relative URL or absolute if needed. Backend used absolute http://127.0.0.1:8002/...
        # We can use /stream/{cam_id}
        stream_url = f"/stream/{cam_id}" # or full url

        results.append({
            "id": cam_id,
            "name": f"Active: {cam_id}",
            "stream": stream_url,
            "status": "online"
        })

    return results


@app.get("/compliance-trend")
def compliance_trend(db: Session = Depends(get_db)):

    rows = db.execute(text("""
        SELECT DATE(occurred_at) AS day, COUNT(*)
        FROM safe.live_detection
        WHERE occurred_at >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY day
        ORDER BY day
    """)).fetchall()

    return [
        {"date": str(r[0]), "violations": r[1]}
        for r in rows
    ]

@app.get("/alerts")
def get_alerts(db: Session = Depends(get_db)):

    # Fetch live violations
    try:
        rows = db.execute(text("""
            SELECT
                violation_type,
                camera_id,
                timestamp
            FROM safe.violations
            ORDER BY timestamp DESC
            LIMIT 50
        """)).fetchall()
    except:
        return []

    results = []
    type_map = {
        "NO-Hardhat": "Helmet",
        "NO-Mask": "Mask",
        "NO-Safety Vest": "Safety Vest"
    }

    for r in rows:
        v_type_raw = r[0]
        # Clean up the violation string for display
        clean_type = type_map.get(v_type_raw, v_type_raw.replace('NO-', ''))

        results.append({
            "type": clean_type,
            "camera": r[1],
            "location": "Live Detection",
            "time": str(r[2])
        })

    return results

@app.post("/upload-video")
def upload_video(file: UploadFile = File(...)):

    filepath = f"uploads/{file.filename}"

    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "message": "uploaded",
        "filename": file.filename,
        "url": f"/uploads/{file.filename}"
    }

# ======================================================
# DASHBOARD (ROOT)
# ======================================================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Secure Surveillance Hub</title>
        <meta http-equiv="refresh" content="60">
        <style>
            body { background: #121212; color: #eee; font-family: sans-serif; padding: 20px; }
            h1 { text-align: center; color: #4CAF50; margin-bottom: 30px;}
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr)); gap: 20px; }
            .card { background: #1e1e1e; border: 2px solid #333; border-radius: 8px; overflow: hidden; }
            .video-feed { width: 100%; height: 300px; background: #000; object-fit: cover; display: block; }
            .info { padding: 10px; background: #252525; display: flex; justify-content: space-between; }
            .status { color: #4CAF50; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>🔒 Secure AI Surveillance Hub</h1>
        <p style="text-align:center">Integrated Backend Running</p>
        <div id="grid" class="grid"></div>
        <script>
            let activeCams = new Set();
            async function updateGrid() {
                try {
                    let res = await fetch('/cameras');
                    let cams = await res.json();
                    cams.forEach(cam => {
                         let id = cam.id;
                         if (!activeCams.has(id)) {
                            let card = document.createElement('div');
                            card.className = 'card'; card.id = 'card-' + id;
                            card.innerHTML = `<img src="${cam.stream}" class="video-feed"><div class="info"><strong>${id}</strong><span class="status">🔒 Encrypted</span></div>`;
                            document.getElementById('grid').appendChild(card); activeCams.add(id);
                        }
                    });
                } catch(e) {}
            }
            setInterval(updateGrid, 2000); updateGrid();
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    print(f"[*] Secure Server Active on port {PORT}")
    # Use SSL if available, as in Connect
    uvicorn.run(app, host=HOST_IP, port=PORT, ssl_keyfile=SSL_KEY, ssl_certfile=SSL_CERT)
