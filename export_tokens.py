#!/usr/bin/env python3
"""
สคริปต์สำหรับ Export Garmin Session Token ออกมาเป็น Base64 String
เพื่อนำไปใส่ใน Environment Variable 'GARMIN_TOKENS_BASE64' บน Render หรือ Cloud
"""

import sys
from pathlib import Path

try:
    from garminconnect import Garmin
except ImportError:
    print("Error: Library 'garminconnect' ยังไม่ได้ติดตั้ง")
    sys.exit(1)

PROJECT_DIR = Path(__file__).resolve().parent
TOKEN_DIR = PROJECT_DIR / ".garminconnect"

if not TOKEN_DIR.exists():
    home_token = Path.home() / ".garminconnect"
    if home_token.exists():
        TOKEN_DIR = home_token
    else:
        print(f"Error: ไม่พบโฟลเดอร์ Token กรุณารัน 'python connect_garmin.py' ก่อน")
        sys.exit(1)

client = Garmin()
try:
    client.login(str(TOKEN_DIR))
    token_str = client.garth.dumps()
    print("\n" + "=" * 60)
    print("คัดลอกข้อความด้านล่างนี้ ไปใส่ใน Environment Variable 'GARMIN_TOKENS_BASE64' บน Render:")
    print("=" * 60)
    print(token_str)
    print("=" * 60 + "\n")
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
