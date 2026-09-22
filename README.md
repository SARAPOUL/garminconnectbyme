# Garmin Connect & AI Running Coach

โปรเจกต์สำหรับเชื่อมต่อ Garmin Connect เพื่อวิเคราะห์ข้อมูลสุขภาพ, การฝึกซ้อมวิ่ง และโต้ตอบผ่าน **Antigravity Custom Skill** หรือ **LINE Bot ส่วนตัว**

---

## 📁 โครงสร้างโปรเจกต์
```text
garmin-connect/
├── .agents/skills/garmin/  # Custom Skill สำหรับ Antigravity (/garmin)
│   ├── SKILL.md
│   └── scripts/garmin_cli.py
├── connect_garmin.py       # สคริปต์ยืนยันตัวตนรอบแรกและสร้าง Session Token
├── line_bot.py             # เซิร์ฟเวอร์ Webhook สำหรับ LINE Bot (FastAPI)
├── requirements.txt        # รายการ dependencies
├── .env                    # ตั้งค่า Credentials (LINE, Gemini, Garmin)
└── README.md
```

---

## 🔑 ขั้นตอนการเริ่มใช้งาน

### 1. ยืนยันตัวตน Garmin Connect (รอบแรก)
รันคำสั่งใน Terminal เพื่อสร้าง Session Token เก็บไว้ในเครื่อง (ทำเพียงครั้งเดียว):
```bash
cd /Users/pornthepphadungmai/.gemini/antigravity/scratch/garmin-connect
source venv/bin/activate
python connect_garmin.py
```

---

## 💬 ใช้งานผ่าน LINE Bot ส่วนตัว

### 1. ตั้งค่า API Keys ใน `.env`
ไฟล์ `.env` ได้บันทึก `LINE_CHANNEL_SECRET` และ `LINE_CHANNEL_ACCESS_TOKEN` ไว้แล้ว
หากต้องการให้ AI วิเคราะห์การซ้อมวิ่ง ให้ใส่ `GEMINI_API_KEY` เพิ่มเติม:
```env
GEMINI_API_KEY=your_gemini_api_key_here
```

### 2. รันเซิร์ฟเวอร์ LINE Bot
```bash
source venv/bin/activate
python line_bot.py
```
*(เซิร์ฟเวอร์จะรันอยู่ที่ `http://localhost:8000`)*

### 3. เปิด Public HTTPS URL ด้วย Cloudflare Tunnel (ในอีก Terminal หนึ่ง)
ติดตั้ง `cloudflared` (หากยังไม่มี):
```bash
brew install cloudflared
```
เปิด Tunnel ชี้มาที่เซิร์ฟเวอร์:
```bash
cloudflared tunnel --url http://localhost:8000
```
จะได้รับ URL เช่น:
`https://example-random-subdomain.trycloudflare.com`

### 4. ผูก Webhook ใน LINE Developers Console
1. ไปที่แท็บ **Messaging API** ใน [LINE Developers Console](https://developers.line.biz/console/)
2. ในช่อง **Webhook URL** ให้ใส่ URL ที่ได้ต่อท้ายด้วย `/callback` เช่น:
   ```text
   https://example-random-subdomain.trycloudflare.com/callback
   ```
3. เปิดสวิตช์ **Use webhook** ให้เป็น **ON**
4. กดปุ่ม **Verify** เพื่อทดสอบการเชื่อมต่อ

---

## 📲 ตัวอย่างคำสั่งที่พิมพ์คุยใน LINE

| ฟังก์ชัน | ตัวอย่างข้อความที่พิมพ์ |
| :--- | :--- |
| **บันทึกน้ำหนัก** | `น้ำหนัก 68.5` หรือ `หนัก 70 kg` *(บันทึกเข้า Garmin ทันที)* |
| **สรุปสุขภาพวันนี้** | `สถานะ`, `วันนี้`, หรือ `สรุป` *(แสดงก้าว, HR, การนอน, ความพร้อม)* |
| **ประวัติการวิ่ง** | `ประวัติวิ่ง` หรือ `วิ่งล่าสุด` *(แสดงระยะ, Pace, HR 5 ครั้งล่าสุด)* |
| **ปรึกษาโค้ช AI** | `เดือนหน้าจะแข่ง 10k ต้องเตรียมตัวอย่างไร` *(AI ดึงตัวเลขจริงมาวิเคราะห์)* |
| **ดูวิธีใช้** | `เมนู` หรือ `วิธีใช้` |
