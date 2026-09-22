#!/usr/bin/env python3
"""
Garmin Connect Python Client
เชื่อมต่อ Garmin Connect เพื่อดึงข้อมูลสุขภาพและกิจกรรมออกกำลังกาย
"""

import os
import sys
from datetime import date
from getpass import getpass
from pathlib import Path
from dotenv import load_dotenv

try:
    from garminconnect import (
        Garmin,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )
except ImportError:
    print("Error: Library 'garminconnect' ยังไม่ได้ติดตั้ง")
    print("รันคำสั่ง: pip install -r requirements.txt")
    sys.exit(1)

# โหลดตัวแปรจากไฟล์ .env (หากมี)
load_dotenv()

# กำหนดไดเรกทอรีเก็บ Token ไว้ในโฟลเดอร์โปรเจกต์ (หรือ fallback ไปที่ home)
PROJECT_DIR = Path(__file__).resolve().parent
TOKEN_DIR = PROJECT_DIR / ".garminconnect"


def get_credentials():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email:
        email = input("Garmin Email: ").strip()
    if not password:
        password = getpass("Garmin Password: ")
    return email, password


def init_garmin():
    # 1. เชื่อมต่อด้วย Cached Session Token หากมีอยู่
    if TOKEN_DIR.exists():
        try:
            print(f"กำลังโหลด Session Token จาก {TOKEN_DIR}...")
            garmin = Garmin()
            garmin.login(str(TOKEN_DIR))
            print("เข้าสู่ระบบสำเร็จผ่าน Session Token")
            return garmin
        except Exception as e:
            print(f"Session Token เดิมหมดอายุหรือไม่ถูกต้อง: {e}")
            print("กำลังเข้าสู่ระบบใหม่ด้วย Email / Password...")

    # 2. เข้าสู่ระบบด้วย Email / Password (garth จะถาม MFA ทาง Terminal อัตโนมัติหากเปิดไว้)
    email, password = get_credentials()
    garmin = Garmin(email=email, password=password)

    try:
        print("กำลังส่งคำขอเข้าสู่ระบบ Garmin Connect...")
        garmin.login()

        # บันทึก Session Token สำหรับใช้งานในอนาคต
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        garmin.garth.dump(str(TOKEN_DIR))
        print(f"เข้าสู่ระบบสำเร็จ บันทึก Token เรียบร้อยที่: {TOKEN_DIR}")
        return garmin
    except GarminConnectAuthenticationError as e:
        print(f"Authentication Error: ตรวจสอบ Email, Password หรือรหัส MFA ({e})")
        sys.exit(1)
    except GarminConnectTooManyRequestsError as e:
        print(f"Rate Limit Error: เซิร์ฟเวอร์ Garmin บล็อกชั่วคราวเนื่องจาก Request ถี่เกินไป ({e})")
        sys.exit(1)
    except GarminConnectConnectionError as e:
        print(f"Connection Error: ไม่สามารถเชื่อมต่อกับ Garmin ได้ ({e})")
        sys.exit(1)
    except Exception as e:
        print(f"เกิดข้อผิดพลาด: {e}")
        sys.exit(1)


def display_today_stats(garmin):
    today = date.today().isoformat()
    print(f"\n--- สรุปข้อมูลสุขภาพประจำวัน ({today}) ---")
    try:
        stats = garmin.get_user_summary(today)
        steps = stats.get("totalSteps", 0)
        step_goal = stats.get("dailyStepGoal", 0)
        distance_km = stats.get("totalDistanceMeters", 0) / 1000
        calories = stats.get("totalKilocalories", 0)
        resting_hr = stats.get("restingHeartRate", "N/A")

        print(f"จำนวนก้าว (Steps): {steps:,} / {step_goal:,} ก้าว")
        print(f"ระยะทางรวม (Distance): {distance_km:.2f} km")
        print(f"แคลอรีรวม (Calories): {calories:.0f} kcal")
        print(f"อัตราการเต้นหัวใจขณะพัก (Resting HR): {resting_hr} bpm")
    except Exception as e:
        print(f"ไม่สามารถดึงข้อมูลสรุปประจำวันได้: {e}")


def display_recent_activities(garmin, count=5):
    print(f"\n--- ประวัติกิจกรรมล่าสุด ({count} รายการ) ---")
    try:
        activities = garmin.get_activities(0, count)
        if not activities:
            print("ไม่พบประวัติกิจกรรม")
            return

        for act in activities:
            name = act.get("activityName", "Unnamed Activity")
            sport = act.get("activityType", {}).get("typeKey", "unknown")
            start_time = act.get("startTimeLocal", "")
            distance_km = act.get("distance", 0) / 1000
            duration_min = act.get("duration", 0) / 60
            avg_hr = act.get("averageHR", "N/A")

            print(
                f"- [{start_time}] {name} ({sport}): "
                f"ระยะทาง {distance_km:.2f} km, "
                f"เวลา {duration_min:.1f} นาที, "
                f"Avg HR {avg_hr} bpm"
            )
    except Exception as e:
        print(f"ไม่สามารถดึงประวัติกิจกรรมได้: {e}")


def main():
    print("========================================")
    print("      Garmin Connect Data Client        ")
    print("========================================")

    client = init_garmin()

    if client.full_name:
        print(f"\nผู้ใช้งาน: {client.full_name} ({client.display_name})")

    display_today_stats(client)
    display_recent_activities(client, count=5)


if __name__ == "__main__":
    main()
