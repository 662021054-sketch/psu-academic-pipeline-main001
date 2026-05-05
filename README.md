# PSU Academic Calendar Pipeline

ระบบสร้างและจัดการ Date Dimension จากปฏิทินการศึกษาของมหาวิทยาลัยสงขลานครินทร์ (ม.อ.)
รองรับหลายวิทยาเขต หลายคณะ และหลายปีการศึกษา

## Features

- อ่านปฏิทินการศึกษาจากไฟล์ PDF (ทั้ง text-based และ image-based ผ่าน OCR)
- สร้าง Date Dimension แยกตามคณะ (Normal, BBA, Dent, Med) และวิทยาเขต (หาดใหญ่ ปัตตานี ภูเก็ต ตรัง สุราษฎร์ฯ)
- คำนวณวันหยุดทางพุทธศาสนาอัตโนมัติ (มาฆบูชา วิสาขบูชา อาสาฬหบูชา เข้าพรรษา)
- Export ผลลัพธ์เป็น Excel และบันทึกลง PostgreSQL
- ตรวจจับ PDF ใหม่ใน input folder และรัน pipeline อัตโนมัติ
- Admin UI ผ่าน Streamlit สำหรับดูและดาวน์โหลดข้อมูล
- รองรับ Docker Compose สำหรับ production deployment

## Architecture

```
input/          ← วาง PDF ปฏิทินที่นี่
  PATITIN*.pdf      ← ปฏิทินมหาวิทยาลัย (หาดใหญ่)
  *BBA*.pdf         ← ปฏิทินคณะ BBA
  *MED*.pdf         ← ปฏิทินคณะแพทย์
  *DENT*.pdf        ← ปฏิทินคณะทันตแพทย์
  วันหยุด_ม.อ._.xlsx ← ไฟล์วันหยุดแยกวิทยาเขต

scripts/
  watcher.py    ← คอยตรวจ input folder, trigger pipeline
  extract.py    ← อ่าน PDF ล่าสุด → dates_raw.xlsx
  pipeline.py   ← สร้าง Date Dimension ทุกปี → Excel + PostgreSQL

admin/
  app.py        ← Streamlit Admin UI

output/         ← ไฟล์ Excel ที่สร้าง
logs/           ← Log files (pipeline.log, extract.log, watcher.log)
```

## Quick Start (Docker)

```bash
# 1. Copy PDF files ไปที่ input/
cp your_calendars/*.pdf input/

# 2. Start all services
docker compose up -d

# 3. ดู logs
docker compose logs -f pipeline

# 4. เปิด Admin UI ที่ http://localhost:8501
```

## Environment Variables (DB)

| Variable      | Default      | Description            |
|---------------|--------------|------------------------|
| `DB_HOST`     | `postgres`   | PostgreSQL host        |
| `DB_PORT`     | `5432`       | PostgreSQL port        |
| `DB_NAME`     | `psu_academic` | Database name        |
| `DB_USER`     | `admin`      | Database user          |
| `DB_PASSWORD` | `psu2024`    | Database password      |

## Output Files

| File pattern                              | Description                    |
|-------------------------------------------|--------------------------------|
| `date_dim_{year}_faculty_{name}.xlsx`     | Date dim รายคณะ               |
| `date_dimension_{year}_by_faculty.xlsx`   | รวมทุกคณะในปีเดียว            |
| `date_dim_{year}_campus_{name}.xlsx`      | Date dim รายวิทยาเขต          |

## Project Structure

```
academic_pipeline/
├── config.yaml           ← ค่า config ทั้งหมด (paths, logging, ฯลฯ)
├── docker-compose.yml    ← Pipeline + PostgreSQL + Admin UI
├── Dockerfile            ← Pipeline container
├── Dockerfile.admin      ← Streamlit admin container
├── requirements.txt      ← Python dependencies
├── input/                ← PDF input files
├── output/               ← Excel output files
├── logs/                 ← Log files
├── scripts/
│   ├── pipeline.py       ← Main pipeline
│   ├── extract.py        ← PDF extractor
│   ├── watcher.py        ← File watcher
│   └── date_dimension.py ← Standalone prototype
└── admin/
    └── app.py            ← Streamlit admin UI
```

## Local Development

ดูรายละเอียดการติดตั้งและรันบนเครื่อง Windows ที่ [SETUP.md](SETUP.md)
