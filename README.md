# Garmin Connect & AI Running Coach LINE Bot

ระบบผู้ช่วยและโค้ชวิ่งส่วนตัวผ่าน LINE Bot ที่เชื่อมต่อข้อมูลกับ **Garmin Connect** และขับเคลื่อนด้วย **Google Gemini AI** สำหรับวิเคราะห์สมรรถภาพ, แนะนำการฝึกซ้อมตามเกณฑ์แลคเตท (Lactate Threshold), ตรวจสอบตารางซ้อมประจำวัน, รายงานสภาพความพร้อมร่างกาย (Training Readiness/Sleep) และบันทึกน้ำหนักอัตโนมัติ

---

## 🌟 ฟีเจอร์หลัก (Key Features)

* 🏃 **เช็กตารางซ้อมล่วงหน้า (Garmin Calendar Workouts)**:
  * ดึงตารางซ้อมของวันนี้, วันพรุ่งนี้ และตารางซ้อมล่วงหน้า 7 วันที่ตั้งไว้ในปฏิทิน Garmin
  * แสดงรายละเอียดสเต็ปการซ้อม (Warm up, Intervals, Recovery, Cool down)
* 💡 **วิเคราะห์ผลการวิ่งเจาะลึกระดับ Laps/Intervals**:
  * จำแนกช่วงวิ่งจริง (Work Intervals) ออกจากช่วงพัก (Recovery) และคำนวณเพซจริงเทียบกับเป้าหมาย
  * ประเมินความหนักเทียบกับจุดเกณฑ์แลคเตทเฉพาะบุคคล (LT1 / LT2)
* ☀️ **แจ้งเตือนตารางซ้อมอัตโนมัติประจำวัน (Daily Push Notification)**:
  * ส่งสรุปตารางซ้อมและคำแนะนำจากโค้ช AI ทุกเที่ยงวัน (12:00 น.) ไปยัง LINE ของนักกีฬา
* 💬 **ปรึกษาโค้ช AI ส่วนตัว (Personal Running Coach)**:
  * ตอบคำถามด้านการฝึกซ้อม, สภาพร่างกาย, การเตรียมตัวแข่ง หรือการปรับตารางเมื่อร่างกายล้า
* ⚖️ **บันทึกน้ำหนักเข้า Garmin Connect**:
  * พิมพ์บอกน้ำหนักใน LINE (เช่น `น้ำหนัก 65.5` หรือ `หนัก 68 kg`) ระบบจะบันทึกลง Garmin Connect ทันที
* 📝 **สร้างตารางซ้อมและ Sync เข้านาฬิกา Garmin**:
  * ให้ AI ช่วยร่างโปรแกรมซ้อมตามระยะและเป้าหมาย พร้อมปุ่มกดสั่ง Sync เข้า Workout Service ของ Garmin ได้โดยตรง

---

## 📁 โครงสร้างโปรเจกต์ (Project Structure)

```text
garmin-connect/
├── connect_garmin.py       # สคริปต์ล็อกอินรอบแรกเพื่อสร้าง Session Token
├── export_tokens.py        # สคริปต์ Export Token เป็น Base64 สำหรับใส่ใน Cloud Environment
├── line_bot.py             # เซิร์ฟเวอร์หลัก (FastAPI Webhook Server & Background Scheduler)
├── sync_token.py           # สคริปต์สำหรับ Refresh Token และส่งขึ้น Cloud อัตโนมัติ
├── athlete_profile.json    # ค่าเกณฑ์แลคเตท (LT1/LT2) และโซนหัวใจ/เพซเฉพาะบุคคล
├── requirements.txt        # รายการ Python Libraries
├── .env.example            # ตัวอย่างการกำหนดค่า Environment Variables
└── README.md
```

---

## 🚀 ขั้นตอนการติดตั้งและเริ่มใช้งาน (Quick Setup)

### 1. โคลนคลังโค้ดและสร้าง Virtual Environment

```bash
git clone https://github.com/<your-username>/<your-repo-name>.git
cd <your-repo-name>

# สร้างและเปิดใช้งาน Virtual Environment
python3 -m venv venv
source venv/bin/activate    # สำหรับ macOS / Linux
# venv\Scripts\activate     # สำหรับ Windows

# ติดตั้ง Dependencies
pip install -r requirements.txt
```

