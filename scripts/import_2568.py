#!/usr/bin/env python3
"""Import academic calendar data from PDFs using Gemini Vision API.

Usage:
    python scripts/import_2568.py
    python scripts/import_2568.py --year 2568   # only process a specific year
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import execute_values

# ── Load .env into os.environ (Docker sets these; local dev reads .env) ────────
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
if not GROQ_API_KEY:
    sys.exit("ERROR: GROQ_API_KEY environment variable is not set")

from groq import Groq as _Groq
import base64 as _b64
import pdfplumber as _pdfplumber
import fitz as _fitz

INPUT_DIR = os.environ.get("INPUT_DIR", "/app/input")

# ── Thai helpers ───────────────────────────────────────────────────────────────
_THAI_DIGIT = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

THAI_MONTHS_FULL  = {
    1:"มกราคม",2:"กุมภาพันธ์",3:"มีนาคม",4:"เมษายน",
    5:"พฤษภาคม",6:"มิถุนายน",7:"กรกฎาคม",8:"สิงหาคม",
    9:"กันยายน",10:"ตุลาคม",11:"พฤศจิกายน",12:"ธันวาคม",
}
THAI_MONTHS_SHORT = {
    1:"ม.ค.",2:"ก.พ.",3:"มี.ค.",4:"เม.ย.",
    5:"พ.ค.",6:"มิ.ย.",7:"ก.ค.",8:"ส.ค.",
    9:"ก.ย.",10:"ต.ค.",11:"พ.ย.",12:"ธ.ค.",
}
THAI_DAYS = {
    0:"จันทร์",1:"อังคาร",2:"พุธ",
    3:"พฤหัสบดี",4:"ศุกร์",5:"เสาร์",6:"อาทิตย์",
}
CAMPUSES = {
    "HatYai":     "วิทยาเขตหาดใหญ่",
    "Pattani":    "วิทยาเขตปัตตานี",
    "Phuket":     "วิทยาเขตภูเก็ต",
    "Trang":      "วิทยาเขตตรัง",
    "Suratthani": "วิทยาเขตสุราษฎร์ฯ",
}

# ── Gemini prompt ──────────────────────────────────────────────────────────────
_PROMPT = """\
อ่านปฏิทินการศึกษามหาวิทยาลัยสงขลานครินทร์จากเอกสาร PDF นี้
ดึงข้อมูลทั้งหมดแล้วตอบเป็น JSON เท่านั้น (ห้ามมีข้อความอื่นนอกจาก JSON):

{
  "campus": "HatYai",
  "academic_year": 2568,
  "semester": null,
  "events": [
    {
      "event_type": "semester_start",
      "semester": 1,
      "date_start": "DD/MM/YYYY",
      "date_end": null,
      "description": "วันเปิดภาคการศึกษาที่ 1"
    },
    {
      "event_type": "semester_end",
      "semester": 1,
      "date_start": "DD/MM/YYYY",
      "date_end": null,
      "description": "วันปิดภาคการศึกษาที่ 1"
    }
  ]
}

กฎ:
- campus: HatYai, Pattani, Phuket, Trang, หรือ Suratthani
- academic_year: ปีการศึกษา พ.ศ. (ถ้าเอกสารระบุ ค.ศ. ให้บวก 543)
- event_type ที่ใช้ได้: semester_start, semester_end, midterm_start, midterm_end,
  final_start, final_end, holiday, registration
