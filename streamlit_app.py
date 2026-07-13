from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
import csv
import sqlite3
import time
from typing import Final

import av
import cv2
import pandas as pd
import requests
import streamlit as st
from streamlit_webrtc import WebRtcMode, webrtc_streamer


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

BASE_DIR: Final = Path(__file__).resolve().parent
STUDENT_FILE: Final = BASE_DIR / "StudentDB-2026.csv"
DATABASE_FILE: Final = BASE_DIR / "jumpstart2026.db"

GOOGLE_FORM_VIEW_URL: Final = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSd_Mc-dGWGKyVbBLGHI3PXpfIzn69iofiOowEI4XxH8QHZqUA/"
    "viewform"
)

GOOGLE_FORM_RESPONSE_URL: Final = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSd_Mc-dGWGKyVbBLGHI3PXpfIzn69iofiOowEI4XxH8QHZqUA/"
    "formResponse"
)

GOOGLE_FORM_ENTRY: Final = "entry.2093000039"

DUPLICATE_WINDOW_MINUTES: Final = 5
FRAME_SCAN_INTERVAL_SECONDS: Final = 0.20
LOCAL_QR_COOLDOWN_SECONDS: Final = 3.0

RTC_CONFIGURATION: Final = {
    "iceServers": [
        {"urls": ["stun:stun.l.google.com:19302"]},
    ]
}

try:
    from zoneinfo import ZoneInfo

    LOCAL_TIMEZONE = ZoneInfo("America/New_York")
except ImportError:
    LOCAL_TIMEZONE = timezone.utc


# ---------------------------------------------------------
# Student roster and database functions
# ---------------------------------------------------------

@st.cache_data(show_spinner=False)
def load_students(file_path: str) -> dict[str, str]:
    """Load the student roster from a CSV file."""
    students: dict[str, str] = {}

    with open(file_path, mode="r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        required_columns = {"ID", "FirstName", "LastName"}
        if not required_columns.issubset(reader.fieldnames or []):
            raise ValueError(
                "StudentDB-2026.csv must contain the columns "
                "ID, FirstName, and LastName."
            )

        for row in reader:
            student_id = row["ID"].strip()
            first_name = row["FirstName"].strip()
            last_name = row["LastName"].strip()

            if student_id:
                students[student_id] = f"{first_name} {last_name}".strip()

    return students


def get_connection() -> sqlite3.Connection:
    """Create a short-lived SQLite connection suitable for concurrent threads."""
    connection = sqlite3.connect(
        DATABASE_FILE,
        timeout=10,
        check_same_thread=False,
    )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def initialize_database() -> None:
    """Create the attendance table and supporting index."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                student_ID TEXT NOT NULL,
                student_name TEXT NOT NULL,
                google_form_status TEXT NOT NULL DEFAULT 'not_attempted'
            )
            """
        )

        # Upgrade an older database that does not yet have google_form_status.
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(attendance)").fetchall()
        }
        if "google_form_status" not in columns:
            conn.execute(
                """
                ALTER TABLE attendance
                ADD COLUMN google_form_status TEXT NOT NULL
                DEFAULT 'not_attempted'
                """
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_attendance_student_timestamp
            ON attendance (student_ID, timestamp)
            """
        )
        conn.commit()


def record_attendance(
    student_id: str,
    student_name: str,
    duplicate_window_minutes: int = DUPLICATE_WINDOW_MINUTES,
) -> tuple[bool, str, int | None]:
    """
    Record attendance unless the same student was recorded recently.

    Returns:
        recorded, message, database_row_id
    """
    now = datetime.now(LOCAL_TIMEZONE)
    cutoff = now - timedelta(minutes=duplicate_window_minutes)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")

        recent_record = conn.execute(
            """
            SELECT timestamp
            FROM attendance
            WHERE student_ID = ?
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (student_id, cutoff.isoformat()),
        ).fetchone()

        if recent_record:
            conn.rollback()
            return (
                False,
                f"{student_name} was already checked in during the last "
                f"{duplicate_window_minutes} minutes.",
                None,
            )

        cursor = conn.execute(
            """
            INSERT INTO attendance (
                timestamp,
                student_ID,
                student_name,
                google_form_status
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                now.isoformat(),
                student_id,
                student_name,
                "pending",
            ),
        )
        conn.commit()

        return (
            True,
            f"{student_name} marked present at {now:%I:%M:%S %p}.",
            int(cursor.lastrowid),
        )


def update_google_form_status(row_id: int, status: str) -> None:
    """Store the result of the Google Form submission."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE attendance
            SET google_form_status = ?
            WHERE id = ?
            """,
            (status, row_id),
        )
        conn.commit()


def submit_to_google_form(student_id: str, student_name: str) -> bool:
    """Submit the attendance record to the configured Google Form."""
    try:
        with requests.Session() as session:
            session.get(GOOGLE_FORM_VIEW_URL, timeout=5)
            response = session.post(
                GOOGLE_FORM_RESPONSE_URL,
                data={
                    GOOGLE_FORM_ENTRY: f"{student_id} {student_name}"
                },
                timeout=8,
            )
        return response.ok
    except requests.RequestException:
        return False


def get_attendance_dataframe(limit: int = 200) -> pd.DataFrame:
    """Return recent attendance records for display."""
    with get_connection() as conn:
        dataframe = pd.read_sql_query(
            """
            SELECT
                timestamp,
                student_ID AS "Student ID",
                student_name AS "Student name",
                google_form_status AS "Google Form"
            FROM attendance
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            conn,
            params=(limit,),
        )

    if not dataframe.empty:
        timestamps = pd.to_datetime(dataframe["timestamp"], errors="coerce")
        dataframe["Timestamp"] = timestamps.dt.strftime(
            "%Y-%m-%d %I:%M:%S %p"
        )
        dataframe = dataframe.drop(columns=["timestamp"])
        dataframe = dataframe[
            ["Timestamp", "Student ID", "Student name", "Google Form"]
        ]

    return dataframe


