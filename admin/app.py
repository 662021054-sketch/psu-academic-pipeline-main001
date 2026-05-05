import io
import json
import os
import re
import tempfile
import urllib.request
from datetime import date, timedelta
from typing import Optional
from pathlib import Path

import base64
import pdfplumber
import fitz  # PyMuPDF
from groq import Groq
import pandas as pd
import psycopg2
import streamlit as st
from PIL import Image
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

st.set_page_config(
    page_title="PSU Academic Pipeline",
    page_icon="📅",
    layout="wide",
)

# ── Config ────────────────────────────────────────────────────────────────────
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "psu_academic")
DB_USER = os.getenv("DB_USER", "admin")
DB_PASS = os.getenv("DB_PASSWORD", "psu2024")

INPUT_DIR = os.getenv("INPUT_DIR", "/app/input")

CAMPUS_OPTIONS  = ["HatYai", "Pattani", "Phuket", "Trang", "Suratthani"]
FACULTY_OPTIONS = ["ไม่มี (Normal)", "BBA", "Dent", "Med"]
HOL_TYPES = [
    "วันหยุดอิสลาม (ปัตตานี)",
    "วันหยุดเฉพาะวิทยาเขต",
    "วันงดการเรียนการสอน",
    "อื่นๆ (ไม่ใช่วันหยุดราชการ)",
]

# ── DB ────────────────────────────────────────────────────────────────────────
@st.cache_resource(ttl=0)
def get_engine():
    return create_engine(
        f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
        connect_args={"client_encoding": "utf8"},
    )

# ── Date extraction helpers ───────────────────────────────────────────────────
_THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_en_date(text: str) -> Optional[date]:
    for pat in (
        r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})',
        r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})',
    ):
        m = re.search(pat, text)
        if not m:
            continue
        g = m.groups()
        if g[0].isdigit():
            day, mon, yr = int(g[0]), g[1].lower(), int(g[2])
        else:
            mon, day, yr = g[0].lower(), int(g[1]), int(g[2])
        mnum = _EN_MONTHS.get(mon)
        if mnum:
            try:
                return date(yr, mnum, day)
            except ValueError:
                pass
    return None


def _parse_th_date(text: str) -> Optional[date]:
    m = re.search(r'(\d{1,2})\s+([ก-๿]{3,})\s+(\d{4})', text)
    if not m:
        return None
    mnum = _THAI_MONTHS.get(m.group(2))
    if not mnum:
        return None
    try:
        return date(int(m.group(3)) - 543, mnum, int(m.group(1)))
    except ValueError:
        return None


def _parse_en_range(text: str) -> tuple[Optional[date], Optional[date]]:
    m = re.search(r'([A-Za-z]+)\s+(\d{1,2})\s*[-–]\s*(\d{1,2}),?\s*(\d{4})', text)
    if m:
        mnum = _EN_MONTHS.get(m.group(1).lower())
        yr   = int(m.group(4))
        if mnum:
            try:
                return date(yr, mnum, int(m.group(2))), date(yr, mnum, int(m.group(3)))
            except ValueError:
                pass
    d = _parse_en_date(text)
    return (d, None)


def extract_dates(text: str) -> dict:
    OPEN_EN  = ["first day of classes", "beginning of semester",
                "1st semester begins", "first semester begins"]
    CLOSE_EN = ["last day of classes", "semester ends",
                "first semester ends", "second semester ends", "summer semester ends"]
    MID_EN   = ["mid-term examination", "midterm examination"]
    FIN_EN   = ["final examination"]
    OPEN_TH  = ["วันเปิดภาคการศึกษา", "วันเปิดภาค", "วันเปดภาค", "วันเปดภาคการศึกษา"]
    CLOSE_TH = ["วันปิดภาคการศึกษา", "วันสุดทายของการเรียน",
                "วันสุดท้ายของการเรียน", "วันสุดท้ายของภาคการศึกษา"]
    MID_TH   = ["วันสอบกลางภาค", "สอบกลางภาค"]
    FIN_TH   = ["วันสอบปลายภาค", "สอบปลายภาค"]

    r: dict = {
        "open": None, "close": None,
        "midterm_start": None, "midterm_end": None,
        "final_start": None,   "final_end": None,
    }

    for line in text.splitlines():
        ll = line.lower()

        if r["open"] is None:
            if any(k in ll for k in OPEN_EN):
                r["open"] = _parse_en_date(line)
            elif any(k in line for k in OPEN_TH):
                r["open"] = _parse_th_date(line)

        if r["close"] is None:
            if any(k in ll for k in CLOSE_EN):
                r["close"] = _parse_en_date(line)
            elif any(k in line for k in CLOSE_TH):
                r["close"] = _parse_th_date(line)

        if r["midterm_start"] is None:
            if any(k in ll for k in MID_EN):
                r["midterm_start"], r["midterm_end"] = _parse_en_range(line)
            elif any(k in line for k in MID_TH):
                r["midterm_start"] = _parse_th_date(line)

        if r["final_start"] is None:
            if any(k in ll for k in FIN_EN):
                r["final_start"], r["final_end"] = _parse_en_range(line)
            elif any(k in line for k in FIN_TH):
                r["final_start"] = _parse_th_date(line)

    return r


# ── Campus / year auto-detection (PDF text) ───────────────────────────────────
_CAMPUS_KEYWORDS = [
    ("วิทยาเขตหาดใหญ่",  "HatYai"),
    ("วิทยาเขตปัตตานี",  "Pattani"),
    ("วิทยาเขตภูเก็ต",   "Phuket"),
    ("phuket campus",    "Phuket"),
    ("วิทยาเขตตรัง",     "Trang"),
    ("วิทยาเขตสุราษฎร์", "Suratthani"),
]


def _detect_campus(text: str) -> Optional[str]:
    tl = text.lower()
    for kw, code in _CAMPUS_KEYWORDS:
        if kw.lower() in tl:
            return code
    return None


def _detect_year(text: str) -> Optional[int]:
    m = re.search(r'ปีการศึกษา\s+(25\d{2})', text)
    if m:
        return int(m.group(1))
    m = re.search(r'academic year\s+(\d{4})', text, re.IGNORECASE)
    if m:
        yr = int(m.group(1))
        return yr + 543 if yr < 2500 else yr
    return None


_GROQ_TEXT_MODEL   = "llama-3.3-70b-versatile"
_GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

_GROQ_PROMPT_ALL = """\
อ่านปฏิทินการศึกษามหาวิทยาลัยสงขลานครินทร์จากเอกสารนี้
ดึงข้อมูล**ทุกภาคการศึกษา**ที่มีในเอกสาร แล้วตอบเป็น JSON เท่านั้น (ห้ามมีข้อความอื่นนอกจาก JSON):

{
  "campus": "HatYai",
  "academic_year": 2568,
  "semesters": [
    {
      "semester": 1,
      "open":          "DD/MM/YYYY",
      "close":         "DD/MM/YYYY",
      "midterm_start": "DD/MM/YYYY",
      "midterm_end":   "DD/MM/YYYY",
      "final_start":   "DD/MM/YYYY",
      "final_end":     "DD/MM/YYYY"
    },
    {
      "semester": 2,
      "open":          "DD/MM/YYYY",
      "close":         "DD/MM/YYYY",
      "midterm_start": "DD/MM/YYYY",
      "midterm_end":   "DD/MM/YYYY",
      "final_start":   "DD/MM/YYYY",
      "final_end":     "DD/MM/YYYY"
    }
  ]
}

กฎ:
- campus        = HatYai, Pattani, Phuket, Trang หรือ Suratthani
- academic_year = ปีการศึกษา พ.ศ. (ถ้า ค.ศ. บวก 543)
- semester      = 1 (ต้นปี มิ.ย.–ต.ค.), 2 (ปลายปี พ.ย.–มี.ค.), 3 (ฤดูร้อน เม.ย.–พ.ค.)
- วันที่ทุกค่า = DD/MM/YYYY เป็น พ.ศ.  ถ้าไม่พบให้ใส่ null
- ใส่เฉพาะภาคที่มีข้อมูลในเอกสาร อย่าแต่งข้อมูลขึ้นมาเอง"""


def _get_groq_client() -> Groq:
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError("GROQ_API_KEY environment variable is not set")
    return Groq(api_key=api_key)


def _pdf_to_base64_images(raw_bytes: bytes, max_pages: int = 4) -> list:
    """แปลง PDF เป็น list ของ base64 PNG (สำหรับ vision model)"""
    doc = fitz.open(stream=raw_bytes, filetype="pdf")
    images = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    return images


def _groq_extract_dates(raw_bytes: bytes, ext: str, semester: int = 1) -> dict:
    client = _get_groq_client()
    prompt = _GROQ_PROMPT_ALL  # ดึงทุกภาคเรียนในครั้งเดียว

    def _parse_json(raw: str) -> dict:
        raw = re.sub(r'^```[a-z]*\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\s*```$', '', raw).strip()
        return json.loads(raw)

    def _p(s) -> Optional[date]:
        if not s:
            return None
        try:
            d, m, y = map(int, str(s).strip().split("/"))
            return date(y - 543, m, d)
        except Exception:
            return None

    # ── PDF: ลอง pdfplumber ก่อน ถ้าไม่มีข้อความค่อยใช้ vision ──
    if ext == ".pdf":
        text = ""
        try:
            import io as _io
            with pdfplumber.open(_io.BytesIO(raw_bytes)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception:
            pass

        if text.strip():
            resp = client.chat.completions.create(
                model=_GROQ_TEXT_MODEL,
                messages=[{"role": "user", "content": prompt + "\n\nข้อความจากเอกสาร:\n" + text[:8000]}],
                temperature=0.0,
            )
            data = _parse_json(resp.choices[0].message.content.strip())
        else:
            # PDF สแกน → แปลงเป็นรูปแล้วส่ง vision
            images_b64 = _pdf_to_base64_images(raw_bytes)
            content = [{"type": "text", "text": prompt}]
            for b64 in images_b64:
                content.append({"type": "image_url",
                                 "image_url": {"url": f"data:image/png;base64,{b64}"}})
            resp = client.chat.completions.create(
                model=_GROQ_VISION_MODEL,
                messages=[{"role": "user", "content": content}],
                temperature=0.0,
            )
            data = _parse_json(resp.choices[0].message.content.strip())

    # ── รูปภาพ: ส่ง vision โดยตรง ──
    else:
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png"}.get(ext.lstrip("."), "image/png")
        b64  = base64.b64encode(raw_bytes).decode()
        resp = client.chat.completions.create(
            model=_GROQ_VISION_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]}],
            temperature=0.0,
        )
        data = _parse_json(resp.choices[0].message.content.strip())

    semesters = []
    for s in data.get("semesters", []):
        semesters.append({
            "semester":      int(s.get("semester", 1)),
            "open":          _p(s.get("open")),
            "close":         _p(s.get("close")),
            "midterm_start": _p(s.get("midterm_start")),
            "midterm_end":   _p(s.get("midterm_end")),
            "final_start":   _p(s.get("final_start")),
            "final_end":     _p(s.get("final_end")),
        })
    # fallback: ถ้า AI ส่งแบบ flat (ไม่มี semesters key)
    if not semesters and data.get("open"):
        semesters.append({
            "semester":      1,
            "open":          _p(data.get("open")),
            "close":         _p(data.get("close")),
            "midterm_start": _p(data.get("midterm_start")),
            "midterm_end":   _p(data.get("midterm_end")),
            "final_start":   _p(data.get("final_start")),
            "final_end":     _p(data.get("final_end")),
        })
    return {
        "campus":        data.get("campus"),
        "academic_year": data.get("academic_year"),
        "semesters":     semesters,
    }