- ทุก event ต้องมี field "semester": 1, 2, หรือ 3 (ภาคฤดูร้อน=3)
- date_start, date_end: DD/MM/YYYY เป็น พ.ศ. (ถ้าไม่มี date_end ให้ใส่ null)
- รวมทุกภาคการศึกษาที่พบในไฟล์นี้ไว้ใน events เดียวกัน
- เลขไทย (๑,๒,...) ให้แปลงเป็นเลขอาหรับ"""


# ── Date helpers ───────────────────────────────────────────────────────────────

def _parse_be_date(s) -> Optional[date]:
    """Parse DD/MM/YYYY (BE) or YYYY-MM-DD string → CE date."""
    if not s:
        return None
    s = str(s).strip().translate(_THAI_DIGIT)
    # DD/MM/YYYY
    m = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yr_ce = yr - 543 if yr > 2500 else yr
        try:
            return date(yr_ce, mo, d)
        except ValueError:
            return None
    # YYYY-MM-DD (CE or BE)
    m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        yr, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yr_ce = yr - 543 if yr > 2500 else yr
        try:
            return date(yr_ce, mo, d)
        except ValueError:
            return None
    return None


# ── Groq call ─────────────────────────────────────────────────────────────────

def gemini_extract(pdf_path: str) -> dict:
    """ใช้ชื่อเดิมเพื่อไม่ต้องแก้ส่วนอื่น แต่เปลี่ยนมาใช้ Groq แล้ว"""
    client = _Groq(api_key=GROQ_API_KEY)
    print(f"  Reading {Path(pdf_path).name} ...")

    # ลอง pdfplumber ก่อน
    text = ""
    try:
        with _pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        pass

    if text.strip():
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": _PROMPT + "\n\nข้อความจากเอกสาร:\n" + text[:8000]}],
            temperature=0.0,
        )
    else:
        # PDF สแกน → vision
        doc = _fitz.open(pdf_path)
        content = [{"type": "text", "text": _PROMPT}]
        for i, page in enumerate(doc):
            if i >= 4:
                break
            pix = page.get_pixmap(matrix=_fitz.Matrix(1.5, 1.5))
            b64 = _b64.b64encode(pix.tobytes("png")).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        resp = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{"role": "user", "content": content}],
            temperature=0.0,
        )

    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r'^```[a-z]*\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw).strip()
    return json.loads(raw)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "postgres"),
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ.get("DB_NAME", "psu_academic"),
        user=os.environ.get("DB_USER", "admin"),
        password=os.environ.get("DB_PASSWORD", "psu2024"),
        client_encoding="utf8",
    )


def _ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dim_date (
            id BIGSERIAL PRIMARY KEY, date_actual DATE UNIQUE NOT NULL,
            date_str VARCHAR(12), year_ce INTEGER, year_be INTEGER,
            month_num INTEGER, month_name TEXT, month_short TEXT,
            day_num INTEGER, day_name TEXT, day_of_week INTEGER,
            quarter VARCHAR(4), is_weekend BOOLEAN)""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dim_campus (
            id SERIAL PRIMARY KEY, campus_code TEXT UNIQUE NOT NULL,
            campus_name_th TEXT)""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dim_faculty (
            id SERIAL PRIMARY KEY, faculty_code TEXT UNIQUE NOT NULL,
            faculty_name_th TEXT)""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fact_academic_calendar (
            id BIGSERIAL PRIMARY KEY,
            date_id BIGINT REFERENCES dim_date(id),
            campus_id INTEGER REFERENCES dim_campus(id),
            faculty_id INTEGER REFERENCES dim_faculty(id),
            academic_year INTEGER, semester INTEGER,
            week_of_semester INTEGER, is_academic_day BOOLEAN,
            is_holiday BOOLEAN, holiday_name TEXT, day_type TEXT,
            UNIQUE(date_id, campus_id, faculty_id, day_type))""")


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
          THAI_MONTHS_FULL[d.month], THAI_MONTHS_SHORT[d.month],
          d.day, THAI_DAYS[wday], wday + 1, f"Q{q}", wday >= 5))
    return cur.fetchone()[0]


def _upsert_campus(cur, code: str) -> int:
    cur.execute("""
        INSERT INTO dim_campus (campus_code, campus_name_th) VALUES (%s,%s)
        ON CONFLICT (campus_code) DO UPDATE SET campus_name_th=EXCLUDED.campus_name_th
        RETURNING id
    """, (code, CAMPUSES.get(code, code)))
    return cur.fetchone()[0]


def _upsert_faculty(cur, code: str) -> int:
    cur.execute("""
        INSERT INTO dim_faculty (faculty_code, faculty_name_th) VALUES (%s,%s)
        ON CONFLICT (faculty_code) DO UPDATE SET faculty_name_th=EXCLUDED.faculty_name_th
        RETURNING id
    """, (code, code))
    return cur.fetchone()[0]


# ── Core insert logic ──────────────────────────────────────────────────────────

