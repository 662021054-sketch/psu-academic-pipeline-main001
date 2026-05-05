import logging
import os
import json
import re as _re
import base64
from pathlib import Path
import pandas as pd
import yaml
import pdfplumber
import fitz
from groq import Groq
from dotenv import load_dotenv

# ── 1. Config & Env ───────────────────────────────────────────────────────────
load_dotenv()  # ดึง GEMINI_API_KEY จากไฟล์ .env
_CFG_PATH = Path(__file__).parent.parent / "config.yaml"

with open(_CFG_PATH, encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)

# ตรวจสอบ Path: ถ้าเป็น Windows ให้ใช้ path ปกติ ถ้า Docker ให้ใช้ /app
def get_path(key):
    path = _cfg["paths"][key]
    return path.replace("/app/", "") if not os.path.exists(path) else path

INPUT_DIR  = get_path("input_dir")
OUTPUT_DIR = get_path("output_dir")
LOGS_DIR   = get_path("logs_dir")

# ตั้งค่า Groq API
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── 2. Logging ────────────────────────────────────────────────────────────────
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
_lc = _cfg.get("logging", {})
logging.basicConfig(
    level=getattr(logging, _lc.get("level", "INFO"), logging.INFO),
    format=_lc.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    datefmt=_lc.get("date_format", "%Y-%m-%d %H:%M:%S"),
    handlers=[
        logging.FileHandler(Path(LOGS_DIR) / "extract.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("extract")

# ── 3. Groq Extraction Function ───────────────────────────────────────────────
_EXTRACT_PROMPT = """คุณคือ AI ผู้เชี่ยวชาญด้านการอ่านปฏิทินการศึกษาของ มหาวิทยาลัยสงขลานครินทร์ (PSU)
ช่วยอ่านข้อมูลที่ให้มา แล้วสกัดข้อมูลกิจกรรมทั้งหมดออกมาเป็น JSON Array

โครงสร้าง JSON ที่ต้องการ:
[
  {
    "ภาคเรียน": 1,
    "เหตุการณ์": "ชื่อกิจกรรม",
    "วัน": 15,
    "เดือน": 8,
    "ชื่อเดือน": "สิงหาคม",
    "ปี_พศ": 2568,
    "ปี_คศ": 2025
  }
]

เงื่อนไขสำคัญ:
1. หากเป็นช่วงวันที่ (เช่น 1-10 ส.ค.) ให้แตกข้อมูลออกมาเป็นรายวัน แยกเป็นแต่ละ Object
2. หากไม่มีระบุปี ให้ใช้ปี พ.ศ. 2568 (ค.ศ. 2025) เป็นหลัก
3. ตอบกลับเป็น JSON บริสุทธิ์เท่านั้น ห้ามมีคำบรรยายใดๆ"""


def extract_with_groq(file_path):
    logger.info(f"🚀 เริ่มประมวลผลด้วย Groq: {file_path.name}")
    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY ไม่ได้ตั้งค่า")
        return []
    try:
        client = Groq(api_key=GROQ_API_KEY)
        ext = file_path.suffix.lower()

        if ext == ".pdf":
            # ลอง pdfplumber ก่อน
            text = ""
            try:
                with pdfplumber.open(str(file_path)) as pdf:
                    text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            except Exception:
                pass

            if text.strip():
                resp = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": _EXTRACT_PROMPT + "\n\nข้อความจากเอกสาร:\n" + text[:8000]}],
                    temperature=0.0,
                )
            else:
                # PDF สแกน → แปลงเป็นรูป
                doc = fitz.open(str(file_path))
                content = [{"type": "text", "text": _EXTRACT_PROMPT}]
                for i, page in enumerate(doc):
                    if i >= 4:
                        break
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                    b64 = base64.b64encode(pix.tobytes("png")).decode()
                    content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                resp = client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    messages=[{"role": "user", "content": content}],
                    temperature=0.0,
                )
        else:
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext.lstrip("."), "image/png")
            b64  = base64.b64encode(file_path.read_bytes()).decode()
            resp = client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": _EXTRACT_PROMPT},
                ]}],
                temperature=0.0,
            )

        json_text = resp.choices[0].message.content.strip()
        json_text = _re.sub(r'```[a-z]*\n?', '', json_text)
        json_text = _re.sub(r'\n?```', '', json_text).strip()
        return json.loads(json_text)

    except Exception as e:
        logger.error(f"❌ เกิดข้อผิดพลาดตอนเรียก Groq: {str(e)}")
        return []

# ── 4. Main Process ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ค้นหาไฟล์ (PDF, JPG, PNG)
    extensions = ("*.pdf", "*.jpg", "*.jpeg", "*.png")
    all_files = []
    for ext in extensions:
        all_files.extend(Path(INPUT_DIR).glob(ext))

    # กรองเฉพาะไฟล์ที่มีคำว่า PATITIN ตามเงื่อนไขใน Config
    target_files = [f for f in all_files if "PATITIN" in f.name.upper()]

    if not target_files:
        logger.error(f"📂 ไม่พบไฟล์ที่มีคำว่า 'PATITIN' ในโฟลเดอร์ {INPUT_DIR}")
        print(f"DEBUG: Path ที่กำลังหาคือ {os.path.abspath(INPUT_DIR)}")
        raise SystemExit(0)

    # เลือกไฟล์ใหม่ล่าสุด
    target_file = max(target_files, key=lambda f: f.stat().st_mtime)
    logger.info(f"📄 ไฟล์ที่จะประมวลผล: {target_file.name}")

    # รัน Groq
    extracted_data = extract_with_groq(target_file)

    # สร้าง DataFrame และเซฟเป็น Excel
    if extracted_data:
        df = pd.DataFrame(extracted_data)
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        
        output_file = Path(OUTPUT_DIR) / "dates_raw.xlsx"
        df.to_excel(output_file, index=False)
        
        logger.info("OK: Saved %d rows to %s", len(df), output_file)
    else:
        logger.warning("No data extracted from file")