def _gen_filename(campus: str, faculty: str, year_be: int) -> str:
    fac = None if faculty == "ไม่มี (Normal)" else faculty
    if fac:
        return f"{year_be}_{fac.upper()}_Calendar.pdf"
    if campus == "HatYai":
        return f"PATITIN{year_be}.pdf"
    return f"{year_be}_{campus}_Calendar.pdf"


def _to_pdf_bytes(raw_bytes: bytes, ext: str) -> bytes:
    if ext == ".pdf":
        return raw_bytes
    img = Image.open(io.BytesIO(raw_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def _read_file(raw_bytes: bytes, ext: str) -> str:
    """Extract text from PDF/image using pdfplumber or Groq Vision."""
    if ext == ".pdf":
        try:
            import io as _io
            with pdfplumber.open(_io.BytesIO(raw_bytes)) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            if text.strip():
                return text
        except Exception:
            pass
    # ภาพหรือ PDF ที่ไม่มี text layer → ใช้ Groq Vision
    try:
        client = _get_groq_client()
        if ext == ".pdf":
            images_b64 = _pdf_to_base64_images(raw_bytes, max_pages=2)
            content = [{"type": "text", "text": "Extract all text from this document. Return only the raw text content."}]
            for b64 in images_b64:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        else:
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext.lstrip("."), "image/png")
            b64  = base64.b64encode(raw_bytes).decode()
            content = [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": "Extract all text from this document. Return only the raw text content."},
            ]
        resp = client.chat.completions.create(
            model=_GROQ_VISION_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0.0,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        return f"[Groq error: {exc}]"


# ── Thai locale constants (for dim_date population) ───────────────────────────
_THAI_MONTHS_FULL = {
    1:"มกราคม",2:"กุมภาพันธ์",3:"มีนาคม",4:"เมษายน",
    5:"พฤษภาคม",6:"มิถุนายน",7:"กรกฎาคม",8:"สิงหาคม",
    9:"กันยายน",10:"ตุลาคม",11:"พฤศจิกายน",12:"ธันวาคม",
}
_THAI_MONTHS_SHORT = {
    1:"ม.ค.",2:"ก.พ.",3:"มี.ค.",4:"เม.ย.",
    5:"พ.ค.",6:"มิ.ย.",7:"ก.ค.",8:"ส.ค.",
    9:"ก.ย.",10:"ต.ค.",11:"พ.ย.",12:"ธ.ค.",
}
_THAI_DAYS_MAP = {
    0:"จันทร์",1:"อังคาร",2:"พุธ",
    3:"พฤหัสบดี",4:"ศุกร์",5:"เสาร์",6:"อาทิตย์",
}
_CAMPUSES_TH = {
    "HatYai":     "วิทยาเขตหาดใหญ่",
    "Pattani":    "วิทยาเขตปัตตานี",
    "Phuket":     "วิทยาเขตภูเก็ต",
    "Trang":      "วิทยาเขตตรัง",
    "Suratthani": "วิทยาเขตสุราษฎร์ฯ",
}

# ── DB helpers (direct psycopg2 for fact inserts) ─────────────────────────────

def _db_conn():
    conn = psycopg2.connect(
        host=DB_HOST, port=int(DB_PORT), dbname=DB_NAME,
        user=DB_USER, password=DB_PASS, client_encoding="utf8",
    )
    # ── Auto-migrate: เพิ่ม column / constraint ที่อาจขาดหายจาก schema เก่า ──
    try:
        with conn.cursor() as _cur:
            # 1) เพิ่ม column source ถ้ายังไม่มี
            _cur.execute("""
                ALTER TABLE fact_academic_calendar
                    ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual'
            """)
            # 2) เพิ่ม UNIQUE constraint ที่ ON CONFLICT ต้องการ (ถ้ายังไม่มี)
            _cur.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'fact_academic_calendar'::regclass
                          AND contype  = 'u'
                          AND conname  = 'uq_fact_cal_date_campus_faculty_daytype'
                    ) THEN
                        ALTER TABLE fact_academic_calendar
                            ADD CONSTRAINT uq_fact_cal_date_campus_faculty_daytype
                            UNIQUE (date_id, campus_id, faculty_id);
                    END IF;
                END$$;
            """)
        conn.commit()
    except Exception:
        conn.rollback()
    return conn


def _upsert_dim_date(cur, d: date) -> int:
    be   = d.year + 543
    wday = d.weekday()
    q    = (d.month - 1) // 3 + 1
    cur.execute("""
        INSERT INTO dim_date
            (date_actual,date_str,year_ce,year_be,month_num,month_name,
             month_short,day_num,day_name,day_of_week,quarter,is_weekend)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (date_actual) DO UPDATE SET date_str=EXCLUDED.date_str
        RETURNING id
    """, (d, d.strftime("%d/%m/%Y"), d.year, be, d.month,
          _THAI_MONTHS_FULL[d.month], _THAI_MONTHS_SHORT[d.month],
          d.day, _THAI_DAYS_MAP[wday], wday + 1, f"Q{q}", wday >= 5))
    return cur.fetchone()[0]


def _upsert_campus(cur, code: str) -> int:
    cur.execute("""
        INSERT INTO dim_campus (campus_code, campus_name_th) VALUES (%s,%s)
        ON CONFLICT (campus_code) DO UPDATE SET campus_name_th=EXCLUDED.campus_name_th
        RETURNING id
    """, (code, _CAMPUSES_TH.get(code, code)))
    return cur.fetchone()[0]


def _upsert_faculty(cur, code: str) -> int:
    cur.execute("""
        INSERT INTO dim_faculty (faculty_code, faculty_name_th) VALUES (%s,%s)
        ON CONFLICT (faculty_code) DO UPDATE SET faculty_name_th=EXCLUDED.faculty_name_th
        RETURNING id
    """, (code, code))
    return cur.fetchone()[0]


def _fetch_public_holidays(year_be: int) -> dict[date, str]:
    """ดึงวันหยุดราชการไทยจาก Google Calendar iCal สำหรับปีการศึกษา BE.
    ครอบคลุม 2 ปี CE ที่ปีการศึกษานั้นพาด (เช่น 2568 → 2025-2026)
    Returns {} เงียบๆ ถ้าเชื่อมต่อไม่ได้"""
    import urllib.request
    from icalendar import Calendar as iCal

    _ICAL_URL = (
        "https://calendar.google.com/calendar/ical/"
        "th.th%23holiday%40group.v.calendar.google.com/public/basic.ics"
    )
    ce_years = {year_be - 543, year_be - 543 + 1}
    try:
        with urllib.request.urlopen(_ICAL_URL, timeout=10) as r:
            cal = iCal.from_ical(r.read())
        hol: dict[date, str] = {}
        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue
            dtstart = comp.get("DTSTART")
            if dtstart is None:
                continue
            ev_date = dtstart.dt
            if hasattr(ev_date, "date"):
                ev_date = ev_date.date()
            if ev_date.year not in ce_years:
                continue
            summary = str(comp.get("SUMMARY", "")).strip()
            if summary:
                hol[ev_date] = summary
        return hol
    except Exception:
        return {}


def _insert_semester_to_db(
    campus: str,
    faculty_label: str,
    year_be: int,
    semester: int,
    open_date: date,
    close_date: date,
    source: str = "manual",
) -> int:
    """Build daily rows for [open_date, close_date] and insert into fact_academic_calendar.
    โหลดวันหยุดราชการจาก Google Calendar ก่อน แล้วใช้ตั้ง is_holiday / is_academic_day / source"""
    faculty_code = "Normal" if faculty_label == "ไม่มี (Normal)" else faculty_label

    # โหลด public holidays ก่อน insert เสมอ
    hol_map = _fetch_public_holidays(year_be)

    conn = _db_conn()
    try:
        cur        = conn.cursor()
        campus_id  = _upsert_campus(cur, campus)
        faculty_id = _upsert_faculty(cur, faculty_code)

        rows     = []
        week_num = 1
        current  = open_date
        while current <= close_date:
            date_id  = _upsert_dim_date(cur, current)
            is_wknd  = current.weekday() >= 5
            is_hol   = current in hol_map
            hol_name = hol_map.get(current)

            if is_hol:       day_type    = "วันหยุดนักขัตฤกษ์"
            elif is_wknd:    day_type    = "วันหยุด"
            else:            day_type    = "วันทำการ"

            row_source = "google_calendar" if is_hol else source

            rows.append((
                date_id, campus_id, faculty_id, year_be, semester,
                week_num,
                not is_wknd and not is_hol,   # is_academic_day
                is_hol, hol_name, day_type, row_source,
            ))
            if current.weekday() == 6:
                week_num += 1
            current += timedelta(days=1)

        execute_values(cur, """
            INSERT INTO fact_academic_calendar
                (date_id,campus_id,faculty_id,academic_year,semester,
                 week_of_semester,is_academic_day,is_holiday,holiday_name,day_type,source)
            VALUES %s
            ON CONFLICT (date_id,campus_id,faculty_id) DO UPDATE SET
                academic_year    = EXCLUDED.academic_year,
                semester         = EXCLUDED.semester,
                week_of_semester = EXCLUDED.week_of_semester,
                is_academic_day  = EXCLUDED.is_academic_day,
                is_holiday       = EXCLUDED.is_holiday,
                holiday_name     = EXCLUDED.holiday_name,
                day_type         = EXCLUDED.day_type,
                source           = EXCLUDED.source
        """, rows)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _insert_semester_full(
    campus: str,
    faculty_label: str,
    year_be: int,
    semester: int,
    open_date: date,
    close_date: date,
    midterm_start: Optional[date],
    midterm_end: Optional[date],
    final_start: Optional[date],
    final_end: Optional[date],
    extra_holidays: list,
) -> int:
    faculty_code = "Normal" if faculty_label == "ไม่มี (Normal)" else faculty_label
    hol_map: dict = {}
    for hdate, hname in extra_holidays:
        if isinstance(hdate, date):
            hol_map[hdate] = hname
    exam_dates: set = set()
    for ds, de in ((midterm_start, midterm_end), (final_start, final_end)):
        if ds:
            cur_d = ds
            end_d = de or ds
            while cur_d <= end_d:
                exam_dates.add(cur_d)
                cur_d += timedelta(days=1)
    conn = _db_conn()
    try:
        cur = conn.cursor()
        campus_id  = _upsert_campus(cur, campus)
        faculty_id = _upsert_faculty(cur, faculty_code)
        rows = []
        week_num = 1
        current = open_date
        while current <= close_date:
            date_id  = _upsert_dim_date(cur, current)
            is_wknd  = current.weekday() >= 5
            is_hol   = current in hol_map
            is_exam  = current in exam_dates
            hol_name = hol_map.get(current)
            if is_hol:       day_type = "วันหยุดนักขัตฤกษ์"
            elif is_wknd:    day_type = "วันหยุด"
            elif is_exam:    day_type = "วันสอบ"
            else:            day_type = "วันทำการ"
            rows.append((
                date_id, campus_id, faculty_id, year_be, semester,
                week_num, not is_wknd and not is_hol, is_hol, hol_name, day_type, "manual",
            ))
            if current.weekday() == 6:
                week_num += 1
            current += timedelta(days=1)
        execute_values(cur, """
            INSERT INTO fact_academic_calendar
                (date_id,campus_id,faculty_id,academic_year,semester,
                 week_of_semester,is_academic_day,is_holiday,holiday_name,day_type,source)
            VALUES %s
            ON CONFLICT (date_id,campus_id,faculty_id) DO UPDATE SET
                academic_year    = EXCLUDED.academic_year,
                semester         = EXCLUDED.semester,
                week_of_semester = EXCLUDED.week_of_semester,
                is_academic_day  = EXCLUDED.is_academic_day,
                is_holiday       = EXCLUDED.is_holiday,
                holiday_name     = EXCLUDED.holiday_name,
                day_type         = EXCLUDED.day_type,
                source           = EXCLUDED.source
        """, rows)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validate_calendar(
    campus: str,
    faculty_label: str,
    year_be: int,
    semester: int,
    open_date: Optional[date],
    close_date: Optional[date],
    midterm_start: Optional[date],
    midterm_end: Optional[date],
    final_start: Optional[date],
    final_end: Optional[date],
) -> list:
    errors = []
    if not open_date:
        errors.append("กรุณาระบุวันเปิดภาค")
    if not close_date:
        errors.append("กรุณาระบุวันปิดภาค")
    if open_date and close_date and open_date >= close_date:
        errors.append("วันเปิดภาคต้องน้อยกว่าวันปิดภาค")
    if midterm_start and midterm_end and midterm_start > midterm_end:
        errors.append("วันเริ่มสอบกลางภาคต้องไม่เกินวันสิ้นสุด")
    if final_start and final_end and final_start > final_end:
        errors.append("วันเริ่มสอบปลายภาคต้องไม่เกินวันสิ้นสุด")
    if open_date and close_date:
        if midterm_start and not (open_date <= midterm_start <= close_date):
            errors.append("วันสอบกลางภาคต้องอยู่ภายในช่วงภาคเรียน")
        if final_start and not (open_date <= final_start <= close_date):
            errors.append("วันสอบปลายภาคต้องอยู่ภายในช่วงภาคเรียน")
    return errors


def _insert_holidays_to_db(campuses: list, year_be: int, semester: int, holidays: list) -> int:
    """INSERT holiday rows with source='manual'. holidays: list of (date, name, day_type)."""
    conn = _db_conn()
    total = 0
    try:
        cur = conn.cursor()
        for campus in campuses:
            campus_id = _upsert_campus(cur, campus)
            cur.execute("""
                SELECT DISTINCT faculty_id FROM fact_academic_calendar
                WHERE campus_id = %s AND academic_year = %s AND semester = %s
            """, (campus_id, year_be, semester))
            faculty_ids = [r[0] for r in cur.fetchall()]
            if not faculty_ids:
                faculty_ids = [_upsert_faculty(cur, "Normal")]
            for item in holidays:
                hdate = item[0] if len(item) > 0 else None
                hname = item[1] if len(item) > 1 else ""
                htype = item[2] if len(item) > 2 else "วันหยุดพิเศษ"
                if not isinstance(hdate, date):
                    continue
                date_id = _upsert_dim_date(cur, hdate)
                for fid in faculty_ids:
                    cur.execute("""
                        INSERT INTO fact_academic_calendar
                            (date_id, campus_id, faculty_id, academic_year, semester,
                             week_of_semester, is_academic_day, is_holiday, holiday_name,
                             day_type, source)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (date_id, campus_id, faculty_id) DO UPDATE SET
                            is_holiday   = true,
                            holiday_name = EXCLUDED.holiday_name,
                            source       = EXCLUDED.source
                    """, (date_id, campus_id, fid, year_be, semester,
                          None, False, True, hname, htype, "manual"))
                    total += 1
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validate_holidays(campuses: list, year_be: int, holidays: list) -> list:
    errors = []
    if not campuses:
        errors.append("กรุณาเลือกวิทยาเขตอย่างน้อย 1 แห่ง")
    if not holidays:
        errors.append("กรุณาเพิ่มวันหยุดอย่างน้อย 1 วัน")
    for i, item in enumerate(holidays):
        hdate = item[0] if len(item) > 0 else None
        hname = item[1] if len(item) > 1 else ""
        if not hdate:
            errors.append(f"รายการที่ {i+1}: กรุณาระบุวันที่")
        if not hname or not str(hname).strip():
            errors.append(f"รายการที่ {i+1}: กรุณาระบุชื่อวันหยุด")
    return errors


def _validate_before_insert(
    campus: str,
    faculty_code: str,
    year_be: int,
    semester: int,
    open_date: Optional[date],
    close_date: Optional[date],
    new_holidays: list = None,
    new_exam_dates: set = None,
) -> list:
    """Check DB for conflicts before inserting. Returns list of {"type", "detail"} dicts."""
    conflicts = []
    if not open_date or not close_date:
        return conflicts
    try:
        conn = _db_conn()
        cur  = conn.cursor()

        cur.execute("SELECT id FROM dim_campus WHERE campus_code = %s", (campus,))
        r = cur.fetchone()
        if not r:
            conn.close()
            return conflicts
        campus_id = r[0]

        cur.execute("SELECT id FROM dim_faculty WHERE faculty_code = %s", (faculty_code,))
        r = cur.fetchone()
        if not r:
            conn.close()
            return conflicts
        faculty_id = r[0]

        # 1. Duplicate rows overlapping the same semester range
        cur.execute("""
            SELECT COUNT(*) FROM fact_academic_calendar f
            JOIN dim_date d ON f.date_id = d.id
            WHERE f.campus_id = %s AND f.faculty_id = %s
              AND f.academic_year = %s AND f.semester = %s
              AND d.date_actual BETWEEN %s AND %s
        """, (campus_id, faculty_id, year_be, semester, open_date, close_date))
        dup = cur.fetchone()[0]
        if dup:
            conflicts.append({
                "type": "duplicate",
                "detail": (
                    f"มีข้อมูลซ้ำ: {campus} เทอม {semester} ปี {year_be} "
                    f"พบ {dup:,} แถว ในช่วง {open_date} – {close_date}"
                ),
            })

        # 2. Open date on an existing holiday
        cur.execute("""
            SELECT f.holiday_name FROM fact_academic_calendar f
            JOIN dim_date d ON f.date_id = d.id
            WHERE f.campus_id = %s AND f.faculty_id = %s
              AND d.date_actual = %s AND f.is_holiday = true
            LIMIT 1
        """, (campus_id, faculty_id, open_date))
        r = cur.fetchone()
        if r:
            conflicts.append({
                "type": "open_on_holiday",
                "detail": f"วันเปิดภาค {open_date} ตรงกับวันหยุด: {r[0]}",
            })

        # 3. New holidays clash with existing exam days
        for item in (new_holidays or []):
            hd = item[0] if isinstance(item, (tuple, list)) else item
            if not isinstance(hd, date):
                continue
            cur.execute("""
                SELECT 1 FROM fact_academic_calendar f
                JOIN dim_date d ON f.date_id = d.id
                WHERE f.campus_id = %s AND f.faculty_id = %s
                  AND d.date_actual = %s AND f.day_type = 'วันสอบ'
                LIMIT 1
            """, (campus_id, faculty_id, hd))
            if cur.fetchone():
                hn = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else ""
                conflicts.append({
                    "type": "holiday_on_exam",
                    "detail": f"วันหยุด {hd} ({hn}) ทับวันสอบที่มีอยู่ใน DB",
                })

        # 4. New exam dates clash with existing holidays
        for ed in sorted(new_exam_dates or set()):
            cur.execute("""
                SELECT f.holiday_name FROM fact_academic_calendar f
                JOIN dim_date d ON f.date_id = d.id
                WHERE f.campus_id = %s AND f.faculty_id = %s
                  AND d.date_actual = %s AND f.is_holiday = true
                LIMIT 1
            """, (campus_id, faculty_id, ed))
            r = cur.fetchone()
            if r:
                conflicts.append({
                    "type": "exam_on_holiday",
                    "detail": f"วันสอบ {ed} ทับวันหยุด: {r[0]}",
                })

        # 5. วันหยุดซ้ำกันในช่วงวันที่เดียวกัน
        cur.execute("""
            SELECT d.date_actual,
                   string_agg(DISTINCT COALESCE(f.holiday_name, f.day_type), ' / ')
            FROM fact_academic_calendar f
            JOIN dim_date d ON f.date_id = d.id
            WHERE f.campus_id = %s AND f.faculty_id = %s
              AND f.is_holiday = true
              AND d.date_actual BETWEEN %s AND %s
            GROUP BY d.date_actual
            HAVING COUNT(*) > 1
            LIMIT 10
        """, (campus_id, faculty_id, open_date, close_date))
        for r in cur.fetchall():
            conflicts.append({
                "type": "holiday_overlap",
                "detail": f"วันที่ {r[0]} มีวันหยุดซ้ำซ้อน: {r[1]}",
            })

        conn.close()
    except Exception:
        pass
    return conflicts


def _validate_holidays_before_insert(
    campuses: list,
    year_be: int,
    semester: int,
    holidays: list,
) -> list:
    """Background DB conflict check specifically for holiday inserts."""
    conflicts = []
    hol_dates = [
        (item[0], item[1] if len(item) > 1 else "")
        for item in holidays
        if isinstance(item[0], date)
    ]
    if not hol_dates or not campuses:
        return conflicts
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        for campus in campuses:
            cur.execute("SELECT id FROM dim_campus WHERE campus_code = %s", (campus,))
            r = cur.fetchone()
            if not r:
                continue
            campus_id = r[0]
            cur.execute("""
                SELECT DISTINCT faculty_id FROM fact_academic_calendar
                WHERE campus_id = %s AND academic_year = %s AND semester = %s
            """, (campus_id, year_be, semester))
            faculty_ids = [row[0] for row in cur.fetchall()]
            if not faculty_ids:
                continue
            fid = faculty_ids[0]
            for hd, hn in hol_dates:
                cur.execute("""
                    SELECT 1 FROM fact_academic_calendar f
                    JOIN dim_date d ON f.date_id = d.id
                    WHERE f.campus_id = %s AND f.faculty_id = %s
                      AND d.date_actual = %s AND f.day_type = 'วันสอบ'
                    LIMIT 1
                """, (campus_id, fid, hd))
                if cur.fetchone():
                    conflicts.append({
                        "type": "holiday_on_exam",
                        "detail": f"วันหยุด {hd} ({hn}) ทับวันสอบใน {campus}",
                    })
                cur.execute("""
                    SELECT f.holiday_name FROM fact_academic_calendar f
                    JOIN dim_date d ON f.date_id = d.id
                    WHERE f.campus_id = %s AND f.faculty_id = %s
                      AND d.date_actual = %s AND f.is_holiday = true
                    LIMIT 1
                """, (campus_id, fid, hd))
                r = cur.fetchone()
                if r:
                    conflicts.append({
                        "type": "holiday_overlap",
                        "detail": f"วันที่ {hd} ({hn}) ซ้ำกับวันหยุดราชการ '{r[0]}' ใน {campus}",
                    })
        conn.close()
    except Exception:
        pass
    return conflicts


def _show_conflict_ui(
    conflicts: list,
    confirm_key: str,
    cancel_key: str,
    confirmed_flag: str,
    conflict_flag: str,
):
    """Render inline conflict warning with confirm / cancel buttons."""
    st.divider()
    st.warning("⚠️ พบข้อขัดแย้งในข้อมูล — กรุณาตรวจสอบก่อนบันทึก")
    for _c in conflicts:
        st.error(f"• {_c['detail']}")
    _cv1, _cv2 = st.columns(2)
    with _cv1:
        if st.button("⚠️ ยืนยันบันทึกต่อ (ทับข้อมูลเดิม)", type="secondary", key=confirm_key):
            st.session_state[confirmed_flag] = True
            st.session_state[conflict_flag]  = None
            st.rerun()
    with _cv2:
        if st.button("❌ ยกเลิก และแก้ไขข้อมูล", key=cancel_key):
            st.session_state[conflict_flag] = None
            st.rerun()


# ── Data Management helpers ───────────────────────────────────────────────────

def _count_and_fetch_for_delete(
    year_be: int,
    campus_code: Optional[str],
    semester: Optional[int],
) -> tuple[int, pd.DataFrame]:
    """Return (count, DataFrame) of rows matching delete filters."""
    engine = get_engine()
    where = "WHERE f.academic_year = :year"
    params: dict = {"year": year_be}
    if campus_code:
        where += " AND dc.campus_code = :campus"
        params["campus"] = campus_code
    if semester is not None:
        where += " AND f.semester = :sem"
        params["sem"] = semester
    df = pd.read_sql(text(f"""
        SELECT
            dd.date_actual  AS วันที่,
            dc.campus_code  AS วิทยาเขต,
            df.faculty_code AS คณะ,
            f.academic_year AS ปีการศึกษา,
            f.semester      AS ภาคเรียน,
            f.is_academic_day,
            f.is_holiday,
            f.holiday_name  AS ชื่อวันหยุด,
            f.day_type      AS ประเภทวัน,
            f.source        AS แหล่งข้อมูล
        FROM fact_academic_calendar f
        JOIN dim_date    dd ON f.date_id    = dd.id
        JOIN dim_campus  dc ON f.campus_id  = dc.id
        JOIN dim_faculty df ON f.faculty_id = df.id
        {where}
        ORDER BY dd.date_actual, dc.campus_code, df.faculty_code
    """), engine, params=params)
    return len(df), df


def _execute_delete(
    year_be: int,
    campus_code: Optional[str],
    semester: Optional[int],
) -> int:
    """Delete matching rows from fact_academic_calendar. Returns rows deleted."""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        campus_id: Optional[int] = None
        if campus_code:
            cur.execute(
                "SELECT id FROM dim_campus WHERE campus_code = %s", (campus_code,)
            )
            r = cur.fetchone()
            campus_id = r[0] if r else None

        conditions = ["academic_year = %s"]
        params: list = [year_be]
        if campus_id is not None:
            conditions.append("campus_id = %s")
            params.append(campus_id)
        if semester is not None:
            conditions.append("semester = %s")
            params.append(semester)

        cur.execute(
            f"DELETE FROM fact_academic_calendar WHERE {' AND '.join(conditions)}",
            params,
        )
        deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Edit helpers ─────────────────────────────────────────────────────────────

def _fetch_for_edit(
    year_be: int,
    campus_code: Optional[str],
    semester: Optional[int],
) -> pd.DataFrame:
    """Fetch rows for inline editing (max 1000). Returns DataFrame with _fact_id column."""
    engine = get_engine()
    where  = "WHERE f.academic_year = :year"
    params: dict = {"year": year_be}
    if campus_code:
        where += " AND dc.campus_code = :campus"
        params["campus"] = campus_code
    if semester is not None:
        where += " AND f.semester = :sem"
        params["sem"] = semester
    return pd.read_sql(text(f"""
        SELECT
            f.id            AS _fact_id,
            dd.date_actual  AS วันที่,
            dc.campus_code  AS วิทยาเขต,
            df.faculty_code AS คณะ,
            f.academic_year AS ปีการศึกษา,
            f.semester      AS ภาคเรียน,
            f.is_academic_day,
            f.is_holiday,
            f.holiday_name  AS ชื่อวันหยุด,
            f.day_type      AS ประเภทวัน,
            f.source        AS แหล่งข้อมูล
        FROM fact_academic_calendar f
        JOIN dim_date    dd ON f.date_id    = dd.id
        JOIN dim_campus  dc ON f.campus_id  = dc.id
        JOIN dim_faculty df ON f.faculty_id = df.id
        {where}
        ORDER BY dd.date_actual, dc.campus_code, df.faculty_code
        LIMIT 1000
    """), engine, params=params)


def _execute_update_rows(changed_rows: pd.DataFrame) -> int:
    """UPDATE changed rows in fact_academic_calendar by id. Returns rows updated."""
    if changed_rows.empty:
        return 0
    conn = _db_conn()
    updated = 0
    try:
        cur = conn.cursor()
        for _, row in changed_rows.iterrows():
            fact_id  = int(row["_fact_id"])
            sem_val  = int(row["ภาคเรียน"]) if pd.notna(row["ภาคเรียน"]) else None
            hol_raw  = str(row["ชื่อวันหยุด"]).strip() if pd.notna(row["ชื่อวันหยุด"]) else ""
            hol_name = hol_raw if hol_raw not in ("", "None", "nan") else None
            cur.execute("""
                UPDATE fact_academic_calendar
                SET
                    semester        = %s,
                    is_academic_day = %s,
                    is_holiday      = %s,
                    holiday_name    = %s,
                    day_type        = %s,
                    source          = 'manual'
                WHERE id = %s
            """, (
                sem_val,
                bool(row["is_academic_day"]),
                bool(row["is_holiday"]),
                hol_name,
                str(row["ประเภทวัน"]),
                fact_id,
            ))
            updated += cur.rowcount
        conn.commit()
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Session state defaults ────────────────────────────────────────────────────
# Transfer _pending_year into up_year BEFORE widgets render
if "_pending_year" in st.session_state:
    st.session_state["up_year"] = st.session_state.pop("_pending_year")
if "_pending_campus" in st.session_state:
    st.session_state.pop("_pending_campus")  # discard — campus is NOT auto-set

for _k, _v in {
    "export_df": None, "export_name": "",
    "up_key": "",   "up_bytes": b"",
    "up_fname": "", "up_ext": "",
    "up_text": "",  "up_parsed": None,
    "up_saved": False,
    "up_auto_campus": None,
    "up_auto_year": None,
    "up_parse_msg": None,
    "up_campus": "HatYai", "up_faculty": "ไม่มี (Normal)",
    "up_year": 2568,
    "up_conflicts": None, "up_conflict_confirmed": False,
    "mc_holidays": [], "mc_validated": None, "mc_saved": False,
    "mc_conflicts": None, "mc_conflict_confirmed": False,
    "hol_list": [], "hol_validated": None, "hol_saved": False,
    "hol_conflicts": None, "hol_conflict_confirmed": False,
    "mgmt_preview": None, "mgmt_backup": None,
    "mgmt_confirmed": False, "mgmt_history": [],
    "edit_df_original": None, "edit_history": [],
}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── Title & tabs ──────────────────────────────────────────────────────────────
st.title("📅 PSU Academic Calendar Pipeline")
st.caption("ระบบปฏิทินการศึกษา มหาวิทยาลัยสงขลานครินทร์ · Star Schema")

tab_dash, tab_upload, tab_manual, tab_holiday, tab_mgmt = st.tabs([
    "📊 Dashboard", "📤 อัปโหลดปฏิทิน", "✏️ กรอกปฏิทิน", "🗓️ เพิ่มวันหยุด", "⚙️ จัดการข้อมูล"
])

# ══════════════════════════════ DASHBOARD ════════════════════════════════════
with tab_dash:
    try:
        engine = get_engine()

        # ── Summary metrics ───────────────────────────────────────────────────
        col1, col2, col3, col4 = st.columns(4)

        with col1:
            n = pd.read_sql("SELECT COUNT(*) AS n FROM dim_date", engine).iloc[0, 0]
            st.metric("วันที่ในระบบ (unique)", f"{n:,}")

        with col2:
            n_year = pd.read_sql(
                "SELECT COUNT(DISTINCT academic_year) AS n FROM fact_academic_calendar",
                engine,
            ).iloc[0, 0]
            st.metric("ปีการศึกษา", int(n_year) if n_year else 0)

        with col3:
            n_campus = pd.read_sql(
                "SELECT COUNT(*) AS n FROM dim_campus", engine
            ).iloc[0, 0]
            st.metric("วิทยาเขต", int(n_campus))

        with col4:
            n_holiday = pd.read_sql(
                """SELECT COUNT(DISTINCT f.date_id) AS n
                   FROM fact_academic_calendar f
                   JOIN dim_faculty df ON f.faculty_id = df.id
                   WHERE f.is_holiday = true
                     AND df.faculty_code = 'Normal'""",
                engine,
            ).iloc[0, 0]
            st.metric("วันหยุดนักขัตฤกษ์ (unique)", f"{n_holiday:,}")

        st.divider()

        # ── Campus breakdown ──────────────────────────────────────────────────
        st.subheader("📊 สถิติแยกตามวิทยาเขต (คณะ Normal)")
        campus_stats = pd.read_sql(
            text("""
                SELECT
                    dc.campus_name_th                                                   AS วิทยาเขต,
                    COUNT(DISTINCT f.date_id)                                           AS วันทั้งหมด,
                    COUNT(DISTINCT CASE WHEN f.is_academic_day THEN f.date_id END)     AS วันทำการ,
                    COUNT(DISTINCT CASE WHEN f.is_holiday      THEN f.date_id END)     AS วันหยุดนักขัตฤกษ์,
                    COUNT(DISTINCT CASE WHEN dd.is_weekend AND NOT f.is_holiday
                                        THEN f.date_id END)                            AS วันหยุดสุดสัปดาห์
                FROM fact_academic_calendar f
                JOIN dim_date    dd ON f.date_id    = dd.id
                JOIN dim_campus  dc ON f.campus_id  = dc.id
                JOIN dim_faculty df ON f.faculty_id = df.id
                WHERE df.faculty_code = 'Normal'
                GROUP BY dc.campus_name_th
                ORDER BY dc.campus_name_th
            """),
            engine,
        )
        st.dataframe(campus_stats, use_container_width=True, hide_index=True)

        st.divider()

        # ── Data export ───────────────────────────────────────────────────────
        st.subheader("📥 ดาวน์โหลดข้อมูล")

        year_list_df = pd.read_sql(
            "SELECT DISTINCT academic_year FROM fact_academic_calendar"
            " ORDER BY academic_year DESC",
            engine,
        )
        year_list = year_list_df["academic_year"].tolist()

        if not year_list:
            st.info("ยังไม่มีข้อมูลในระบบ")
        else:
            campus_df = pd.read_sql(
                "SELECT campus_code, campus_name_th FROM dim_campus ORDER BY campus_code",
                engine,
            )
            campus_options = {"ทั้งหมด": None} | dict(
                zip(campus_df["campus_name_th"], campus_df["campus_code"])
            )

            faculty_df = pd.read_sql(
                "SELECT faculty_code, faculty_name_th FROM dim_faculty ORDER BY faculty_code",
                engine,
            )
            faculty_options = {"ทั้งหมด": None} | dict(
                zip(faculty_df["faculty_name_th"], faculty_df["faculty_code"])
            )

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                selected_year = st.selectbox("ปีการศึกษา", year_list)
            with col_b:
                campus_label    = st.selectbox("วิทยาเขต", list(campus_options.keys()))
                selected_campus = campus_options[campus_label]
            with col_c:
                faculty_label    = st.selectbox("คณะ", list(faculty_options.keys()))
                selected_faculty = faculty_options[faculty_label]

            campus_tag  = selected_campus  or "all"
            faculty_tag = selected_faculty or "all"
            base_name   = f"export_{selected_year}_{campus_tag}_{faculty_tag}"

            where_extra = ""
            params: dict = {"year": selected_year}
            if selected_campus:
                where_extra += " AND dc.campus_code = :campus"
                params["campus"] = selected_campus
            if selected_faculty:
                where_extra += " AND df.faculty_code = :faculty"
                params["faculty"] = selected_faculty

            check_count = pd.read_sql(
                text(f"""
                    SELECT COUNT(*) AS n
                    FROM fact_academic_calendar f
                    JOIN dim_campus  dc ON f.campus_id  = dc.id
                    JOIN dim_faculty df ON f.faculty_id = df.id
                    WHERE f.academic_year = :year{where_extra}
                """),
                engine, params=params,
            ).iloc[0, 0]

            if check_count == 0:
                st.warning(
                    f"❌ ไม่พบข้อมูลของ {faculty_label} "
                    f"วิทยาเขต {campus_label} ปีการศึกษา {selected_year}"
                )
                st.session_state.export_df = None
            else:
                st.success(f"✅ พบข้อมูล {check_count:,} แถว")

                if st.button("📊 โหลดข้อมูล", type="primary"):
                    with st.spinner("กำลังดึงข้อมูล..."):
                        df_fetched = pd.read_sql(
                            text(f"""
                                SELECT
                                    dd.date_actual      AS วันที่,
                                    dd.year_be          AS ปี_พศ,
                                    dd.month_name       AS เดือน,
                                    dd.day_name         AS วัน,
                                    dc.campus_name_th   AS ชื่อวิทยาเขต,
                                    df.faculty_name_th  AS ชื่อคณะ,
                                    f.academic_year     AS ปีการศึกษา,
                                    f.semester          AS ภาคเรียน,
                                    f.is_academic_day,
                                    f.is_holiday,
                                    f.holiday_name      AS ชื่อวันหยุด
                                FROM fact_academic_calendar f
                                JOIN dim_date    dd ON f.date_id    = dd.id
                                JOIN dim_campus  dc ON f.campus_id  = dc.id
                                JOIN dim_faculty df ON f.faculty_id = df.id
                                WHERE f.academic_year = :year{where_extra}
                                ORDER BY dd.date_actual, dc.campus_code, df.faculty_code
                            """),
                            engine, params=params,
                        )
                    st.session_state.export_df   = df_fetched
                    st.session_state.export_name = base_name

                if st.session_state.export_df is not None:
                    df_out = st.session_state.export_df
                    name   = st.session_state.export_name

                    if name == base_name:
                        st.info(f"พร้อมดาวน์โหลด: **{len(df_out):,}** แถว")
                        col_e, col_f = st.columns(2)
                        with col_e:
                            buf = io.BytesIO()
                            df_out.to_excel(buf, index=False)
                            st.download_button(
                                label="📥 ดาวน์โหลด Excel (.xlsx)",
                                data=buf.getvalue(),
                                file_name=f"{name}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True,
                            )
                        with col_f:
                            csv_bytes = df_out.to_csv(
                                index=False, encoding="utf-8-sig"
                            ).encode("utf-8-sig")
                            st.download_button(
                                label="📥 ดาวน์โหลด CSV (.csv)",
                                data=csv_bytes,
                                file_name=f"{name}.csv",
                                mime="text/csv",
                                use_container_width=True,
                            )
                    else:
                        st.info(
                            "กรุณากดปุ่ม 'โหลดข้อมูล' อีกครั้งเพื่ออัปเดตข้อมูลตามที่เลือกใหม่"
                        )

        st.divider()

        # ── Data preview ──────────────────────────────────────────────────────
        st.subheader("ตัวอย่างข้อมูลล่าสุด (50 แถว)")
        preview = pd.read_sql(
            text("""
                SELECT
                    dd.date_actual    AS วันที่,
                    dc.campus_code    AS วิทยาเขต,
                    df.faculty_code   AS คณะ,
                    f.academic_year   AS ปีการศึกษา,
                    f.semester        AS ภาคเรียน,
                    f.is_academic_day,
                    f.is_holiday,
                    f.holiday_name    AS ชื่อวันหยุด
                FROM fact_academic_calendar f
                JOIN dim_date    dd ON f.date_id    = dd.id
                JOIN dim_campus  dc ON f.campus_id  = dc.id
                JOIN dim_faculty df ON f.faculty_id = df.id
                ORDER BY dd.date_actual DESC, dc.campus_code, df.faculty_code
                LIMIT 50
            """),
            engine,
        )
        st.dataframe(preview, use_container_width=True, hide_index=True)

        st.divider()

        # ── Multi day_type / conflict section ─────────────────────────────────
        st.subheader("🔄 วันที่มีหลาย day_type")
        try:
            _mdf = pd.read_sql(text("""
                SELECT
                    d.date_actual AS วันที่,
                    dc.campus_name_th AS วิทยาเขต,
                    string_agg(f.day_type, ' + ' ORDER BY f.day_type) AS ประเภทวัน,
                    bool_or(f.is_holiday) AND bool_or(f.day_type = 'วันสอบ') AS มีข้อขัดแย้ง
                FROM fact_academic_calendar f
                JOIN dim_date d ON f.date_id = d.id
                JOIN dim_campus dc ON f.campus_id = dc.id
                JOIN dim_faculty df ON f.faculty_id = df.id
                WHERE df.faculty_code = 'Normal'
                GROUP BY d.date_actual, f.date_id, f.campus_id, dc.campus_name_th
                HAVING COUNT(DISTINCT f.day_type) > 1
                ORDER BY
                    (bool_or(f.is_holiday) AND bool_or(f.day_type = 'วันสอบ')) DESC,
                    d.date_actual
                LIMIT 50
            """), engine)
            if _mdf.empty:
                st.caption("ยังไม่มีวันที่มีหลาย day_type ในระบบ")
            else:
                _warn = int(_mdf["มีข้อขัดแย้ง"].sum())
                if _warn:
                    st.warning(f"⚠️ พบ {_warn} วัน ที่วันหยุดทับวันสอบ")
                st.dataframe(_mdf, use_container_width=True, hide_index=True)
        except Exception:
            st.caption("ยังไม่มีข้อมูล multi day_type")

        st.divider()

        # ── Holiday comparison: วันหยุดราชการ vs วันหยุดพิเศษ ม.อ. ─────────────
        st.subheader("📅 เปรียบเทียบวันหยุด: ราชการ vs พิเศษ ม.อ.")
        try:
            _hcdf = pd.read_sql(text("""
                SELECT
                    d.date_actual                                                              AS วันที่,
                    d.day_name                                                                AS วัน,
                    MAX(CASE WHEN f.source = 'google_calendar' THEN f.holiday_name END)       AS วันหยุดราชการ,
                    MAX(CASE WHEN f.source = 'manual'          THEN f.holiday_name END)       AS วันหยุดพิเศษ_มอ,
                    bool_or(f.source = 'google_calendar') AND bool_or(f.source = 'manual')   AS ทับกัน
                FROM fact_academic_calendar f
                JOIN dim_date    d  ON f.date_id    = d.id
                JOIN dim_faculty df ON f.faculty_id = df.id
                WHERE f.is_holiday = true
                  AND df.faculty_code = 'Normal'
                GROUP BY d.date_actual, d.day_name
                HAVING bool_or(f.source IN ('google_calendar', 'manual'))
                ORDER BY d.date_actual
            """), engine)

            if _hcdf.empty:
                st.caption("ยังไม่มีข้อมูลวันหยุดในระบบ")
            else:
                # เพิ่มคอลัมน์สถานะ
                def _hstatus(row):
                    if row["ทับกัน"]:
                        return "⚠️ ทับกัน"
                    if pd.notna(row["วันหยุดราชการ"]):
                        return "วันหยุดราชการ"
                    return "วันหยุดพิเศษ ม.อ."
                _hcdf["สถานะ"] = _hcdf.apply(_hstatus, axis=1)
                _overlap_n = int(_hcdf["ทับกัน"].sum())
                if _overlap_n:
                    st.warning(f"⚠️ พบ {_overlap_n} วัน ที่วันหยุดราชการทับกับวันหยุดพิเศษ ม.อ.")
                st.dataframe(
                    _hcdf[["วันที่", "วัน", "วันหยุดราชการ", "วันหยุดพิเศษ_มอ", "สถานะ"]],
                    use_container_width=True, hide_index=True,
                )
        except Exception:
            st.caption("ยังไม่มีข้อมูลวันหยุด")

    except Exception as e:
        st.error(f"เกิดข้อผิดพลาด: {e}")
        st.info("กรุณาตรวจสอบการเชื่อมต่อ Database หรือรอให้ Pipeline ทำงานเสร็จก่อน")

# ══════════════════════════════ MANUAL ENTRY TAB ═════════════════════════════
with tab_manual:
    st.header("✏️ กรอกปฏิทินการศึกษา")
    st.caption("กรอกข้อมูลวันสำคัญของภาคเรียนด้วยตนเอง แล้วบันทึกลง Database โดยตรง")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        mc_campus = st.selectbox("วิทยาเขต", CAMPUS_OPTIONS, key="mc_campus")
    with col2:
        mc_faculty = st.selectbox("คณะพิเศษ (ถ้ามี)", FACULTY_OPTIONS, key="mc_faculty")
    with col3:
        mc_year = st.number_input(
            "ปีการศึกษา (พ.ศ.)", min_value=2564, max_value=2580,
            value=2568, step=1, key="mc_year",
        )
    with col4:
        mc_semester = st.selectbox("ภาคเรียน", [1, 2, 3], key="mc_semester")

    st.divider()
    st.subheader("วันสำคัญ")

    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown("**เปิด / ปิดภาค**")
        mc_open  = st.date_input("วันเปิดภาค",  value=None, key="mc_open")
        mc_close = st.date_input("วันปิดภาค",   value=None, key="mc_close")
        st.markdown("**วันสุดท้ายของการเรียน**")
        mc_last  = st.date_input("วันสุดท้ายก่อนสอบ (ถ้ามี)", value=None, key="mc_last")
    with col_r:
        st.markdown("**สอบกลางภาค**")
        mc_mid_s = st.date_input("เริ่มสอบกลางภาค",   value=None, key="mc_mids")
        mc_mid_e = st.date_input("สิ้นสุดสอบกลางภาค", value=None, key="mc_mide")
        st.markdown("**สอบปลายภาค**")
        mc_fin_s = st.date_input("เริ่มสอบปลายภาค",   value=None, key="mc_fins")
        mc_fin_e = st.date_input("สิ้นสุดสอบปลายภาค", value=None, key="mc_fine")

    st.divider()
    st.subheader("วันหยุดพิเศษในภาคเรียน (ถ้ามี)")

    if st.button("➕ เพิ่มวันหยุด", key="mc_add_hol"):
        st.session_state.mc_holidays.append((None, ""))
        st.rerun()

    for _i, (_hd, _hn) in enumerate(list(st.session_state.mc_holidays)):
        _cols = st.columns([3, 6, 1])
        with _cols[0]:
            _nd = st.date_input("วันที่", value=_hd, key=f"mc_hd_{_i}", label_visibility="collapsed")
        with _cols[1]:
            _nn = st.text_input("ชื่อวันหยุด", value=_hn, key=f"mc_hn_{_i}", label_visibility="collapsed")
        with _cols[2]:
            if st.button("🗑️", key=f"mc_rm_{_i}"):
                st.session_state.mc_holidays.pop(_i)
                st.rerun()
        st.session_state.mc_holidays[_i] = (_nd, _nn)

    st.divider()

    if st.button("🔎 ตรวจสอบข้อมูล", key="mc_validate"):
        _errs = _validate_calendar(
            mc_campus, mc_faculty, int(mc_year), int(mc_semester),
            mc_open, mc_close, mc_mid_s, mc_mid_e, mc_fin_s, mc_fin_e,
        )
        st.session_state.mc_validated = _errs
        st.session_state.mc_saved = False
        st.rerun()

    if st.session_state.mc_validated is not None:
        _errs = st.session_state.mc_validated
        if _errs:
            for _e in _errs:
                st.error(_e)
        else:
            st.success("✅ ข้อมูลถูกต้อง กดบันทึกได้")

            # Show conflict popup when validation found conflicts
            if st.session_state.get("mc_conflicts"):
                _show_conflict_ui(
                    st.session_state.mc_conflicts,
                    confirm_key="mc_ov_yes", cancel_key="mc_ov_no",
                    confirmed_flag="mc_conflict_confirmed", conflict_flag="mc_conflicts",
                )
            _mc_should_save = st.session_state.get("mc_conflict_confirmed", False)

            if st.session_state.mc_saved:
                st.info("บันทึกเรียบร้อยแล้ว กรุณากรอกข้อมูลใหม่หากต้องการเพิ่มภาคเรียนอื่น")
            else:
                if st.button("💾 บันทึกลง Database", type="primary", key="mc_save"):
                    _fac_code_mc = "Normal" if mc_faculty == "ไม่มี (Normal)" else mc_faculty
                    _cf_mc = _validate_before_insert(
                        mc_campus, _fac_code_mc, int(mc_year), int(mc_semester),
                        mc_open, mc_close,
                    )
                    if _cf_mc:
                        st.session_state.mc_conflicts = _cf_mc
                        st.rerun()
                    else:
                        _mc_should_save = True

            if _mc_should_save:
                st.session_state.mc_conflict_confirmed = False
                _hols = [(d, n) for (d, n) in st.session_state.mc_holidays if d]
                with st.spinner("กำลังบันทึก..."):
                    try:
                        _n = _insert_semester_full(
                            campus=mc_campus,
                            faculty_label=mc_faculty,
                            year_be=int(mc_year),
                            semester=int(mc_semester),
                            open_date=mc_open,
                            close_date=mc_close,
                            midterm_start=mc_mid_s,
                            midterm_end=mc_mid_e,
                            final_start=mc_fin_s,
                            final_end=mc_fin_e,
                            extra_holidays=_hols,
                        )
                        st.success(
                            f"✅ บันทึกสำเร็จ: **{_n:,} แถว**  "
                            f"({mc_campus} · {mc_faculty} · ปี {int(mc_year)} · เทอม {int(mc_semester)})"
                        )
                        st.session_state.mc_saved = True
                    except Exception as _exc:
                        st.error(f"บันทึกไม่สำเร็จ: {_exc}")

# ══════════════════════════════ HOLIDAY TAB ═══════════════════════════════════
with tab_holiday:
    st.header("🗓️ เพิ่มวันหยุด")
    st.caption("เพิ่ม / อัปเดตวันหยุดนักขัตฤกษ์สำหรับข้อมูลที่มีอยู่แล้วในระบบ")

    col1, col2, col3 = st.columns(3)
    with col1:
        hol_all = st.checkbox("ทุกวิทยาเขต", value=False, key="hol_all")
        if not hol_all:
            hol_campuses = st.multiselect(
                "เลือกวิทยาเขต", CAMPUS_OPTIONS, key="hol_campuses"
            )
        else:
            hol_campuses = CAMPUS_OPTIONS
    with col2:
        hol_year = st.number_input(
            "ปีการศึกษา (พ.ศ.)", min_value=2564, max_value=2580,
            value=2568, step=1, key="hol_year",
        )
    with col3:
        hol_semester = st.selectbox("ภาคเรียน", [1, 2, 3], key="hol_semester")

    st.divider()
    st.subheader("รายการวันหยุด")

    if st.button("➕ เพิ่มรายการ", key="hol_add"):
        st.session_state.hol_list.append((None, "", HOL_TYPES[0]))
        st.rerun()

    for _i, _entry in enumerate(list(st.session_state.hol_list)):
        _hd = _entry[0] if len(_entry) > 0 else None
        _hn = _entry[1] if len(_entry) > 1 else ""
        _ht = _entry[2] if len(_entry) > 2 else HOL_TYPES[0]
        _cols = st.columns([3, 5, 3, 1])
        with _cols[0]:
            _nd = st.date_input("วันที่", value=_hd, key=f"hol_d_{_i}", label_visibility="collapsed")
        with _cols[1]:
            _nn = st.text_input("ชื่อวันหยุด", value=_hn, key=f"hol_n_{_i}", label_visibility="collapsed")
        with _cols[2]:
            _idx = HOL_TYPES.index(_ht) if _ht in HOL_TYPES else 0
            _nt = st.selectbox("ประเภท", HOL_TYPES, index=_idx, key=f"hol_t_{_i}", label_visibility="collapsed")
        with _cols[3]:
            if st.button("🗑️", key=f"hol_rm_{_i}"):
                st.session_state.hol_list.pop(_i)
                st.rerun()
        st.session_state.hol_list[_i] = (_nd, _nn, _nt)

    st.divider()

    if st.button("🔎 ตรวจสอบข้อมูล", key="hol_validate"):
        _hols = list(st.session_state.hol_list)
        _errs = _validate_holidays(hol_campuses, int(hol_year), _hols)
        st.session_state.hol_validated = _errs
        st.session_state.hol_saved = False
        st.rerun()

    if st.session_state.hol_validated is not None:
        _errs = st.session_state.hol_validated
        if _errs:
            for _e in _errs:
                st.error(_e)
        else:
            st.success("✅ ข้อมูลถูกต้อง กดบันทึกได้")
            _campus_label = "ทุกวิทยาเขต" if hol_all else ", ".join(hol_campuses)
            st.info(
                f"จะอัปเดต: **{_campus_label}**  ·  "
                f"ปี {int(hol_year)} เทอม {int(hol_semester)}  ·  "
                f"{len(st.session_state.hol_list)} วันหยุด"
            )

            # Show conflict popup when validation found conflicts
            if st.session_state.get("hol_conflicts"):
                _show_conflict_ui(
                    st.session_state.hol_conflicts,
                    confirm_key="hol_ov_yes", cancel_key="hol_ov_no",
                    confirmed_flag="hol_conflict_confirmed", conflict_flag="hol_conflicts",
                )
            _hol_should_save = st.session_state.get("hol_conflict_confirmed", False)

            if st.session_state.hol_saved:
                st.info("บันทึกเรียบร้อยแล้ว")
            else:
                if st.button("💾 บันทึกลง Database", type="primary", key="hol_save"):
                    _hols_raw = [(d, n, t) for (d, n, t) in st.session_state.hol_list if d]
                    _cf_hol = _validate_holidays_before_insert(hol_campuses, int(hol_year), _hols_raw)
                    if _cf_hol:
                        st.session_state.hol_conflicts = _cf_hol
                        st.rerun()
                    else:
                        _hol_should_save = True

            if _hol_should_save:
                st.session_state.hol_conflict_confirmed = False
                _hols = [(d, n, t) for (d, n, t) in st.session_state.hol_list if d]
                with st.spinner("กำลังบันทึก..."):
                    try:
                        _n = _insert_holidays_to_db(
                            campuses=hol_campuses,
                            year_be=int(hol_year),
                            semester=int(hol_semester),
                            holidays=_hols,
                        )
                        st.success(f"✅ อัปเดตสำเร็จ: **{_n:,} แถว**")
                        st.session_state.hol_saved = True
                    except Exception as _exc:
                        st.error(f"บันทึกไม่สำเร็จ: {_exc}")

# ══════════════════════════════ UPLOAD TAB ════════════════════════════════════
with tab_upload:
    st.header("📤 อัปโหลดปฏิทินการศึกษา")
    st.caption("PDF / JPG / PNG → Groq AI (llama-3.3-70b / llama-4-scout vision)")

    # ── Step 1 : File uploader ────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "เลือกไฟล์ PDF หรือรูปภาพ",
        type=["pdf", "jpg", "jpeg", "png"],
        help="PDF → pdfplumber (OCR fallback ต่อหน้า)  ·  JPG/PNG → Claude Vision API",
    )

    if uploaded is not None:
        file_key = f"{uploaded.name}:{uploaded.size}"
        if file_key != st.session_state.up_key:
            st.session_state.up_key    = file_key
            st.session_state.up_bytes  = uploaded.read()
            st.session_state.up_fname  = uploaded.name
            st.session_state.up_ext    = Path(uploaded.name).suffix.lower()
            st.session_state.up_text   = ""
            st.session_state.up_parsed = None
            st.session_state.up_saved  = False
            st.session_state.up_auto_campus = None
            st.session_state.up_auto_year   = None
            st.session_state.up_parse_msg   = None

    if not st.session_state.up_bytes:
        st.stop()

    st.success(
        f"ไฟล์: **{st.session_state.up_fname}**  "
        f"({len(st.session_state.up_bytes):,} bytes)"
    )

    # ── Step 2 : Metadata form ────────────────────────────────────────────────
    st.subheader("ข้อมูลปฏิทิน")
    col1, col2, col3 = st.columns(3)
    with col1:
        campus = st.selectbox("วิทยาเขต", CAMPUS_OPTIONS, key="up_campus")
    with col2:
        faculty = st.selectbox("คณะพิเศษ (ถ้ามี)", FACULTY_OPTIONS, key="up_faculty")
    with col3:
        year_be = st.number_input(
            "ปีการศึกษา (พ.ศ.)", min_value=2564, max_value=2580,
            step=1, key="up_year",
        )
        if st.session_state.get("up_auto_year"):
            st.caption(f"✅ ตรวจพบอัตโนมัติ: {st.session_state.up_auto_year}")

    if st.button("🔍 อ่านและวิเคราะห์ไฟล์", type="primary", key="btn_parse"):
        ext = st.session_state.up_ext
        st.session_state["up_auto_campus"] = None
        st.session_state["up_auto_year"]   = None
        st.session_state["up_parse_msg"]   = None

        with st.spinner("กำลังวิเคราะห์ด้วย Groq AI (ทุกภาคเรียน)..."):
            try:
                result     = _groq_extract_dates(st.session_state.up_bytes, ext)
                campus_det = result.pop("campus", None)
                year_det   = result.pop("academic_year", None)
                # campus: ไม่ auto-fill ให้ผู้ใช้เลือกเอง
                if year_det:
                    st.session_state["_pending_year"] = int(year_det)
                    st.session_state["up_auto_year"]  = int(year_det)
                st.session_state.up_text   = ""
                # result["semesters"] = list of semester dicts
                st.session_state.up_parsed = result.get("semesters", [])
            except Exception as exc:
                st.session_state["up_parse_msg"] = (
                    "warning",
                    f"Groq Vision ล้มเหลว: {exc} — กรุณากรอกวันที่ด้วยตนเอง",
                )
                st.session_state.up_text   = ""
                # fallback: สร้าง 2 ภาค ว่างๆ ให้กรอกเอง
                st.session_state.up_parsed = [
                    {"semester": 1, "open": None, "close": None,
                     "midterm_start": None, "midterm_end": None,
                     "final_start": None, "final_end": None},
                    {"semester": 2, "open": None, "close": None,
                     "midterm_start": None, "midterm_end": None,
                     "final_start": None, "final_end": None},
                ]

        st.session_state.up_saved = False
        st.rerun()

    # ── Step 3 : Review (ทุกภาคเรียน) ──────────────────────────────────────────
    if st.session_state.up_parsed is None:
        st.stop()

    if st.session_state.get("up_parse_msg"):
        msg_type, msg_text = st.session_state["up_parse_msg"]
        getattr(st, msg_type)(msg_text)

    st.subheader("ผลการวิเคราะห์")

    semesters_data = st.session_state.up_parsed  # list of dicts
    if not semesters_data:
        st.warning("ไม่พบข้อมูลภาคเรียนในเอกสาร กรุณาเพิ่มด้วยตนเอง")
        semesters_data = []

    def _found(d) -> str:
        return "✅ พบอัตโนมัติ" if d else "⚠️ ไม่พบ — กรุณากรอก"

    # สร้าง tab สำหรับแต่ละภาคเรียน
    sem_labels  = [f"ภาคที่ {s['semester']}" for s in semesters_data]
    date_inputs = {}   # { semester_num: {open, close, midterm_start, ...} }

    if sem_labels:
        tabs = st.tabs(sem_labels)
        for tab, sem_dict in zip(tabs, semesters_data):
            sn = sem_dict["semester"]
            with tab:
                st.markdown(f"**วันสำคัญ ภาคเรียนที่ {sn}** (แก้ไขได้ก่อนยืนยัน)")
                col_l, col_r = st.columns(2)
                with col_l:
                    st.markdown("**เปิด / ปิดภาค**")
                    st.caption(f"วันเปิดภาค: {_found(sem_dict['open'])}")
                    od = st.date_input("วันเปิดภาค",  value=sem_dict["open"],  key=f"di_open_{sn}")
                    st.caption(f"วันปิดภาค: {_found(sem_dict['close'])}")
                    cd = st.date_input("วันปิดภาค",   value=sem_dict["close"], key=f"di_close_{sn}")
                with col_r:
                    st.markdown("**การสอบ**")
                    st.caption(f"สอบกลางภาค: {_found(sem_dict['midterm_start'])}")
                    ms = st.date_input("สอบกลางภาค (เริ่ม)",   value=sem_dict["midterm_start"], key=f"di_mids_{sn}")
                    me = st.date_input("สอบกลางภาค (สิ้นสุด)", value=sem_dict["midterm_end"],   key=f"di_mide_{sn}")
                    st.caption(f"สอบปลายภาค: {_found(sem_dict['final_start'])}")
                    fs = st.date_input("สอบปลายภาค (เริ่ม)",   value=sem_dict["final_start"],   key=f"di_fins_{sn}")
                    fe = st.date_input("สอบปลายภาค (สิ้นสุด)", value=sem_dict["final_end"],     key=f"di_fine_{sn}")
                date_inputs[sn] = {"open": od, "close": cd,
                                   "midterm_start": ms, "midterm_end": me,
                                   "final_start": fs, "final_end": fe}
    else:
        st.info("ไม่มีข้อมูล — กรุณากรอกวันที่ด้วยตนเอง")

    # ── Step 4 : Confirm / Cancel ─────────────────────────────────────────────
    st.divider()
    target_fname = _gen_filename(campus, faculty, int(year_be))
    if st.session_state.up_ext != ".pdf":
        st.info("รูปภาพจะถูกแปลงเป็น PDF อัตโนมัติ")
    st.info(f"จะบันทึกไฟล์: **`{target_fname}`** → `{INPUT_DIR}/`")

    _ready_sems = [sn for sn, d in date_inputs.items() if d["open"] and d["close"]]
    _can_confirm = bool(_ready_sems) and not st.session_state.up_saved

    if not _ready_sems:
        st.warning("กรุณาระบุวันเปิดภาคและวันปิดภาคอย่างน้อย 1 ภาคเรียนก่อนยืนยัน")

    col_ok, col_reset = st.columns([2, 8])
    with col_ok:
        confirm = st.button(
            "✅ ยืนยันและบันทึกทั้งหมด",
            type="primary",
            disabled=not _can_confirm,
            key="btn_confirm",
        )
    with col_reset:
        if st.button("🔄 ล้างและเริ่มใหม่", key="btn_reset"):
            for _k in ("up_key", "up_fname", "up_ext", "up_text"):
                st.session_state[_k] = ""
            for _k in ("up_bytes", "up_parsed", "up_auto_campus", "up_auto_year", "up_parse_msg"):
                st.session_state[_k] = None
            st.session_state.up_saved = False
            st.rerun()

    # Show conflict popup when background validation found issues
    if st.session_state.get("up_conflicts"):
        _show_conflict_ui(
            st.session_state.up_conflicts,
            confirm_key="up_ov_yes", cancel_key="up_ov_no",
            confirmed_flag="up_conflict_confirmed", conflict_flag="up_conflicts",
        )
    _up_should_save = st.session_state.get("up_conflict_confirmed", False)

    if confirm:
        _fac_code = "Normal" if faculty == "ไม่มี (Normal)" else faculty
        _all_cf = []
        for sn, d in sorted(date_inputs.items()):
            if not (d.get("open") and d.get("close")):
                continue
            _cf = _validate_before_insert(campus, _fac_code, int(year_be), sn, d["open"], d["close"])
            _all_cf.extend(_cf)
        if _all_cf:
            st.session_state.up_conflicts = _all_cf
            st.rerun()
        else:
            _up_should_save = True

    if _up_should_save:
        st.session_state.up_conflict_confirmed = False

        # 1. Save PDF to input/
        save_bytes = _to_pdf_bytes(st.session_state.up_bytes, st.session_state.up_ext)
        save_path  = Path(INPUT_DIR) / target_fname
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(save_bytes)
            st.success(f"✅ บันทึกไฟล์: `{save_path}`")
        except Exception as exc:
            st.error(f"บันทึกไฟล์ไม่สำเร็จ: {exc}")
            st.stop()

        # 2. Insert each semester into DB
        total_rows = 0
        with st.spinner("กำลังบันทึกข้อมูลลง Database..."):
            for sn, d in sorted(date_inputs.items()):
                if not (d["open"] and d["close"]):
                    continue
                try:
                    n = _insert_semester_to_db(
                        campus=campus,
                        faculty_label=faculty,
                        year_be=int(year_be),
                        semester=sn,
                        open_date=d["open"],
                        close_date=d["close"],
                        source='pdf',
                    )
                    total_rows += n
                    st.success(f"✅ ภาคที่ {sn}: **{n:,} แถว** ({d['open']} → {d['close']})")
                except Exception as exc:
                    st.error(f"บันทึก DB ภาคที่ {sn} ไม่สำเร็จ: {exc}")

        if total_rows:
            st.info(f"📊 รวมทั้งหมด **{total_rows:,} แถว** ({campus} · ปี {int(year_be)})")

        # 3. Trigger pipeline (best-effort)
        try:
            _req = urllib.request.Request(
                "http://pipeline:8502/run", data=b"", method="POST"
            )
            with urllib.request.urlopen(_req, timeout=5) as _resp:
                if _resp.status == 202:
                    st.info("🚀 Pipeline triggered — Dashboard จะอัปเดตใน 1-2 นาที")
        except Exception:
            pass

        st.session_state.up_saved = True
        st.rerun()

# ══════════════════════════════ DATA MANAGEMENT ══════════════════════════════
with tab_mgmt:
    st.header("⚙️ จัดการข้อมูล")

    # ── Year options (shared between delete and edit) ─────────────────────────
    try:
        _yr_df   = pd.read_sql(
            "SELECT DISTINCT academic_year FROM fact_academic_calendar ORDER BY academic_year DESC",
            get_engine(),
        )
        _yr_opts = _yr_df["academic_year"].tolist()
    except Exception:
        _yr_opts = [2568]

    _sub_del, _sub_edit = st.tabs(["🗑️ ลบข้อมูล", "✏️ แก้ไขข้อมูล"])

    # ══ DELETE ════════════════════════════════════════════════════════════════
    with _sub_del:
        st.caption("ลบข้อมูลจาก fact_academic_calendar พร้อม Backup อัตโนมัติก่อนลบทุกครั้ง")
        st.subheader("🔍 เลือกเงื่อนไขการลบ")

        _col1, _col2, _col3 = st.columns(3)

        with _col1:
            mgmt_year = st.selectbox("ปีการศึกษา", _yr_opts, key="mgmt_year")

        with _col2:
            mgmt_all_campus = st.checkbox("ทุกวิทยาเขต", value=False, key="mgmt_all_campus")
            if mgmt_all_campus:
                mgmt_campus: Optional[str] = None
                st.caption("เลือก: ทุกวิทยาเขต")
            else:
                mgmt_campus = st.selectbox("วิทยาเขต", CAMPUS_OPTIONS, key="mgmt_campus_sel")

        with _col3:
            mgmt_all_sem = st.checkbox("ทุกภาคเรียน", value=False, key="mgmt_all_sem")
            if mgmt_all_sem:
                mgmt_semester: Optional[int] = None
                st.caption("เลือก: ทุกภาคเรียน")
            else:
                mgmt_semester = st.selectbox("ภาคเรียน", [1, 2, 3], key="mgmt_sem_sel")

        st.divider()

        # ── Preview ───────────────────────────────────────────────────────────
        if st.button("🔍 ตรวจสอบข้อมูลที่จะลบ", type="primary", key="mgmt_preview_btn"):
            with st.spinner("กำลังนับข้อมูล..."):
                try:
                    _cnt, _prev_df = _count_and_fetch_for_delete(
                        int(mgmt_year),
                        None if mgmt_all_campus else mgmt_campus,
                        None if mgmt_all_sem    else int(mgmt_semester),
                    )
                    st.session_state.mgmt_preview   = (_cnt, _prev_df)
                    st.session_state.mgmt_backup    = None
                    st.session_state.mgmt_confirmed = False
                except Exception as _exc:
                    st.error(f"เกิดข้อผิดพลาด: {_exc}")
                    st.session_state.mgmt_preview = None

        if st.session_state.mgmt_preview is not None:
            _cnt, _prev_df = st.session_state.mgmt_preview
            _campus_lbl = "ทุกวิทยาเขต" if mgmt_all_campus else mgmt_campus
            _sem_lbl    = "ทุกภาคเรียน" if mgmt_all_sem    else f"ภาคเรียนที่ {mgmt_semester}"

            if _cnt == 0:
                st.info(f"ไม่พบข้อมูลที่ตรงกับเงื่อนไข (ปี {mgmt_year} · {_campus_lbl} · {_sem_lbl})")
            else:
                st.error(
                    f"⚠️ พบ **{_cnt:,} แถว** ที่จะถูกลบ  "
                    f"(ปี {mgmt_year} · {_campus_lbl} · {_sem_lbl})"
                )
                st.dataframe(_prev_df.head(100), use_container_width=True, hide_index=True)
                if _cnt > 100:
                    st.caption(f"แสดง 100 แถวแรกจากทั้งหมด {_cnt:,} แถว")

                st.divider()

                # ── Backup download ───────────────────────────────────────────
                st.subheader("📦 Backup ก่อนลบ")
                _bk_buf  = io.BytesIO()
                _prev_df.to_excel(_bk_buf, index=False)
                _bk_name = (
                    f"backup_{mgmt_year}"
                    f"_{(None if mgmt_all_campus else mgmt_campus) or 'all'}"
                    f"_{(None if mgmt_all_sem else mgmt_semester) or 'all'}.xlsx"
                )
                st.download_button(
                    label="📥 ดาวน์โหลด Backup (.xlsx)",
                    data=_bk_buf.getvalue(),
                    file_name=_bk_name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="mgmt_dl_preview",
                )
                st.caption("แนะนำให้ดาวน์โหลด Backup ก่อนดำเนินการลบ")

                st.divider()

                # ── Confirm & delete ──────────────────────────────────────────
                st.subheader("🗑️ ยืนยันการลบ")
                st.warning(
                    "การลบข้อมูลไม่สามารถย้อนกลับได้  "
                    "กรุณาพิมพ์ **ยืนยันการลบ** ในช่องด้านล่างเพื่อดำเนินการต่อ"
                )
                _confirm_input = st.text_input(
                    "พิมพ์ 'ยืนยันการลบ' เพื่อยืนยัน",
                    value="",
                    key="mgmt_confirm_input",
                    placeholder="ยืนยันการลบ",
                )
                _can_delete = _confirm_input.strip() == "ยืนยันการลบ"

                if st.button(
                    f"🗑️ ลบ {_cnt:,} แถว ถาวร",
                    type="primary",
                    disabled=not _can_delete,
                    key="mgmt_delete_btn",
                ):
                    with st.spinner("กำลังสร้าง Backup และลบข้อมูล..."):
                        try:
                            _final_bk = io.BytesIO()
                            _prev_df.to_excel(_final_bk, index=False)
                            st.session_state.mgmt_backup = (_bk_name, _final_bk.getvalue())

                            _deleted = _execute_delete(
                                int(mgmt_year),
                                None if mgmt_all_campus else mgmt_campus,
                                None if mgmt_all_sem    else int(mgmt_semester),
                            )

                            from datetime import datetime as _dt
                            st.session_state.mgmt_history.insert(0, {
                                "เวลา":         _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "ปีการศึกษา":  int(mgmt_year),
                                "วิทยาเขต":    _campus_lbl,
                                "ภาคเรียน":    _sem_lbl,
                                "แถวที่ลบ":    _deleted,
                                "ไฟล์ Backup": _bk_name,
                            })
                            st.session_state.mgmt_preview   = None
                            st.session_state.mgmt_confirmed = False
                            st.success(f"✅ ลบสำเร็จ: **{_deleted:,} แถว**")
                            st.rerun()

                        except Exception as _exc:
                            st.error(f"ลบไม่สำเร็จ: {_exc}")

        # ── Last backup download ──────────────────────────────────────────────
        if st.session_state.mgmt_backup:
            _lbk_name, _lbk_bytes = st.session_state.mgmt_backup
            st.info("Backup ล่าสุดพร้อมดาวน์โหลด")
            st.download_button(
                label="📥 ดาวน์โหลด Backup ล่าสุด",
                data=_lbk_bytes,
                file_name=_lbk_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="mgmt_dl_last",
            )

        # ── Deletion history ──────────────────────────────────────────────────
        if st.session_state.mgmt_history:
            st.divider()
            st.subheader("📋 ประวัติการลบ (session นี้)")
            st.dataframe(
                pd.DataFrame(st.session_state.mgmt_history),
                use_container_width=True,
                hide_index=True,
            )

    # ══ EDIT ══════════════════════════════════════════════════════════════════
    with _sub_edit:
        st.caption("กรองข้อมูล → โหลด → แก้ไขตรง cell เหมือน Excel → กดบันทึก")
        st.subheader("🔍 เลือกข้อมูลที่ต้องการแก้ไข")

        _ecol1, _ecol2, _ecol3 = st.columns(3)

        with _ecol1:
            edit_year = st.selectbox("ปีการศึกษา", _yr_opts, key="edit_year")

        with _ecol2:
            edit_all_campus = st.checkbox("ทุกวิทยาเขต", value=False, key="edit_all_campus")
            if edit_all_campus:
                edit_campus: Optional[str] = None
                st.caption("เลือก: ทุกวิทยาเขต")
            else:
                edit_campus = st.selectbox("วิทยาเขต", CAMPUS_OPTIONS, key="edit_campus_sel")

        with _ecol3:
            edit_all_sem = st.checkbox("ทุกภาคเรียน", value=False, key="edit_all_sem")
            if edit_all_sem:
                edit_semester: Optional[int] = None
                st.caption("เลือก: ทุกภาคเรียน")
            else:
                edit_semester = st.selectbox("ภาคเรียน", [1, 2, 3], key="edit_sem_sel")

        st.divider()

        if st.button("📂 โหลดข้อมูลเพื่อแก้ไข", type="primary", key="edit_load_btn"):
            with st.spinner("กำลังโหลดข้อมูล..."):
                try:
                    _edf = _fetch_for_edit(
                        int(edit_year),
                        None if edit_all_campus else edit_campus,
                        None if edit_all_sem    else int(edit_semester),
                    )
                    st.session_state.edit_df_original = _edf.copy()
                except Exception as _exc:
                    st.error(f"โหลดไม่สำเร็จ: {_exc}")
                    st.session_state.edit_df_original = None

        if st.session_state.edit_df_original is not None:
            _orig  = st.session_state.edit_df_original
            _total = len(_orig)

            if _total == 0:
                st.info("ไม่พบข้อมูลตามเงื่อนไขที่เลือก")
            else:
                if _total >= 1000:
                    st.warning(f"⚠️ แสดงสูงสุด 1,000 แถว — กรุณา filter วิทยาเขต/ภาคเรียนให้แคบลง")
                else:
                    st.success(f"โหลดสำเร็จ **{_total:,} แถว** — คลิก cell เพื่อแก้ไขได้เลย")

                _DAY_TYPES = ["วันทำการ", "วันหยุด", "วันหยุดนักขัตฤกษ์", "วันสอบ", "ปิดภาค"]

                _edited = st.data_editor(
                    _orig.drop(columns=["_fact_id"]),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "วันที่":          st.column_config.DateColumn("วันที่",       disabled=True),
                        "วิทยาเขต":       st.column_config.TextColumn("วิทยาเขต",     disabled=True),
                        "คณะ":             st.column_config.TextColumn("คณะ",          disabled=True),
                        "ปีการศึกษา":     st.column_config.NumberColumn("ปีการศึกษา", disabled=True),
                        "ภาคเรียน":       st.column_config.NumberColumn(
                                               "ภาคเรียน", min_value=1, max_value=3, step=1
                                           ),
                        "is_academic_day": st.column_config.CheckboxColumn("วันทำการ ✓"),
                        "is_holiday":      st.column_config.CheckboxColumn("วันหยุด ✓"),
                        "ชื่อวันหยุด":    st.column_config.TextColumn("ชื่อวันหยุด"),
                        "ประเภทวัน":      st.column_config.SelectboxColumn(
                                               "ประเภทวัน", options=_DAY_TYPES
                                           ),
                        "แหล่งข้อมูล":    st.column_config.TextColumn("แหล่งข้อมูล", disabled=True),
                    },
                    key="edit_data_editor",
                    num_rows="fixed",
                )

                # ── Detect changed rows ───────────────────────────────────────
                _editable_cols = ["ภาคเรียน", "is_academic_day", "is_holiday", "ชื่อวันหยุด", "ประเภทวัน"]
                _orig_cmp = (
                    _orig.drop(columns=["_fact_id"])[_editable_cols]
                    .reset_index(drop=True)
                    .astype(str)
                )
                _edit_cmp = _edited[_editable_cols].reset_index(drop=True).astype(str)
                _mask        = (_orig_cmp != _edit_cmp).any(axis=1)
                _changed_idx = list(_mask[_mask].index)

                if _changed_idx:
                    st.info(f"✏️ แก้ไขแล้ว **{len(_changed_idx)} แถว** — กดบันทึกเพื่อยืนยัน")

                    _sc1, _sc2 = st.columns([2, 8])
                    with _sc1:
                        if st.button("💾 บันทึกการแก้ไข", type="primary", key="edit_save_btn"):
                            # Re-attach _fact_id by index alignment
                            _fact_ids    = _orig["_fact_id"].reset_index(drop=True)
                            _changed_df  = _edited.reset_index(drop=True).loc[_changed_idx].copy()
                            _changed_df["_fact_id"] = _fact_ids.loc[_changed_idx].values
                            with st.spinner("กำลังบันทึก..."):
                                try:
                                    _n_up = _execute_update_rows(_changed_df)
                                    from datetime import datetime as _dt
                                    st.session_state.edit_history.insert(0, {
                                        "เวลา":          _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "ปีการศึกษา":   int(edit_year),
                                        "วิทยาเขต":     "ทุกวิทยาเขต" if edit_all_campus else edit_campus,
                                        "ภาคเรียน":     "ทุกภาคเรียน" if edit_all_sem    else f"ภาคเรียนที่ {edit_semester}",
                                        "แถวที่แก้ไข":  _n_up,
                                    })
                                    # Reload to reflect saved state
                                    st.session_state.edit_df_original = _fetch_for_edit(
                                        int(edit_year),
                                        None if edit_all_campus else edit_campus,
                                        None if edit_all_sem    else int(edit_semester),
                                    ).copy()
                                    st.success(f"✅ บันทึกสำเร็จ: **{_n_up} แถว**")
                                    st.rerun()
                                except Exception as _exc:
                                    st.error(f"บันทึกไม่สำเร็จ: {_exc}")
                    with _sc2:
                        if st.button("↩️ ยกเลิกและโหลดใหม่", key="edit_cancel_btn"):
                            st.session_state.edit_df_original = None
                            st.rerun()
                else:
                    st.caption("ยังไม่มีการแก้ไข — คลิกที่ cell ในตารางเพื่อแก้ไขได้เลย")

        # ── Edit history ──────────────────────────────────────────────────────
        if st.session_state.edit_history:
            st.divider()
            st.subheader("📋 ประวัติการแก้ไข (session นี้)")
            st.dataframe(
                pd.DataFrame(st.session_state.edit_history),
                use_container_width=True,
                hide_index=True,
            )
