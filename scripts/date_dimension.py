import os
import yaml
import pandas as pd
from datetime import date, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
_CFG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CFG_PATH, encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

OUTPUT_DIR = _cfg["paths"]["output_dir"]
HOL_FILE   = _cfg["paths"]["holiday_file"]

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

SEMESTER_RANGES = [
    {"ภาคเรียน": 1, "เปิด": date(2025, 6, 23), "ปิด": date(2025, 10, 27)},
    {"ภาคเรียน": 2, "เปิด": date(2025, 11, 17), "ปิด": date(2026, 3, 21)},
    {"ภาคเรียน": 3, "เปิด": date(2026, 4, 16), "ปิด": date(2026, 6,  7)},
]

# ── โหลดวันหยุดจากไฟล์ Excel ──────────────────────
hol_path = Path(HOL_FILE)
if not hol_path.exists():
    raise FileNotFoundError(
        f"ไม่พบไฟล์วันหยุด: {hol_path}\n"
        f"กรุณาตรวจสอบ config.yaml → paths.holiday_file"
    )

hol_df = pd.read_excel(hol_path, sheet_name="วิทยาเขตหาดใหญ่")

# กรองเฉพาะปี 2568
hol_2568 = hol_df[hol_df["ปี พ.ศ."] == 2568].copy()

# แปลง column วันที่ให้เป็น date object (ชื่อ column จริงในไฟล์คือ "วันที่ (พ.ศ.)")
hol_2568["วันที่"] = pd.to_datetime(hol_2568["วันที่ (พ.ศ.)"], dayfirst=True).dt.date

# สร้าง dict  {date: ชื่อวันหยุด}
HOLIDAYS = dict(zip(hol_2568["วันที่"], hol_2568["ชื่อวันหยุด"]))

print(f"โหลดวันหยุดหาดใหญ่ปี 2568 ได้ {len(HOLIDAYS)} วัน")
for d, name in sorted(HOLIDAYS.items()):
    print(f"  {d}  {name}")
print()

# ── สร้าง Date Dimension ───────────────────────────
rows    = []
date_id = 1

for sem in SEMESTER_RANGES:
    current  = sem["เปิด"]
    end      = sem["ปิด"]
    sem_num  = sem["ภาคเรียน"]
    week_num = 1
    day_num  = 1

    while current <= end:
        be       = current.year + 543
        wday     = current.weekday()
        is_wknd  = wday >= 5
        is_hol   = current in HOLIDAYS
        hol_name = HOLIDAYS.get(current, "")
        quarter  = (current.month - 1) // 3 + 1

        if is_hol:
            day_type = "วันหยุดนักขัตฤกษ์"
        elif is_wknd:
            day_type = "วันหยุด"
        else:
            day_type = "วันทำการ"

        rows.append({
            "date_id":           date_id,
            "วันที่":             current.strftime("%d/%m/%Y"),
            "ปี_คศ":             current.year,
            "ปี_พศ":             be,
            "ปีการศึกษา":        2568,
            "ภาคเรียน":          sem_num,
            "เดือน":             THAI_MONTHS_FULL[current.month],
            "เดือน_ตัวเลข":      current.month,
            "เดือน_ย่อ":         THAI_MONTHS_SHORT[current.month],
            "วันที่_ตัวเลข":     current.day,
            "วัน":               THAI_DAYS[wday],
            "วัน_ตัวเลข":        wday + 1,
            "ไตรมาส":            f"Q{quarter}",
            "สัปดาห์ที่_ของภาค": week_num,
            "วันที่_ของภาค":     day_num,
            "สถานะเทอม":         f"เทอม {sem_num}",
            "is_academic_day":   not is_wknd and not is_hol,
            "is_weekend":        is_wknd,
            "is_holiday":        is_hol,
            "ชื่อวันหยุด":       hol_name,
            "ประเภทวัน":         day_type,
            "วิทยาเขต":          "หาดใหญ่",
        })

        date_id += 1
        day_num += 1
        if wday == 6:
            week_num += 1
        current += timedelta(days=1)

df = pd.DataFrame(rows)
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
out = Path(OUTPUT_DIR) / "date_dimension.xlsx"
df.to_excel(out, index=False)

total    = len(df)
workdays = len(df[df["ประเภทวัน"] == "วันทำการ"])
weekends = len(df[df["is_weekend"] == True])
holidays = len(df[df["is_holiday"] == True])

print(f"Total rows: {total}")
print(f"Workdays: {workdays}")
print(f"Weekends: {weekends}")
print(f"Holidays: {holidays}")
print(f"Saved to: {out}")