def insert_calendar(data: dict):
    campus        = data.get("campus", "HatYai")
    academic_year = int(data.get("academic_year", 2568))
    events        = data.get("events", [])

    # Group events by semester
    sem_starts: dict[int, date] = {}
    sem_ends:   dict[int, date] = {}
    holidays:   dict[date, str] = {}

    for ev in events:
        et   = ev.get("event_type", "")
        sem  = int(ev.get("semester") or data.get("semester") or 1)
        ds   = _parse_be_date(ev.get("date_start"))
        de   = _parse_be_date(ev.get("date_end")) or ds
        desc = ev.get("description", "")

        if et == "semester_start" and ds:
            sem_starts[sem] = ds
        elif et == "semester_end" and ds:
            sem_ends[sem] = ds
        elif et == "holiday" and ds and de:
            cur = ds
            while cur <= de:
                holidays[cur] = desc
                cur += timedelta(days=1)

    if not sem_starts:
        print(f"  [WARN] No semester_start events found — skipping {campus} {academic_year}")
        return 0

    conn = _get_conn()
    total = 0
    try:
        cur = conn.cursor()
        _ensure_tables(cur)
        campus_id  = _upsert_campus(cur, campus)
        faculty_id = _upsert_faculty(cur, "Normal")

        for sem, start_d in sorted(sem_starts.items()):
            end_d = sem_ends.get(sem)
            if not end_d:
                print(f"  [WARN] No semester_end for semester {sem} — skipping")
                continue

            current  = start_d
            week_num = 1
            rows     = []
            while current <= end_d:
                date_id = _upsert_dim_date(cur, current)
                is_wknd = current.weekday() >= 5
                is_hol  = current in holidays
                hol_nm  = holidays.get(current)
                is_acad = not is_wknd and not is_hol

                if is_hol:    day_type = "วันหยุดนักขัตฤกษ์"
                elif is_wknd: day_type = "วันหยุด"
                else:         day_type = "วันทำการ"

                rows.append((
                    date_id, campus_id, faculty_id, academic_year, sem,
                    week_num, is_acad, is_hol, hol_nm, day_type,
                ))
                if current.weekday() == 6:
                    week_num += 1
                current += timedelta(days=1)

            execute_values(cur, """
                INSERT INTO fact_academic_calendar
                    (date_id,campus_id,faculty_id,academic_year,semester,
                     week_of_semester,is_academic_day,is_holiday,holiday_name,day_type)
                VALUES %s
                ON CONFLICT (date_id,campus_id,faculty_id,day_type) DO UPDATE SET
                    academic_year    = EXCLUDED.academic_year,
                    semester         = EXCLUDED.semester,
                    week_of_semester = EXCLUDED.week_of_semester,
                    is_academic_day  = EXCLUDED.is_academic_day,
                    is_holiday       = EXCLUDED.is_holiday,
                    holiday_name     = EXCLUDED.holiday_name
            """, rows)
            total += len(rows)
            print(f"  Semester {sem}: {start_d} → {end_d}  ({len(rows)} days)")

        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"  [ERROR] DB: {exc}")
        raise
    finally:
        conn.close()

    return total


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import PSU calendar PDFs via Gemini")
    parser.add_argument("--year", type=int, default=None, help="Filter by BE academic year")
    args = parser.parse_args()

    pdf_files = sorted(Path(INPUT_DIR).glob("*.pdf"))
    if not pdf_files:
        sys.exit(f"No PDF files found in {INPUT_DIR}")

    print(f"Found {len(pdf_files)} PDF(s) in {INPUT_DIR}")
    grand_total = 0

    for pdf_path in pdf_files:
        print(f"\n[{pdf_path.name}]")
        try:
            data = gemini_extract(str(pdf_path))
            yr   = data.get("academic_year")
            if args.year and yr != args.year:
                print(f"  Skip (year={yr}, filter={args.year})")
                continue
            print(f"  campus={data.get('campus')}  year={yr}  events={len(data.get('events', []))}")
            n = insert_calendar(data)
            grand_total += n
            print(f"  → {n} rows inserted")
        except Exception as exc:
            print(f"  [ERROR] {exc}")
            continue

    print(f"\nDone. Total rows inserted: {grand_total}")


if __name__ == "__main__":
    main()