def clear_attendance() -> None:
    """Delete all local attendance records."""
    with get_connection() as conn:
        conn.execute("DELETE FROM attendance")
        conn.commit()


# ---------------------------------------------------------
# Video processing
# ---------------------------------------------------------

class QRScanner:
    """Thread-safe QR scanner state used by the WebRTC frame callback."""

    def __init__(self, students: dict[str, str]) -> None:
        self.students = students
        self.detector = cv2.QRCodeDetector()
        self.lock = Lock()
        self.last_frame_scan = 0.0
        self.recent_qr_times: dict[str, float] = {}
        self.status_message = "Camera ready. Present a QR code."
        self.status_color = (255, 255, 255)

    def process(self, frame: av.VideoFrame) -> av.VideoFrame:
        image = frame.to_ndarray(format="bgr24")
        now_monotonic = time.monotonic()

        with self.lock:
            if now_monotonic - self.last_frame_scan < FRAME_SCAN_INTERVAL_SECONDS:
                self._draw_status(image)
                return av.VideoFrame.from_ndarray(image, format="bgr24")

            self.last_frame_scan = now_monotonic

        try:
            detected, decoded_values, points, _ = (
                self.detector.detectAndDecodeMulti(image)
            )
        except cv2.error:
            detected, decoded_values, points = False, (), None

        if detected and points is not None:
            for raw_value, polygon in zip(decoded_values, points):
                student_id = raw_value.strip()
                if not student_id:
                    continue

                polygon_int = polygon.astype(int)
                is_known_student = student_id in self.students
                outline_color = (
                    (0, 200, 0) if is_known_student else (0, 0, 255)
                )

                cv2.polylines(
                    image,
                    [polygon_int],
                    isClosed=True,
                    color=outline_color,
                    thickness=5,
                )

                self._handle_student_id(
                    student_id=student_id,
                    now_monotonic=now_monotonic,
                )

        self._draw_status(image)
        return av.VideoFrame.from_ndarray(image, format="bgr24")

    def _handle_student_id(
        self,
        student_id: str,
        now_monotonic: float,
    ) -> None:
        with self.lock:
            last_seen = self.recent_qr_times.get(student_id, 0.0)
            if now_monotonic - last_seen < LOCAL_QR_COOLDOWN_SECONDS:
                return
            self.recent_qr_times[student_id] = now_monotonic

        student_name = self.students.get(student_id)

        if student_name is None:
            with self.lock:
                self.status_message = f"Unknown student ID: {student_id}"
                self.status_color = (0, 0, 255)
            return

        try:
            recorded, message, row_id = record_attendance(
                student_id=student_id,
                student_name=student_name,
            )

            if not recorded:
                with self.lock:
                    self.status_message = message
                    self.status_color = (0, 200, 255)
                return

            google_form_ok = submit_to_google_form(
                student_id=student_id,
                student_name=student_name,
            )

            if row_id is not None:
                update_google_form_status(
                    row_id,
                    "submitted" if google_form_ok else "failed",
                )

            with self.lock:
                self.status_message = (
                    f"{message} Google Form: "
                    f"{'submitted' if google_form_ok else 'failed'}."
                )
                self.status_color = (
                    (0, 200, 0) if google_form_ok else (0, 200, 255)
                )

        except Exception as error:
            with self.lock:
                self.status_message = f"Attendance error: {error}"
                self.status_color = (0, 0, 255)

    def _draw_status(self, image) -> None:
        with self.lock:
            message = self.status_message
            color = self.status_color

        height, width = image.shape[:2]
        overlay = image.copy()
        cv2.rectangle(
            overlay,
            (0, 0),
            (width, min(90, height)),
            (0, 0, 0),
            thickness=-1,
        )
        cv2.addWeighted(overlay, 0.55, image, 0.45, 0, image)

        font_scale = max(0.55, min(1.0, width / 900))
        max_chars = max(30, int(width / 11))
        display_message = (
            message
            if len(message) <= max_chars
            else message[: max_chars - 3] + "..."
        )

        cv2.putText(
            image,
            display_message,
            (15, 50),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            color,
            2,
            cv2.LINE_AA,
        )


