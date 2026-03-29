from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import (
    text, Column, Integer, String, TIMESTAMP,
    cast, Date
)
from datetime import date, datetime, timedelta
import csv
import os
import smtplib
from email.message import EmailMessage

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import Paragraph
from reportlab.lib.styles import getSampleStyleSheet

from passlib.context import CryptContext
from pydantic import BaseModel
from db_sync import get_db, Base

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

from fastapi import UploadFile, File
from pathlib import Path
import shutil
import uuid

# =====================================================
# ✅ LOGIN (ONLY THIS PART CHANGED)
# =====================================================

class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login", tags=["1. Login"])
def login(data: LoginRequest, db: Session = Depends(get_db)):

    query = text("""
        SELECT password_hash, full_name
        FROM safe.users
        WHERE email = :email
    """)

    row = db.execute(query, {"email": data.email}).fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="Email not found")

    db_hash = row[0]   # password_hash from DB

    # ✅ verify bcrypt password
    if not pwd_context.verify(data.password, db_hash):
        raise HTTPException(status_code=401, detail="Wrong password")

    return {
        "message": "Login successful",
        "name": row[1],
        "email": data.email
    }


# =====================================================
# DASHBOARD (UNCHANGED)
# =====================================================
class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = {"schema": "safe"}
    camera_id = Column(Integer, primary_key=True)

class LiveDetection(Base):
    __tablename__ = "live_detection"
    __table_args__ = {"schema": "safe"}
    violation_id = Column(Integer, primary_key=True)
    occurred_at = Column(TIMESTAMP)
    image_path = Column(String)

@router.get("/dashboard/summary", tags=["2. Dashboard"])
def dashboard_summary(db: Session = Depends(get_db)):
    return {
        "total_cameras": db.query(Camera).count(),
        "todays_violations": db.query(LiveDetection)
            .filter(cast(LiveDetection.occurred_at, Date) == date.today())
            .count()
    }

# =====================================================
# DASHBOARD COMPLIANCE (NEW API - ADD ONLY THIS)
# =====================================================

@router.get("/dashboard/compliance", tags=["2. Dashboard"])
def dashboard_compliance(db: Session = Depends(get_db)):

    query = text("""
        SELECT
            CURRENT_DATE AS date,
            CASE
                WHEN COUNT(*) = 0 THEN 100
                ELSE ROUND(
                    100 - (COUNT(*) * 100.0 / NULLIF((SELECT COUNT(*) FROM safe.live_detection), 0))
                )
            END AS compliance_rate
        FROM safe.live_detection
        WHERE DATE(occurred_at) = CURRENT_DATE
    """)

    result = db.execute(query).fetchone()

    return [
        {
            "date": str(result[0]),
            "compliance_rate": int(result[1])
        }
    ]
# =========================================================
# -------------------- REPORTS -----------------------------
# =========================================================
def get_detailed_report(from_date, to_date, source, db):
    start = datetime.combine(from_date, datetime.min.time())
    end = datetime.combine(to_date + timedelta(days=1), datetime.min.time())

    data = []

    # -------- LIVE DETECTIONS ----------
    if source in ("live", "all"):
        live_query = text("""
            SELECT
                'LIVE' AS source,
                se.equip_name AS violation,
                s.site_name,
                l.location_name,
                c.camera_name,
                ld.occurred_at,
                ld.image_path AS reference
            FROM safe.live_detection ld
            JOIN safe.safety_equip se ON ld.equip_id = se.equip_id
            JOIN safe.cameras c ON ld.camera_id = c.camera_id
            JOIN safe.locations l ON c.location_id = l.location_id
            JOIN safe.sites s ON l.site_id = s.site_id
            WHERE ld.occurred_at BETWEEN :start AND :end
            ORDER BY ld.occurred_at DESC
        """)
        rows = db.execute(live_query, {"start": start, "end": end}).fetchall()
        for r in rows:
            data.append(dict(r._mapping))

    # -------- VIDEO DETECTIONS ----------
    if source in ("video", "all"):
        video_query = text("""
            SELECT
                'VIDEO' AS source,
                se.equip_name AS violation,
                NULL AS site_name,
                NULL AS location_name,
                NULL AS camera_name,
                NULL AS occurred_at,
                vu.file_path AS reference
            FROM safe.detections d
            JOIN safe.safety_equip se ON d.equip_id = se.equip_id
            JOIN safe.video_uploads vu ON d.video_id = vu.video_id
        """)
        rows = db.execute(video_query).fetchall()
        for r in rows:
            data.append(dict(r._mapping))

    return data


@router.get("/reports/details", tags=["3. Reports"])
def report_details(
    from_date: date = Query(...),
    to_date: date = Query(...),
    source: str = Query("all", pattern="^(live|video|all)$"),
    db: Session = Depends(get_db)
):
    return get_detailed_report(from_date, to_date, source, db)


