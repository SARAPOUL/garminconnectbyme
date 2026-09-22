---
name: garmin
description: >-
  Use this skill when the user asks questions prefixed with /garmin, requests running advice or race training plans (5k, 10k, half marathon, marathon), asks to analyze Garmin Connect fitness metrics (VO2 max, training readiness, sleep, resting HR, activities), or requests to log body weight to Garmin.
---

# Garmin Connect Skill

Skill สำหรับวิเคราะห์ข้อมูลสุขภาพ การฝึกซ้อมวิ่ง และจัดการข้อมูล Garmin Connect ของผู้ใช้

## คำสั่ง CLI Helper

สคริปต์ helper อยู่ที่ `.agents/skills/garmin/scripts/garmin_cli.py` ให้รันผ่าน virtual environment ในโปรเจกต์:

### 1. ดึงข้อมูลสรุปสมรรถภาพและการวิ่ง (Summary & Fitness Context)
ใช้เมื่อผู้ใช้ถามคำถามเกี่ยวกับการซ้อมวิ่ง วางแผนแข่ง หรือเช็กความฟิต:
```bash
venv/bin/python .agents/skills/garmin/scripts/garmin_cli.py summary
```
ข้อมูลที่ได้จะครอบคลุม:
- ข้อมูลสุขภาพวันนี้: ก้าว, แคลอรี, Resting Heart Rate, การนอนหลับ (Sleep Score / Hours)
- Training Readiness และ Training Status
- VO2 Max (Running / Cycling)
- Race Predictions (เวลาคาดการณ์ 5K, 10K, Half, Full Marathon)
- ข้อมูลการวิ่ง 4 สัปดาห์ล่าสุด: ระยะรวม, จำนวนครั้ง, ประวัติการวิ่ง 5 ครั้งล่าสุด (Pace, Distance, HR)

### 2. บันทึกค่าน้ำหนัก (Log Weigh-In)
ใช้เมื่อผู้ใช้แจ้งน้ำหนัก เช่น "บันทึกน้ำหนัก 68.5" หรือ "น้ำหนัก 70 kg":
```bash
venv/bin/python .agents/skills/garmin/scripts/garmin_cli.py weight <number>
```
ตัวอย่าง:
```bash
venv/bin/python .agents/skills/garmin/scripts/garmin_cli.py weight 68.5
```

### 3. ดึงประวัติกิจกรรมล่าสุด (Activities List)
ใช้เมื่อผู้ใช้ต้องการดูประวัติกิจกรรมการออกกำลังกายย้อนหลัง:
```bash
venv/bin/python .agents/skills/garmin/scripts/garmin_cli.py activities --count 10
```

---

## แนวทางการวิเคราะห์แผนฝึกซ้อมวิ่ง (เช่น แข่ง 10K)

เมื่อผู้ใช้สอบถามแผนการซ้อม เช่น `"/garmin เดือนหน้าจะแข่ง 10k ต้องเตรียมตัวอย่างไร"`:

1. **ดึง Context**: รันคำสั่ง `summary` เพื่อดึงข้อมูลจริงของผู้ใช้
2. **ประเมินความฟิตปัจจุบัน**:
   - ตรวจสอบค่า VO2 Max และเวลา 10K จาก `race_predictions` เพื่อตั้งเป้าหมายเวลา (Target Time) ที่สมเหตุสมผล
   - ตรวจสอบระยะทางวิ่งสะสมต่อสัปดาห์ (Weekly Mileage) จาก `recent_running_overview`
3. **กำหนด Pace การซ้อมแต่ละประเภท**:
   - **Easy / Recovery Run**: วิ่งเบาคุม Heart Rate Zone 2
   - **Tempo / Threshold Run**: ซ้อมที่ความเร็วแข่ง 10K หรือเร็วกว่าเล็กน้อย
   - **Interval / Speed Work**: ซ้อมความเร็วช่วงสั้น
   - **Long Run**: ซ้อมความอึดโดยระยะไม่ควรเกิน 25-30% ของ Weekly Mileage ทั้งหมด
4. **วางโครงสร้างตารางซ้อม (Periodization)**:
   - สัปดาห์ที่ 1 (Build Base): รักษาระยะ วิ่งเน้น Zone 2
   - สัปดาห์ที่ 2 (Peak Mileage & Tempo): เพิ่มความเข้มข้น มีการซ้อม 10K Pace
   - สัปดาห์ที่ 3 (Race-Specific): ทดสอบ Long Run และ Tempo สั้น
   - สัปดาห์ที่ 4 (Tapering): ลดระยะทางลง 40-50% พักผ่อนและรักษาสภาพความสด
5. **ข้อควรระวัง**:
   - หากตรวจพบว่าไม่มี Session Token ให้แจ้งผู้ใช้รัน `python connect_garmin.py` ใน Terminal เพื่อล็อกอินรอบแรกก่อน