# ---------------------------------------------------------
# Streamlit interface
# ---------------------------------------------------------

st.set_page_config(
    page_title="Jumpstart 2026 Attendance Checker",
    page_icon="✅",
    layout="wide",
)

st.title("Jumpstart Summer Program 2026")
st.subheader("Live QR Code Attendance Checker")

initialize_database()

try:
    student_database = load_students(str(STUDENT_FILE))
except FileNotFoundError:
    st.error(
        f"Student roster not found: {STUDENT_FILE.name}. "
        "Place it in the same folder as this application."
    )
    st.stop()
except ValueError as error:
    st.error(str(error))
    st.stop()

st.info(
    "Click START, allow camera access, and hold a student's QR code "
    "steadily in front of the camera. A successful scan is suppressed "
    f"for {DUPLICATE_WINDOW_MINUTES} minutes."
)

scanner = QRScanner(student_database)

left_column, right_column = st.columns([3, 2], gap="large")

with left_column:
    webrtc_context = webrtc_streamer(
        key="jumpstart-live-qr-scanner",
        mode=WebRtcMode.SENDRECV,
        rtc_configuration=RTC_CONFIGURATION,
        media_stream_constraints={
            "video": {
                "width": {"ideal": 1280},
                "height": {"ideal": 720},
                "facingMode": "environment",
            },
            "audio": False,
        },
        video_frame_callback=scanner.process,
        async_processing=True,
    )

    if webrtc_context.state.playing:
        st.success("Live scanner is running.")
    else:
        st.caption("Click START above to activate the live scanner.")

with right_column:
    st.markdown("### Scanner guidance")
    st.write(
        "Use good lighting, avoid glare, and keep the full QR code "
        "inside the camera frame. The green outline indicates a recognized "
        "student ID; a red outline indicates an unknown ID."
    )

    st.metric("Students in roster", len(student_database))

    with st.expander("Administrative controls"):
        confirm_clear = st.checkbox(
            "I understand that this deletes the local attendance table."
        )
        if st.button(
            "Clear local attendance",
            type="secondary",
            disabled=not confirm_clear,
        ):
            clear_attendance()
            st.success("Local attendance records were deleted.")


@st.fragment(run_every="1s")
def live_attendance_panel() -> None:
    st.divider()
    st.subheader("Recent Attendance")

    attendance_df = get_attendance_dataframe()

    if attendance_df.empty:
        st.info("No attendance records have been recorded.")
        return

    submitted_count = int(
        (attendance_df["Google Form"] == "submitted").sum()
    )
    failed_count = int(
        (attendance_df["Google Form"] == "failed").sum()
    )

    metric_1, metric_2, metric_3 = st.columns(3)
    metric_1.metric("Displayed records", len(attendance_df))
    metric_2.metric("Google Form submitted", submitted_count)
    metric_3.metric("Submission failures", failed_count)

    st.dataframe(
        attendance_df,
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        label="Download attendance as CSV",
        data=attendance_df.to_csv(index=False).encode("utf-8"),
        file_name="jumpstart2026_attendance.csv",
        mime="text/csv",
        key="download-attendance",
    )


live_attendance_panel()
