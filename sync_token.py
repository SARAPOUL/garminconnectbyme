#!/usr/bin/env python3
"""
Sync Garmin Token to Render Web Service
รันบนเครื่อง Mac เพื่อ Refresh Token ผ่าน Residential IP (ไม่ติด 429)
แล้วส่งไปอัปเดต Render Webhook Server ทันที
"""
import os
import sys
from pathlib import Path
import garth
import requests
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

LOCAL_TOKEN_DIRS = [
    PROJECT_DIR / ".garminconnect",
    Path.home() / ".garminconnect",
]

RENDER_URL = os.getenv("RENDER_APP_URL", "https://garminconnectbyme.onrender.com").rstrip("/")
SECRET = os.getenv("TOKEN_UPDATE_SECRET", "garmin_token_secret_sync_key")


def sync():
    token_dir = None
    for p in LOCAL_TOKEN_DIRS:
        if (p / "oauth1_token.json").exists():
            token_dir = p
            break

    if not token_dir:
        print(f"❌ ไม่พบโฟลเดอร์ Token บนเครื่อง Local")
        sys.exit(1)

    print(f"[1] โหลด Garmin Session จาก: {token_dir}")
    garth.resume(str(token_dir))

    print("[2] ทำการ Refresh OAuth2 Token ผ่าน Home IP (ไม่มีปัญหา 429)...")
    try:
        garth.client.refresh_oauth2()
        garth.save(str(token_dir))
        print(f"[3] Refresh สำเร็จ! หมดอายุ: {garth.client.oauth2_token.expires_at}")
    except Exception as e:
        print(f"❌ Refresh ไม่สำเร็จ: {e}")
        sys.exit(1)

    token_b64 = garth.client.dumps()

    update_endpoint = f"{RENDER_URL}/update-token"
    print(f"[4] กำลังส่ง Token สดใหม่ไปยัง Render: {update_endpoint}")
    try:
        resp = requests.post(
            update_endpoint,
            headers={"X-Update-Secret": SECRET},
            json={"token_base64": token_b64},
            timeout=30,
        )
        if resp.status_code == 200:
            print(f"✅ อัปเดต Token บน Render สำเร็จ: {resp.json()}")
        else:
            print(f"❌ อัปเดตไม่สำเร็จ (HTTP {resp.status_code}): {resp.text}")
    except Exception as req_err:
        print(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อไปยัง Render: {req_err}")


if __name__ == "__main__":
    sync()
