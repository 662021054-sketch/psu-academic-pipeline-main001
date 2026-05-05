# Setup Guide

คู่มือติดตั้งและใช้งานระบบ PSU Academic Calendar Pipeline ตั้งแต่เริ่มต้น

---

## Prerequisites

### วิธีที่ 1: รันผ่าน Docker (แนะนำสำหรับ Production)

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac/Linux)
- ไม่ต้องติดตั้ง Python หรือ dependency อื่น ๆ เพิ่มเติม

### วิธีที่ 2: รันบนเครื่อง Windows โดยตรง

- Python 3.9+  (แนะนำใช้ [Miniconda](https://docs.conda.io/en/latest/miniconda.html))
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki)  พร้อม language pack **tha**
- [Poppler](https://github.com/oschwartz10612/poppler-windows/releases)  (สำหรับ pdf2image)
- PostgreSQL 15 (optional — สำหรับบันทึก DB)

---

## วิธีที่ 1: Docker Compose

### 1. Clone หรือ copy project

```bash
git clone <repo-url> academic_pipeline
cd academic_pipeline
```

### 2. วาง input files

```
input/
  PATITIN2566.pdf
  PATITIN2567.pdf
  PATITIN2568.pdf
  PATITIN2569.pdf
  2024_BBA_Calendar.pdf
  2025_BBA_Calendar_Change.pdf
  2024_MED_Calendar.pdf
  2025_MED_Calendar.pdf
  2024_DENT_Calendar (1).pdf
  2025_DENT_Calendar (1).pdf
  วันหยุด_ม.อ._แยกวิทยาเขต.xlsx
  holidays_template.xlsx
```

### 3. (Optional) แก้ไข config

แก้ `config.yaml` ถ้าต้องการปรับ log level หรือ cooldown:

```yaml
logging:
  level: DEBUG   # DEBUG | INFO | WARNING | ERROR
watcher:
  cooldown_seconds: 60
```

แก้ DB credentials ใน `docker-compose.yml` (ถ้าต้องการ):

```yaml
environment:
  - DB_PASSWORD=your_password
```

### 4. Start services

```bash
docker compose up -d
```

Services ที่จะรัน:
| Service     | Port  | Description                  |
|-------------|-------|------------------------------|
| `pipeline`  | —     | Watcher + pipeline runner    |
| `postgres`  | 5432  | PostgreSQL database          |
| `admin`     | 8501  | Streamlit Admin UI           |

### 5. ตรวจสอบ

```bash
# ดู logs แบบ real-time
docker compose logs -f pipeline

# ดูสถานะ containers
docker compose ps

# เปิด Admin UI
# http://localhost:8501
```

### 6. รัน pipeline ด้วยมือ (ไม่ต้องรอ watcher)

```bash
docker compose exec pipeline python scripts/pipeline.py
```

### 7. หยุดระบบ

```bash
docker compose down          # หยุดและลบ containers
docker compose down -v       # หยุด + ลบ database volume ด้วย
```

---

## วิธีที่ 2: รันบน Windows โดยตรง

### 1. ติดตั้ง Tesseract OCR

1. ดาวน์โหลด installer จาก https://github.com/UB-Mannheim/tesseract/wiki
2. เลือกติดตั้ง **Additional language data** → Thai
3. จด path ที่ติดตั้ง เช่น `C:\Program Files\Tesseract-OCR\tesseract.exe`

### 2. ติดตั้ง Poppler

1. ดาวน์โหลดจาก https://github.com/oschwartz10612/poppler-windows/releases
2. แตก zip ไปที่ `C:\poppler\`
3. จด path ของ `bin/` เช่น `C:\poppler\poppler-25.12.0\Library\bin`

### 3. สร้าง Python environment

```bash
conda create -n academic_pipeline python=3.9
conda activate academic_pipeline
pip install -r requirements.txt
```

### 4. แก้ config.yaml สำหรับ Windows

แก้ไข `config.yaml` ให้ใช้ Windows paths:

```yaml
paths:
  input_dir: C:/academic_pipeline/input
  output_dir: C:/academic_pipeline/output
  logs_dir: C:/academic_pipeline/logs
  holiday_file: C:/academic_pipeline/input/วันหยุด_ม.อ._แยกวิทยาเขต.xlsx
  holidays_template: C:/academic_pipeline/input/holidays_template.xlsx

tesseract:
  cmd: C:/Program Files/Tesseract-OCR/tesseract.exe
  poppler_path: C:/poppler/poppler-25.12.0/Library/bin
```

### 5. (Optional) ตั้งค่า environment variables สำหรับ DB

```powershell
$env:DB_HOST     = "localhost"
$env:DB_PORT     = "5432"
$env:DB_NAME     = "psu_academic"
$env:DB_USER     = "admin"
$env:DB_PASSWORD = "psu2024"
```

ถ้าไม่ตั้ง env vars ระบบจะข้าม DB save และบันทึกเฉพาะ Excel

### 6. รัน pipeline

```bash
# รันครั้งเดียว
python scripts/pipeline.py

# รัน watcher (ตรวจ input folder อัตโนมัติ)
python scripts/watcher.py
```

---

## โครงสร้างไฟล์ Input

| ไฟล์                                  | หมายเหตุ                              |
|---------------------------------------|---------------------------------------|
| `PATITIN{ปีพศ}.pdf`                   | ปฏิทินหลักของมหาวิทยาลัย (ต้องมี)   |
| `*BBA*.pdf`                           | ปฏิทินคณะบริหารธุรกิจ                |
| `*MED*.pdf`                           | ปฏิทินคณะแพทย์                       |
| `*DENT*.pdf`                          | ปฏิทินคณะทันตแพทย์                   |
| `วันหยุด_ม.อ._แยกวิทยาเขต.xlsx`      | วันหยุดแต่ละวิทยาเขต (ต้องมี)        |
| `holidays_template.xlsx`              | วันหยุดเพิ่มเติม (optional)           |

---

## โครงสร้าง holidays Excel

### `วันหยุด_ม.อ._แยกวิทยาเขต.xlsx`

แต่ละ sheet = วิทยาเขต (เช่น `วิทยาเขตหาดใหญ่`, `วิทยาเขตปัตตานี`, ...)

| คอลัมน์        | ตัวอย่าง          |
|----------------|-------------------|
| `ปี พ.ศ.`      | 2567              |
| `วันที่ (พ.ศ.)`| 01/01/2567        |
| `ชื่อวันหยุด`  | วันปีใหม่         |

### `holidays_template.xlsx`

| คอลัมน์      | ตัวอย่าง      |
|--------------|---------------|
| `ปี_พศ`      | 2567          |
| `วันที่`     | 2024-01-01    |
| `ชื่อวันหยุด`| วันปีใหม่     |

---

## Logs

Log files จะถูกสร้างที่ `logs/`:

| File              | Script        |
|-------------------|---------------|
| `pipeline.log`    | pipeline.py   |
| `extract.log`     | extract.py    |
| `watcher.log`     | watcher.py    |

---

## Database Schema

ตาราง `date_dimension` ใน PostgreSQL:

| Column            | Type      | Description                   |
|-------------------|-----------|-------------------------------|
| `id`              | BIGSERIAL | Primary key                   |
| `date_str`        | VARCHAR   | วันที่ (dd/mm/yyyy)            |
| `year_ce`         | INTEGER   | ปี ค.ศ.                       |
| `year_be`         | INTEGER   | ปี พ.ศ.                       |
| `academic_year`   | INTEGER   | ปีการศึกษา (พ.ศ.)             |
| `semester`        | INTEGER   | ภาคเรียน (1/2/3, 0=ปิดภาค)   |
| `is_academic_day` | BOOLEAN   | วันทำการในภาคการศึกษา         |
| `is_weekend`      | BOOLEAN   | วันเสาร์-อาทิตย์              |
| `is_holiday`      | BOOLEAN   | วันหยุดนักขัตฤกษ์             |
| `campus`          | TEXT      | วิทยาเขต                      |
| `faculty`         | TEXT      | คณะ ('' = ไม่ระบุ)            |

---

## Troubleshooting

**Pipeline ไม่ parse PDF ได้**
- ตรวจสอบว่า PDF อยู่ใน input/ และชื่อไฟล์ถูกต้อง (PATITIN*.pdf)
- ถ้าเป็น scanned PDF ให้ตรวจสอบ Tesseract ติดตั้งถูกต้อง

**DB save ไม่ทำงาน**
- ตรวจสอบ environment variables DB_HOST, DB_NAME, DB_USER, DB_PASSWORD
- ดู error ใน logs/pipeline.log

**Watcher ไม่ตอบสนอง**
- ตรวจสอบ watch_folder ใน config.yaml ตรงกับ input directory
- เพิ่ม log level เป็น DEBUG เพื่อดู debug messages
