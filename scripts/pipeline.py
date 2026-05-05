import json
import os
import logging
from pathlib import Path
from typing import Optional
import glob
import re as re_module
from datetime import date, timedelta

import ephem
from groq import Groq as _Groq
import base64 as _base64
import pdfplumber as _pdfplumber
import fitz as _fitz
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import requests
import yaml
from icalendar import Calendar

# ── Config ───────────────────────────────────────────────────────────────────
_CFG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CFG_PATH, encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

INPUT_DIR  = _cfg["paths"]["input_dir"]
OUTPUT_DIR = _cfg["paths"]["output_dir"]
LOGS_DIR   = _cfg["paths"]["logs_dir"]

# ── Logging ───────────────────────────────────────────────────────────────────
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
_lc = _cfg.get("logging", {})
logging.basicConfig(
    level=getattr(logging, _lc.get("level", "INFO"), logging.INFO),
    format=_lc.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    datefmt=_lc.get("date_format", "%Y-%m-%d %H:%M:%S"),
    handlers=[
        logging.FileHandler(Path(LOGS_DIR) / "pipeline.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("pipeline")

# ── Constants ─────────────────────────────────────────────────────────────────
THAI_MONTHS = {
    "มกราคม":1,"กุมภาพันธ์":2,"มีนาคม":3,
    "เมษายน":4,"พฤษภาคม":5,"มิถุนายน":6,
    "กรกฎาคม":7,"สิงหาคม":8,"กันยายน":9,
    "ตุลาคม":10,"พฤศจิกายน":11,"ธันวาคม":12
}
THAI_MONTHS_SHORT = {
    1:"ม.ค.",2:"ก.พ.",3:"มี.ค.",4:"เม.ย.",
    5:"พ.ค.",6:"มิ.ย.",7:"ก.ค.",8:"ส.ค.",
    9:"ก.ย.",10:"ต.ค.",11:"พ.ย.",12:"ธ.ค."
}
THAI_MONTHS_FULL = {
    1:"มกราคม",2:"กุมภาพันธ์",3:"มีนาคม",4:"เมษายน",
    5:"พฤษภาคม",6:"มิถุนายน",7:"กรกฎาคม",8:"สิงหาคม",
    9:"กันยายน",10:"ตุลาคม",11:"พฤศจิกายน",12:"ธันวาคม"
}
THAI_DAYS = {
    0:"จันทร์",1:"อังคาร",2:"พุธ",
    3:"พฤหัสบดี",4:"ศุกร์",5:"เสาร์",6:"อาทิตย์"
}

SEMESTERS = {
    "ภาคการศึกษาที่ 1": 1,
    "ภาคการศึกษาที่ 2": 2,
    "ภาคฤดูร้อน": 3,
}
_THAI_DIGIT_TABLE = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

FACULTY_SPECIAL_MAP = {
    2566: {
        "BBA": [
            {"ภาคเรียน": 1, "เปิด": date(2023,8,7),   "ปิด": date(2023,12,7)},
            {"ภาคเรียน": 2, "เปิด": date(2024,1,3),   "ปิด": date(2024,5,2)},
        ],
        "Dent": [
            {"ภาคเรียน": 1, "เปิด": date(2023,5,29),  "ปิด": date(2024,5,2)},
        ],
        "Med": [
            {"ภาคเรียน": 1, "เปิด": date(2023,5,29),  "ปิด": date(2024,4,18)},
        ],
    },
    2567: {
        "BBA": [
            {"ภาคเรียน": 1, "เปิด": date(2024,8,13),  "ปิด": date(2024,12,14)},
            {"ภาคเรียน": 2, "เปิด": date(2025,1,6),   "ปิด": date(2025,5,10)},
        ],
        "Dent": [
            {"ภาคเรียน": 1, "เปิด": date(2024,5,27),  "ปิด": date(2025,4,30)},
        ],
        "Med": [
            {"ภาคเรียน": 1, "เปิด": date(2024,5,27),  "ปิด": date(2025,4,25)},
        ],
    },
    2568: {
        "BBA": [
            {"ภาคเรียน": 1, "เปิด": date(2025,8,13),  "ปิด": date(2025,12,13)},
            {"ภาคเรียน": 2, "เปิด": date(2026,1,12),  "ปิด": date(2026,5,15)},
        ],
        "Dent": [
            {"ภาคเรียน": 1, "เปิด": date(2025,5,13),  "ปิด": date(2026,4,10)},
        ],
        "Med": [
            {"ภาคเรียน": 1, "เปิด": date(2025,6,23),  "ปิด": date(2026,4,24)},
        ],
    },
}

FACULTY_NAME_MAP = {
    "Normal": "Normal",
    "BBA":    "BBA",
    "Dent":   "Dent",
    "Med":    "Med",
}

CAMPUSES = {
    "HatYai":     "วิทยาเขตหาดใหญ่",
    "Pattani":    "วิทยาเขตปัตตานี",
    "Phuket":     "วิทยาเขตภูเก็ต",
    "Trang":      "วิทยาเขตตรัง",
    "Suratthani": "วิทยาเขตสุราษฎร์ฯ",
}

# ── DB helpers ────────────────────────────────────────────────────────────────
_DROP_OLD_SQL = "DROP TABLE IF EXISTS date_dimension CASCADE"

_DIM_DATE_SQL = """
CREATE TABLE IF NOT EXISTS dim_date (
    id          BIGSERIAL PRIMARY KEY,
    date_actual DATE UNIQUE NOT NULL,
    date_str    VARCHAR(12),
    year_ce     INTEGER,
    year_be     INTEGER,
    month_num   INTEGER,
    month_name  TEXT,
    month_short TEXT,
    day_num     INTEGER,
    day_name    TEXT,
    day_of_week INTEGER,
    quarter     VARCHAR(4),
    is_weekend  BOOLEAN
)"""

_DIM_CAMPUS_SQL = """
CREATE TABLE IF NOT EXISTS dim_campus (
    id             SERIAL PRIMARY KEY,
    campus_code    TEXT UNIQUE NOT NULL,
    campus_name_th TEXT
)"""

_DIM_FACULTY_SQL = """
CREATE TABLE IF NOT EXISTS dim_faculty (
    id              SERIAL PRIMARY KEY,
    faculty_code    TEXT UNIQUE NOT NULL,
    faculty_name_th TEXT
)"""

_FACT_SQL = """
CREATE TABLE IF NOT EXISTS fact_academic_calendar (
    id               BIGSERIAL PRIMARY KEY,
    date_id          BIGINT REFERENCES dim_date(id),
    campus_id        INTEGER REFERENCES dim_campus(id),
    faculty_id       INTEGER REFERENCES dim_faculty(id),
    academic_year    INTEGER,
    semester         INTEGER,
    week_of_semester INTEGER,
    is_academic_day  BOOLEAN,
    is_holiday       BOOLEAN,
    holiday_name     TEXT,
    day_type         TEXT,
    source           TEXT DEFAULT 'pipeline',
    UNIQUE(date_id, campus_id, faculty_id, day_type)
)"""

_tables_initialized = False


def _init_schema(cur):
    global _tables_initialized
    if _tables_initialized:
        return
    cur.execute(_DROP_OLD_SQL)
    cur.execute(_DIM_DATE_SQL)
    cur.execute(_DIM_CAMPUS_SQL)
    cur.execute(_DIM_FACULTY_SQL)
    cur.execute(_FACT_SQL)
    # ล้างข้อมูลวันหยุดเก่าที่มาจาก Excel ออกจาก DB
    cur.execute("""
        UPDATE fact_academic_calendar
        SET is_holiday = false, holiday_name = null, day_type = 'วันทำการ', source = 'pipeline'
        WHERE source = 'excel'
           OR holiday_name IN (
               'วันวาเลนไทน์', 'วันตรุษจีน', 'วันตรุษจีน (วันที่ 2)',
               'วันตรุษจีน (วันที่ 3)', 'นักขัตฤกษ์'
           )
    """)
    _tables_initialized = True


def validate_before_insert(df: pd.DataFrame) -> list:
    """ตรวจ conflict 3 ประเภทในข้อมูลก่อน insert.

    1. วันเปิดภาคทับวันหยุด    — is_academic_day=True AND is_holiday=True พร้อมกัน
    2. วันหยุดทับวันสอบ         — is_holiday=True AND ประเภทวัน มีคำว่า 'สอบ'
    3. วันหยุดซ้ำหลาย day_type — (date, campus, faculty) เดียวกัน มี ประเภทวัน > 1 ประเภท

    Returns:
        list[dict] — แต่ละ dict มี: conflict_type, date, campus, faculty, detail
    """
    conflicts: list = []
    if df.empty:
        return conflicts

    # ── 1. วันเปิดภาคทับวันหยุด ──────────────────────────────────────────────
    mask1 = df["is_academic_day"].astype(bool) & df["is_holiday"].astype(bool)
    for _, r in df[mask1].iterrows():
        conflicts.append({
            "conflict_type": "วันเปิดภาคทับวันหยุด",
            "date":    r["date_actual"],
            "campus":  r.get("วิทยาเขต", "-"),
            "faculty": r.get("คณะ", "-"),
            "detail":  f"is_academic_day=True แต่เป็นวันหยุด: {r.get('ชื่อวันหยุด', '')}",
        })

    # ── 2. วันหยุดทับวันสอบ ───────────────────────────────────────────────────
    if "ประเภทวัน" in df.columns:
        mask2 = (
            df["is_holiday"].astype(bool) &
            df["ประเภทวัน"].str.contains("สอบ", na=False)
        )
        for _, r in df[mask2].iterrows():
            conflicts.append({
                "conflict_type": "วันหยุดทับวันสอบ",
                "date":    r["date_actual"],
                "campus":  r.get("วิทยาเขต", "-"),
                "faculty": r.get("คณะ", "-"),
                "detail":  (
                    f"is_holiday=True + ประเภทวัน={r['ประเภทวัน']}"
                    f"  ชื่อวันหยุด: {r.get('ชื่อวันหยุด', '')}"
                ),
            })

    # ── 3. วันหยุดซ้ำหลาย day_type ───────────────────────────────────────────
    if "ประเภทวัน" in df.columns:
        grp_cols = [c for c in ("date_actual", "วิทยาเขต", "คณะ") if c in df.columns]
        if grp_cols:
            counts = df.groupby(grp_cols)["ประเภทวัน"].nunique()
            for idx, cnt in counts[counts > 1].items():
                idx_t = idx if isinstance(idx, tuple) else (idx,)
                flt   = pd.Series([True] * len(df), index=df.index)
                for col, val in zip(grp_cols, idx_t):
                    flt &= df[col] == val
                types = df.loc[flt, "ประเภทวัน"].unique().tolist()
                conflicts.append({
                    "conflict_type": "วันหยุดซ้ำหลาย day_type",
                    "date":    idx_t[0],
                    "campus":  idx_t[1] if len(idx_t) > 1 else "-",
                    "faculty": idx_t[2] if len(idx_t) > 2 else "-",
                    "detail":  f"ประเภทวัน: {' / '.join(str(t) for t in types)}",
                })

    if conflicts:
        logger.warning(
            "validate_before_insert: พบ %d conflict(s) — ตรวจสอบก่อน insert",
            len(conflicts),
        )
    return conflicts


def save_to_db(df: pd.DataFrame) -> None:
    needed = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [v for v in needed if not os.environ.get(v)]
    if missing:
        logger.warning("Skipping DB save — missing env vars: %s", missing)
        return

    conn = None
    try:
        conn = psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", 5432)),
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            client_encoding="utf8",
        )
        cur = conn.cursor()
        _init_schema(cur)

        # 1. Upsert dim_date (unique dates in this batch)
        dd = (
            df[["date_actual", "วันที่", "ปี_คศ", "ปี_พศ", "เดือน_ตัวเลข",
                "เดือน", "เดือน_ย่อ", "วันที่_ตัวเลข", "วัน", "วัน_ตัวเลข",
                "ไตรมาส", "is_weekend"]]
            .drop_duplicates(subset=["date_actual"])
        )
        date_tuples = list(dd.itertuples(index=False, name=None))
        execute_values(cur, """
            INSERT INTO dim_date
                (date_actual, date_str, year_ce, year_be, month_num, month_name,
                 month_short, day_num, day_name, day_of_week, quarter, is_weekend)
            VALUES %s
            ON CONFLICT (date_actual) DO UPDATE SET
                date_str    = EXCLUDED.date_str,
                year_ce     = EXCLUDED.year_ce,
                year_be     = EXCLUDED.year_be,
                month_num   = EXCLUDED.month_num,
                month_name  = EXCLUDED.month_name,
                month_short = EXCLUDED.month_short,
                day_num     = EXCLUDED.day_num,
                day_name    = EXCLUDED.day_name,
                day_of_week = EXCLUDED.day_of_week,
                quarter     = EXCLUDED.quarter,
                is_weekend  = EXCLUDED.is_weekend
        """, date_tuples)

        actuals = [t[0] for t in date_tuples]
        cur.execute(
            "SELECT date_actual, id FROM dim_date WHERE date_actual = ANY(%s)",
            (actuals,),
        )
        date_id_map = {row[0]: row[1] for row in cur.fetchall()}

        # 2-4. For each (campus, faculty) group: upsert dims then fact rows
        df2 = df.copy()
        df2["คณะ"] = df2["คณะ"].fillna("Normal").replace("", "Normal")

        for (campus_code, faculty_code), grp in df2.groupby(["วิทยาเขต", "คณะ"]):
            campus_th = CAMPUSES.get(campus_code, campus_code)
            cur.execute("""
                INSERT INTO dim_campus (campus_code, campus_name_th)
                VALUES (%s, %s)
                ON CONFLICT (campus_code) DO UPDATE SET campus_name_th = EXCLUDED.campus_name_th
                RETURNING id
            """, (campus_code, campus_th))
            campus_id = cur.fetchone()[0]

            faculty_th = FACULTY_NAME_MAP.get(faculty_code, faculty_code)
            cur.execute("""
                INSERT INTO dim_faculty (faculty_code, faculty_name_th)
                VALUES (%s, %s)
                ON CONFLICT (faculty_code) DO UPDATE SET faculty_name_th = EXCLUDED.faculty_name_th
                RETURNING id
            """, (faculty_code, faculty_th))
            faculty_id = cur.fetchone()[0]

            fact_rows = []
            for _, row in grp.iterrows():
                d_id = date_id_map[row["date_actual"]]
                sem  = row["ภาคเรียน"]
                sem  = None if pd.isna(sem) else int(sem)
                wk   = row["สัปดาห์ที่_ของภาค"]
                wk   = None if pd.isna(wk) else int(wk)
                fact_rows.append((
                    d_id, campus_id, faculty_id,
                    int(row["ปีการศึกษา"]),
                    sem, wk,
                    bool(row["is_academic_day"]),
                    bool(row["is_holiday"]),
                    row["ชื่อวันหยุด"] or None,
                    row["ประเภทวัน"],
                    row.get("source", "pipeline"),
                ))
            execute_values(cur, """
                INSERT INTO fact_academic_calendar
                    (date_id, campus_id, faculty_id, academic_year, semester,
                     week_of_semester, is_academic_day, is_holiday, holiday_name, day_type, source)
                VALUES %s
                ON CONFLICT (date_id, campus_id, faculty_id, day_type) DO UPDATE SET
                    academic_year    = EXCLUDED.academic_year,
                    semester         = EXCLUDED.semester,
                    week_of_semester = EXCLUDED.week_of_semester,
                    is_academic_day  = EXCLUDED.is_academic_day,
                    is_holiday       = EXCLUDED.is_holiday,
                    holiday_name     = EXCLUDED.holiday_name,
                    source           = EXCLUDED.source
            """, fact_rows)

        conn.commit()
        logger.info("Saved %d rows to database", len(df))

    except psycopg2.Error as exc:
        logger.error("Database error: %s", exc)
        if conn:
            conn.rollback()
    except Exception as exc:
        logger.error("Unexpected error saving to DB: %s", exc)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

# ── Pipeline functions ────────────────────────────────────────────────────────

def parse_date_str(day, month_th, year_be):
    m = THAI_MONTHS.get(month_th, 0)
    if m == 0:
        logger.warning("Unknown Thai month: %r", month_th)
        return None
    try:
        return date(int(year_be) - 543, m, int(day))
    except ValueError as exc:
        logger.warning("Invalid date %s %s %s: %s", day, month_th, year_be, exc)
        return None


# ── Groq Vision helpers ───────────────────────────────────────────────────────

_GROQ_TEXT_MODEL   = "llama-3.3-70b-versatile"
_GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

_GROQ_PATITIN_PROMPT = """\
อ่านปฏิทินการศึกษาของมหาวิทยาลัยสงขลานครินทร์จาก PDF นี้
ดึงวันเปิดและวันปิดของแต่ละภาคการศึกษา ตอบเป็น JSON เท่านั้น (ห้ามมีข้อความอื่น):

{
  "academic_year": 2568,
  "campus": "HatYai",
  "semesters": [
    {"semester": 1, "open": "DD/MM/YYYY", "close": "DD/MM/YYYY"},
    {"semester": 2, "open": "DD/MM/YYYY", "close": "DD/MM/YYYY"},
    {"semester": 3, "open": "DD/MM/YYYY", "close": "DD/MM/YYYY"}
  ]
}

กฎ:
- วันที่รูปแบบ DD/MM/YYYY เป็น พ.ศ. (ถ้าเอกสารเป็น ค.ศ. ให้บวก 543)
- campus: HatYai, Pattani, Phuket, Trang, หรือ Suratthani
- ภาคเรียน 3 = ภาคฤดูร้อน (ถ้าไม่มีให้ละไว้)
- เลขไทย (๑,๒,...) ให้แปลงเป็นเลขอาหรับ"""


def _get_groq_client():
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError("GROQ_API_KEY environment variable is not set")
    return _Groq(api_key=api_key)


def _pdf_to_b64_images(pdf_path: str, max_pages: int = 4) -> list:
    doc = _fitz.open(pdf_path)
    images = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=_fitz.Matrix(1.5, 1.5))
        images.append(_base64.b64encode(pix.tobytes("png")).decode())
    return images


def _groq_ask_pdf(pdf_path: str, prompt: str) -> str:
    """ส่ง PDF ให้ Groq: ลอง pdfplumber ก่อน ถ้าไม่มีข้อความค่อยใช้ vision"""
    client = _get_groq_client()
    text = ""
    try:
        with _pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        pass

    if text.strip():
        resp = client.chat.completions.create(
            model=_GROQ_TEXT_MODEL,
            messages=[{"role": "user", "content": prompt + "\n\nข้อความจากเอกสาร:\n" + text[:8000]}],
            temperature=0.0,
        )
    else:
        images = _pdf_to_b64_images(pdf_path)
        content = [{"type": "text", "text": prompt}]
        for b64 in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        resp = client.chat.completions.create(
            model=_GROQ_VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0.0,
        )
    return resp.choices[0].message.content.strip()


def _parse_be_date_str(s) -> Optional[date]:
    if not s:
        return None
    s = str(s).strip().translate(_THAI_DIGIT_TABLE)
    m = re_module.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if not m:
        return None
    day, month, yr_be = int(m.group(1)), int(m.group(2)), int(m.group(3))
    yr_ce = yr_be - 543 if yr_be > 2500 else yr_be
    try:
        return date(yr_ce, month, day)
    except ValueError:
        return None


def parse_patitin_pdf(pdf_path: str) -> list:
    """Parse semester open/close dates from a PDF using Groq."""
    try:
        raw = _groq_ask_pdf(str(pdf_path), _GROQ_PATITIN_PROMPT)
        raw = re_module.sub(r'```[a-z]*\n?', '', raw)
        raw = re_module.sub(r'\n?```', '', raw).strip()
        data = json.loads(raw)

        results = []
        for sem in data.get("semesters", []):
            open_d  = _parse_be_date_str(sem.get("open"))
            close_d = _parse_be_date_str(sem.get("close"))
            if open_d and close_d:
                results.append({
                    "ภาคเรียน": int(sem["semester"]),
                    "เปิด":     open_d,
                    "ปิด":      close_d,
                })
        logger.info("Groq parsed %s → %d semesters", Path(pdf_path).name, len(results))
        return results
    except Exception as exc:
        logger.error("Groq parse failed for %s: %s", Path(pdf_path).name, exc)
        return []


def get_year_from_pdf(pdf_path):
    YEAR_MIN, YEAR_MAX = 2564, 2569
    # ลองดึงปีจากชื่อไฟล์ก่อน (ไม่ต้องเรียก API)
    m = re_module.search(r'(25\d{2})', Path(pdf_path).stem)
    if m:
        yr = int(m.group(1))
        if YEAR_MIN <= yr <= YEAR_MAX:
            return yr
    # Fallback: ถามจาก Groq
    try:
        raw = _groq_ask_pdf(str(pdf_path), 'Return only JSON {"year": YYYY} with the BE academic year (พ.ศ., 25XX) from this document.')
        raw = re_module.sub(r'```[a-z]*\n?', '', raw.strip())
        raw = re_module.sub(r'\n?```', '', raw).strip()
        yr  = int(json.loads(raw).get("year", 0))
        if YEAR_MIN <= yr <= YEAR_MAX:
            return yr
    except Exception as exc:
        logger.warning("Groq year detection failed for %s: %s", Path(pdf_path).name, exc)
    # Last resort: CE year in filename + 543
    m = re_module.search(r'(\d{4})', Path(pdf_path).stem)
    if m:
        yr    = int(m.group(1))
        yr_be = yr + 543 if yr < 2500 else yr
        if YEAR_MIN <= yr_be <= YEAR_MAX:
            return yr_be
    return None

def get_buddhist_holidays(academic_year):
    ce_year = academic_year - 543
    hol = {}
    def full_moon_after(month, day, year):
        d = ephem.Date(f"{year}/{month}/{day}")
        next_full = ephem.next_full_moon(d)
        dt = ephem.Date(next_full).datetime()
        return date(dt.year, dt.month, dt.day)
    makha   = full_moon_after(1, 15, ce_year)
    visakha = full_moon_after(4, 15, ce_year)
    asanha  = full_moon_after(6, 15, ce_year)
    hol[makha]                      = "วันมาฆบูชา"
    hol[visakha]                    = "วันวิสาขบูชา"
    hol[asanha]                     = "วันอาสาฬหบูชา"
    hol[asanha + timedelta(days=1)] = "วันเข้าพรรษา"
    return hol

def be_to_ce(date_str):
    parts = str(date_str).strip().split("/")
    day, month, year_be = int(parts[0]), int(parts[1]), int(parts[2])
    return date(year_be - 543, month, day)

_GOOGLE_ICAL_URL = (
    "https://calendar.google.com/calendar/ical/"
    "th.th%23holiday%40group.v.calendar.google.com/public/basic.ics"
)

def get_thai_holidays_google(academic_year: int) -> dict:
    """Fetch Thai public holidays from Google Calendar iCal for a BE academic year.

    Covers the two CE years that the academic year spans (e.g. 2567 BE → 2024-2025 CE).
    Returns {} and logs a warning if the feed cannot be reached.
    """
    ce_years = {academic_year - 543, academic_year - 543 + 1}
    try:
        resp = requests.get(_GOOGLE_ICAL_URL, timeout=15)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
        hol = {}
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            dtstart = component.get("DTSTART")
            if dtstart is None:
                continue
            event_date = dtstart.dt
            if hasattr(event_date, "date"):
                event_date = event_date.date()
            if event_date.year not in ce_years:
                continue
            summary = str(component.get("SUMMARY", "")).strip()
            if summary:
                hol[event_date] = summary
        logger.info(
            "Loaded %d Thai public holidays from Google Calendar (year %s)",
            len(hol), academic_year,
        )
        return hol
    except Exception as exc:
        logger.warning(
            "Cannot fetch Google Calendar holidays (%s) — falling back to Excel only", exc
        )
        return {}

def get_islamic_holidays(academic_year: int) -> dict:
    """คำนวณวันหยุดอิสลามหลักสำหรับปีการศึกษา BE (ใช้ hijridate).

    ครอบคลุมวันสำคัญ:
      - วันอีดิ้ลฟิฏร์  (1 เชาวาล 1H)   + วันถัดไป
      - วันอีดิ้ลอัฎฮา  (10 ซุลหิจญะห์ 1H) + 3 วันถัดไป
      - วันเมาลิด       (12 เราะบีอุลเอาวัล 1H)
      - วันอาชูรออ์     (10 มุฮัรรอม 1H)

    แปลง Hijri → CE แล้ว filter เฉพาะวันที่อยู่ในช่วงปีการศึกษา.
    คืน {} เงียบๆ ถ้า hijridate ไม่ได้ติดตั้ง.
    """
    try:
        from hijridate import Hijri, Gregorian
    except ImportError:
        logger.warning("hijridate ไม่ได้ติดตั้ง — ข้ามวันหยุดอิสลาม")
        return {}

    # ปีการศึกษา BE ครอบคลุมสองปี CE
    ce_years = {academic_year - 543, academic_year - 543 + 1}

    # ประมาณปี Hijri ที่ต้องสแกน (1 CE ≈ 1.0307 Hijri)
    hol: dict[date, str] = {}

    def _hijri_to_ce(hh: int, hm: int, hd: int) -> Optional[date]:
        try:
            g = Hijri(hh, hm, hd).to_gregorian()
            return date(g.year, g.month, g.day)
        except Exception:
            return None

    def _add_range(start: Optional[date], days: int, name: str):
        if not start:
            return
        for i in range(days):
            d = start + timedelta(days=i)
            if d.year in ce_years:
                hol[d] = name

    # สแกน Hijri ปีที่ครอบคลุม CE ช่วงนั้น (~2 ปี Hijri)
    base_ce  = min(ce_years)
    # Hijri year ≈ (CE - 622) * 1.0307 + 1
    hj_start = max(1, int((base_ce - 622) * 1.0307))
    for hj_yr in range(hj_start, hj_start + 3):
        # อีดิ้ลฟิฏร์: 1-2 เชาวาล
        _add_range(_hijri_to_ce(hj_yr, 10, 1), 2, "วันอีดิ้ลฟิฏร์")
        # อีดิ้ลอัฎฮา: 10-13 ซุลหิจญะห์
        _add_range(_hijri_to_ce(hj_yr, 12, 10), 4, "วันอีดิ้ลอัฎฮา")
        # เมาลิด: 12 เราะบีอุลเอาวัล
        _add_range(_hijri_to_ce(hj_yr, 3, 12), 1, "วันเมาลิดนบี")
        # อาชูรออ์: 10 มุฮัรรอม
        _add_range(_hijri_to_ce(hj_yr, 1, 10), 1, "วันอาชูรออ์")

    logger.info(
        "Islamic holidays for academic_year %s: %d วัน",
        academic_year, len(hol),
    )
    return hol


def load_holidays(sheet_name, academic_year):
    """โหลดวันหยุดจาก Google Calendar + ephem เท่านั้น (ไม่ใช้ Excel อีกต่อไป).

    Returns:
        hol     : dict[date, str]  — ชื่อวันหยุด
        sources : dict[date, str]  — แหล่งข้อมูล (google_calendar/ephem)
    """
    hol:     dict[date, str] = {}
    sources: dict[date, str] = {}

    # 1. Google Calendar: วันหยุดราชการไทย (source of truth หลัก)
    for d, name in get_thai_holidays_google(academic_year).items():
        hol[d]     = name
        sources[d] = "google_calendar"

    # 2. ephem: วันหยุดทางพุทธศาสนา (เติมช่องว่างที่ Google ไม่มี)
    for yr in [academic_year, academic_year + 1]:
        for d, name in get_buddhist_holidays(yr).items():
            if d not in hol:
                hol[d]     = name
                sources[d] = "ephem"

    # 3. วันหยุดอิสลาม: เฉพาะวิทยาเขตปัตตานี
    if "ปัตตานี" in sheet_name or sheet_name == "วิทยาเขตปัตตานี":
        for d, name in get_islamic_holidays(academic_year).items():
            if d not in hol:
                hol[d]     = name
                sources[d] = "ephem"

    return hol, sources

def get_faculty_pdfs(target_year):
    result = {}
    keywords = {
        "BBA":  "bba",
        "Dent": "dent",
        "Med":  "med",
    }
    all_pdfs = glob.glob(f"{INPUT_DIR}/*.pdf")
    for faculty, kw in keywords.items():
        for f in all_pdfs:
            if kw in Path(f).name.lower():
                be_year = get_year_from_pdf(f)
                if be_year == target_year:
                    result[faculty] = f
                    logger.info("Found %s: %s year %s", faculty, Path(f).name, be_year)
                    break
                logger.warning(
                    "PDF %s matches faculty %s but year %s != target %s",
                    Path(f).name, faculty, be_year, target_year,
                )
        if faculty not in result:
            logger.warning("No PDF found for faculty %s year %s", faculty, target_year)
    return result

def build_dim(name, sem_ranges, holidays, academic_year, include_faculty=True, campus_name="HatYai", hol_sources=None):
    if hol_sources is None:
        hol_sources = {}
    start = min(s["เปิด"] for s in sem_ranges)
    end   = max(s["ปิด"]  for s in sem_ranges)

    def get_semester(d):
        for s in sem_ranges:
            if s["เปิด"] <= d <= s["ปิด"]:
                return s["ภาคเรียน"], f"เทอม {s['ภาคเรียน']}"
        for i in range(len(sem_ranges) - 1):
            if sem_ranges[i]["ปิด"] < d < sem_ranges[i+1]["เปิด"]:
                return None, f"ปิดระหว่างเทอม {sem_ranges[i]['ภาคเรียน']}-{sem_ranges[i+1]['ภาคเรียน']}"
        return None, "ปิดภาค"

    rows = []
    week_counters = {s["ภาคเรียน"]: 1 for s in sem_ranges}
    current = start

    while current <= end:
        be       = current.year + 543
        wday     = current.weekday()
        is_wknd  = wday >= 5
        is_hol   = current in holidays
        hol_name = holidays.get(current) or None
        quarter  = (current.month - 1) // 3 + 1
        sem_num, sem_label = get_semester(current)
        is_in_sem = sem_num is not None

        # Holidays in inter-semester breaks inherit the nearest semester
        row_sem = sem_num
        if is_hol and sem_num is None:
            future = [s for s in sem_ranges if s["เปิด"] > current]
            past   = [s for s in sem_ranges if s["ปิด"]  < current]
            if future:
                row_sem = min(future, key=lambda s: s["เปิด"])["ภาคเรียน"]
            elif past:
                row_sem = max(past,   key=lambda s: s["ปิด"])["ภาคเรียน"]

        if is_hol:          day_type = "วันหยุดนักขัตฤกษ์"
        elif is_wknd:       day_type = "วันหยุด"
        elif not is_in_sem: day_type = "ปิดภาค"
        else:               day_type = "วันทำการ"

        row = {
            "date_actual":       current,
            "วันที่":             current.strftime("%d/%m/%Y"),
            "ปี_คศ":             current.year,
            "ปี_พศ":             be,
            "ปีการศึกษา":        academic_year,
            "ภาคเรียน":          row_sem,
            "เดือน":             THAI_MONTHS_FULL[current.month],
            "เดือน_ตัวเลข":      current.month,
            "เดือน_ย่อ":         THAI_MONTHS_SHORT[current.month],
            "วันที่_ตัวเลข":     current.day,
            "วัน":               THAI_DAYS[wday],
            "วัน_ตัวเลข":        wday + 1,
            "ไตรมาส":            f"Q{quarter}",
            "สัปดาห์ที่_ของภาค": week_counters[sem_num] if sem_num else None,
            "สถานะเทอม":         sem_label,
            "is_academic_day":   is_in_sem and not is_wknd and not is_hol,
            "is_weekend":        is_wknd,
            "is_holiday":        is_hol,
            "is_semester":       is_in_sem,
            "ชื่อวันหยุด":       hol_name,
            "ประเภทวัน":         day_type,
            "source":            hol_sources.get(current, "pipeline") if is_hol else "pipeline",
            "วิทยาเขต":          campus_name,
            "คณะ":               name if include_faculty else "Normal",
        }
        rows.append(row)

        if is_in_sem and wday == 6:
            week_counters[sem_num] += 1
        current += timedelta(days=1)

    return pd.DataFrame(rows)

def process_year(academic_year, sem_ranges):
    logger.info("Processing year %s...", academic_year)

    faculty_pdfs = get_faculty_pdfs(academic_year)
    special      = FACULTY_SPECIAL_MAP.get(academic_year, {})

    faculty_ranges = {"Normal": sem_ranges}
    for faculty, f in faculty_pdfs.items():
        if faculty in special:
            faculty_ranges[faculty] = special[faculty]
            logger.info("Added %s for year %s", faculty, academic_year)
        else:
            logger.info("No special data for %s year %s", faculty, academic_year)

    holidays, hol_src = load_holidays("วิทยาเขตหาดใหญ่", academic_year)
    logger.info("Holidays loaded: %d", len(holidays))

    all_dfs = []
    for faculty, ranges in faculty_ranges.items():
        df = build_dim(faculty, ranges, holidays, academic_year, include_faculty=True, hol_sources=hol_src)
        all_dfs.append(df)
        fname = FACULTY_NAME_MAP.get(faculty, faculty)
        out_f = f"{OUTPUT_DIR}/date_dim_{academic_year}_faculty_{fname}.xlsx"
        df.to_excel(out_f, index=False)
        logger.info("Done %s: %d rows", fname, len(df))
        save_to_db(df)

    df_all = pd.concat(all_dfs, ignore_index=True)
    out = f"{OUTPUT_DIR}/date_dimension_{academic_year}_by_faculty.xlsx"
    df_all.to_excel(out, index=False)
    logger.info("All rows year %s: %d", academic_year, len(df_all))
    save_to_db(df_all)

    for campus_key, sheet in CAMPUSES.items():
        hol_c, hol_src_c = load_holidays(sheet, academic_year)
        df_c  = build_dim(campus_key, faculty_ranges["Normal"], hol_c, academic_year, include_faculty=False, campus_name=campus_key, hol_sources=hol_src_c)
        out_c = f"{OUTPUT_DIR}/date_dim_{academic_year}_campus_{campus_key}.xlsx"
        df_c.to_excel(out_c, index=False)
        logger.info("Done campus %s: %d rows", campus_key, len(df_c))
        save_to_db(df_c)

    logger.info("Year %s completed", academic_year)

# ── Main: process ALL years ───────────────────────────────────────────────────
pdf_files = glob.glob(f"{INPUT_DIR}/PATITIN*.pdf")
if not pdf_files:
    logger.error("No PATITIN PDF found in %s", INPUT_DIR)
    raise SystemExit(0)

YEARS_DATA = {}

# 2566 dates are hardcoded because the source PDF for this year is image-based
YEARS_DATA[2566] = [
    {"ภาคเรียน": 1, "เปิด": date(2023,6,26),  "ปิด": date(2023,10,28)},
    {"ภาคเรียน": 2, "เปิด": date(2023,11,20), "ปิด": date(2024,3,23)},
    {"ภาคเรียน": 3, "เปิด": date(2024,4,17),  "ปิด": date(2024,6,8)},
]
logger.info("Hardcoded year 2566: 3 semesters")

for pdf_path in sorted(pdf_files):
    m = re_module.search(r'(\d{4})', Path(pdf_path).stem)
    if not m:
        continue
    yr = int(m.group(1))
    if yr in YEARS_DATA:
        logger.info("Year %s already loaded", yr)
        continue
    sem_ranges = parse_patitin_pdf(pdf_path)
    if sem_ranges:
        YEARS_DATA[yr] = sem_ranges
        logger.info("Parsed year %s: %d semesters", yr, len(sem_ranges))
    else:
        logger.warning("Skip year %s: could not parse", yr)

logger.info("Total years to process: %d", len(YEARS_DATA))

for yr, sem_ranges in sorted(YEARS_DATA.items()):
    process_year(yr, sem_ranges)

logger.info("All years completed successfully")