---

### 2. ตั้งค่า Environment Variables (`.env`)

คัดลอกไฟล์ตัวอย่างและใส่ค่า Credentials ของตนเอง:

```bash
cp .env.example .env
```

แก้ไขไฟล์ `.env` โดยระบุค่าดังนี้:

| ตัวแปร | คำอธิบาย |
| :--- | :--- |
| `GARMIN_EMAIL` | อีเมลบัญชี Garmin Connect |
| `GARMIN_PASSWORD` | รหัสผ่านบัญชี Garmin Connect |
| `LINE_CHANNEL_SECRET` | Channel Secret จาก [LINE Developers Console](https://developers.line.biz/) |
| `LINE_CHANNEL_ACCESS_TOKEN` | Channel Access Token จาก LINE Developers Console |
| `LINE_ALLOWED_USER_ID` | LINE User ID ของตนเอง (ขึ้นต้นด้วย `U...`) เพื่อรับ Push Notification |
| `GEMINI_API_KEY` | API Key จาก [Google AI Studio](https://aistudio.google.com/) |
| `TOKEN_UPDATE_SECRET` | คีย์รหัสผ่านสำหรับ Endpoint `/update-token` (ตั้งเป็นข้อความลับอะไรก็ได้) |
| `RENDER_APP_URL` | URL ของ Web Service ที่โฮสต์อยู่ (กรณี Deploy บน Cloud) |

---

### 3. ยืนยันตัวตน Garmin Connect (First-time Login)

เนื่องจากระบบป้องกันของ Garmin Connect (Cloudflare WAF) บล็อกการล็อกอินจาก Cloud IP (Error 429) **ต้องล็อกอินผ่านคอมพิวเตอร์ส่วนบุคคลในเน็ตบ้านครั้งแรก**:

```bash
python connect_garmin.py
```

* หากเปิด Two-Factor Authentication (MFA) ให้กรอกรหัสยืนยันที่ได้รับทางอีเมล/SMS ใน Terminal
* เมื่อสำเร็จ ระบบจะบันทึก Session Token ลงในโฟลเดอร์ `.garminconnect/`

---

### 4. ปรับแต่งข้อมูลนักกีฬา (`athlete_profile.json`)

แก้ไขค่าแลคเตทและโซนความเร็วให้ตรงกับผลทดสอบของตนเองในไฟล์ `athlete_profile.json`:
* `LT1`: เพซและ Heart Rate จุดแอโรบิกพื้นฐาน (Aerobic Threshold)
* `LT2`: เพซและ Heart Rate จุดเกณฑ์แลคเตท (Threshold / Tempo)
* `training_zones_by_objective`: ช่วงเพซและโซนหัวใจสำหรับ Easy Run, Steady, Tempo และ Interval

---

## 💻 การทดสอบและรันในเครื่อง (Local Development)

### 1. รัน Webhook Server

```bash
python line_bot.py
```
*(เซิร์ฟเวอร์จะเริ่มทำงานที่ `http://localhost:8000`)*

### 2. เปิด Public Webhook URL ด้วย Cloudflare Tunnel (หรือ ngrok)

```bash
# ติดตั้ง cloudflared (กรณี macOS)
brew install cloudflared

# เปิด Tunnel ไปที่ Port 8000
cloudflared tunnel --url http://localhost:8000
```
จะได้รับ HTTPS URL ตัวอย่างเช่น: `https://xxxx.trycloudflare.com`

### 3. ผูก Webhook ใน LINE Developers Console
1. ไปที่แท็บ **Messaging API** ใน [LINE Developers Console](https://developers.line.biz/)
2. ในช่อง **Webhook URL** ให้ใส่:
   ```text
   https://xxxx.trycloudflare.com/callback
   ```
3. เปิดสวิตช์ **Use webhook** ให้เป็น **ON**
4. กดปุ่ม **Verify** เพื่อตรวจสอบความถูกต้อง

---

## ☁️ การ Deploy บน Cloud (Render / Railway / VPS)

### 1. Export Session Token สำหรับ Cloud

รันสคริปต์เพื่อแปลง Session Token ในเครื่องเป็น Base64:

```bash
python export_tokens.py
```
คัดลอกข้อความยาวที่ได้ ไปใส่ในตัวแปร `GARMIN_TOKENS_BASE64` บน Environment Settings ของ Cloud Provider (เช่น Render)

### 2. ตั้งค่าการ Deploy บน Render
* **Build Command**: `pip install -r requirements.txt`
* **Start Command**: `uvicorn line_bot.py:app --host 0.0.0.0 --port $PORT`

### 3. การอัปเดต Token เมื่อ Session หลุด (Token Auto-Sync)
เนื่องจากเซิร์ฟเวอร์บน Cloud อาจโดน Garmin บล็อก 429 เมื่อทำการต่ออายุ Session:
* สามารถรัน `python sync_token.py` บนเครื่องคอมพิวเตอร์ที่บ้าน เพื่อทำการ Refresh Token ผ่าน Home IP แล้วส่งเข้าไปอัปเดตบน Render ผ่าน Endpoint `/update-token` ได้ทันทีแบบ Zero-Downtime โดยไม่ต้อง Restart เซิร์ฟเวอร์

---

## 📱 ตัวอย่างคำสั่งใช้งานใน LINE Bot

| หมวดหมู่ | ตัวอย่างคำสั่งที่พิมพ์ | ผลลัพธ์ |
| :--- | :--- | :--- |
| **ตารางซ้อมวันนี้** | `วันนี้ซ้อมอะไร`, `ขอตารางวันนี้`, `ตารางซ้อม` | ดึงแผนซ้อมวันนี้ + คำแนะนำจากโค้ช AI |
| **ตารางซ้อมวันพรุ่งนี้** | `พรุ่งนี้ซ้อมอะไร`, `พน.ซ้อมอะไร`, `ตารางพรุ่งนี้` | ตรวจสอบตารางวันพรุ่งนี้และคำแนะนำการเตรียมตัว |
| **ตารางซ้อม 7 วัน** | `ตารางล่วงหน้า 7 วัน`, `ตาราง 7 วัน` | สรุปภาพรวมโปรแกรมซ้อมทั้งสัปดาห์ |
| **สรุปผลการวิ่ง/สุขภาพ** | `สถานะ`, `วันนี้`, `สรุป` | สรุปก้าว, HR, การนอน, Readiness และวิเคราะห์ผลวิ่งเจาะลึก Laps |
| **ประวัติการวิ่ง** | `ประวัติวิ่ง`, `วิ่งล่าสุด` | แสดงสถิติการวิ่ง 5 ครั้งล่าสุด |
| **บันทึกน้ำหนัก** | `น้ำหนัก 68.5`, `หนัก 70 kg` | บันทึกค่าน้ำหนักลง Garmin Connect ทันที |
| **ปรึกษาโค้ช AI** | `พัก 2 นาทีตอน interval ควรเดินหรือจ็อกดี` | AI วิเคราะห์ร่วมกับข้อมูลร่างกายจริงและตารางซ้อม |
| **วิธีใช้งาน** | `เมนู`, `help`, `วิธีใช้` | แสดงการ์ดเมนูคำสั่งทั้งหมด |

---

## 🔒 ความปลอดภัยและข้อควรระวัง (Security & Best Practices)

* **ห้าม Commit ไฟล์ `.env` หรือโฟลเดอร์ `.garminconnect/` เข้า Git เด็ดขาด** (ไฟล์ `.gitignore` ได้กำหนดการยกเว้นไว้แล้ว)
* ควรเก็บ `TOKEN_UPDATE_SECRET` เป็นความลับเพื่อป้องกันการส่ง Token ปลอมเข้ามายังเซิร์ฟเวอร์
* การเข้าถึง Garmin Connect เป็นการใช้งานผ่าน Reverse-Engineered API (`garth` / `garminconnect`) ควรหลีกเลี่ยงการส่งคำขอถี่เกินไปเพื่อป้องกันการติด Rate Limit (HTTP 429)