# =========================================================
# ------------------ EXPORT REPORT ------------------------
# =========================================================
@router.get("/reports/export", tags=["3. Reports"])
def export_report(
    export_type: str = Query(..., pattern="^(csv|pdf)$"),
    from_date: date = Query(...),
    to_date: date = Query(...),
    source: str = Query("all", pattern="^(live|video|all)$"),
    db: Session = Depends(get_db)
):
    data = get_detailed_report(from_date, to_date, source, db)

    if not data:
        raise HTTPException(404, "No data found")

    # ---------------- CSV ----------------
    if export_type == "csv":
        filename = "report.csv"
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        return FileResponse(filename, filename=filename)

    # ---------------- PDF ----------------
    filename = "report.pdf"

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    header = styles["Heading4"]

    headers = ["Source", "Violation", "Site", "Location", "Camera", "Time", "Reference"]

    table_data = [[Paragraph(f"<b>{h}</b>", header) for h in headers]]

    for d in data:
        table_data.append([
            Paragraph(str(d["source"] or ""), normal),
            Paragraph(str(d["violation"] or ""), normal),
            Paragraph(str(d["site_name"] or ""), normal),
            Paragraph(str(d["location_name"] or ""), normal),
            Paragraph(str(d["camera_name"] or ""), normal),
            Paragraph(str(d["occurred_at"] or ""), normal),
            Paragraph(str(d["reference"] or ""), normal),
        ])

    pdf = SimpleDocTemplate(
        filename,
        pagesize=A4,
        leftMargin=20,
        rightMargin=20,
        topMargin=20,
        bottomMargin=20
    )

    table = Table(
        table_data,
        colWidths=[50, 70, 90, 90, 90, 80, 120]
    )

    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.5, colors.black),
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("FONTSIZE", (0,0), (-1,-1), 8),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))

    pdf.build([table])
    return FileResponse(filename, filename=filename)
# =====================================================
# ALERTS (UNCHANGED)
# =====================================================
@router.post("/alerts/send", tags=["4. Alerts"])
def send_alert(violation_id: int, db: Session = Depends(get_db)):

    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT"))
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")

    query = text("""
        SELECT l.email, se.equip_name, ld.occurred_at
        FROM safe.live_detection ld
        JOIN safe.safety_equip se ON ld.equip_id = se.equip_id
        JOIN safe.cameras c ON ld.camera_id = c.camera_id
        JOIN safe.locations l ON c.location_id = l.location_id
        WHERE ld.violation_id = :vid
    """)
    row = db.execute(query, {"vid": violation_id}).fetchone()

    if not row:
        raise HTTPException(404, "Violation not found")

    msg = EmailMessage()
    msg["From"] = smtp_email
    msg["To"] = row[0]
    msg["Subject"] = "PPE ALERT"
    msg.set_content(f"Violation: {row[1]}\nTime: {row[2]}")

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_email, smtp_password)
        server.send_message(msg)

    db.execute(
        text("INSERT INTO safe.alerts (violation_id, email) VALUES (:vid, :email)"),
        {"vid": violation_id, "email": row[0]}
    )
    db.commit()

    return {"message": "Alert sent", "email": row[0]}

   # =====================================================
# VIDEO MONITOR (SINGLE FILE – NO EXTRA FILES)
# =====================================================

from fastapi import UploadFile, File
from pathlib import Path
import shutil
import uuid
import threading
import time

# -------------------------------
# VIDEO UPLOAD
# -------------------------------

UPLOAD_DIR = Path("uploaded_videos")
UPLOAD_DIR.mkdir(exist_ok=True)

@router.post("/upload/video", tags=["5. Video Upload"])
async def upload_video(file: UploadFile = File(...)):
    video_id = f"UPLOAD_{uuid.uuid4().hex[:6]}"
    file_path = UPLOAD_DIR / f"{video_id}_{file.filename}"

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "camera_id": video_id,
        "video_path": str(file_path),
        "status": "uploaded"
    }


# -------------------------------
# SIMPLE IN-MEMORY RUNNER
# -------------------------------

class VideoMonitorRunner:
    def __init__(self):
        self.is_running = False
        self.video_path = None
        self.camera_id = None
        self.thread = None

    def _run(self):
        while self.is_running:
            print(
                f"[MONITOR] Processing video={self.video_path} | camera={self.camera_id}"
            )
            time.sleep(2)

    def start(self, video_path: str, camera_id: str):
        if self.is_running:
            return

        self.video_path = video_path
        self.camera_id = camera_id
        self.is_running = True

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.is_running = False

    def get_status(self):
        return {
            "running": self.is_running,
            "camera_id": self.camera_id,
            "video_path": self.video_path
        }


# -------------------------------
# GLOBAL RUNNER INSTANCE
# -------------------------------

runner = VideoMonitorRunner()


# -------------------------------
# MONITOR APIs
# -------------------------------

@router.post("/monitor/start", tags=["6. Monitor"])
def start_monitor(
    video_path: str = Query(...),
    camera_id: str = Query(...)
):
    if runner.is_running:
        raise HTTPException(status_code=400, detail="Monitoring already running")

    runner.start(video_path, camera_id)

    return {
        "status": "started",
        "camera_id": camera_id,
        "video_path": video_path
    }


@router.post("/monitor/stop", tags=["6. Monitor"])
def stop_monitor():
    if not runner.is_running:
        raise HTTPException(status_code=400, detail="Monitoring not running")

    runner.stop()
    return {"status": "stopped"}


@router.get("/monitor/status", tags=["6. Monitor"])
def monitor_status():
    return runner.get_status()
