#!/usr/bin/env python3
"""
Sync Garmin Token to Render Web Service
รองรับทั้งการรันบนเครื่อง Local (Mac) และรันผ่าน GitHub Actions
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
    loaded = False
    token_b64_env = os.getenv("GARMIN_TOKENS_BASE64", "").strip()

    # 1. โหลดจาก Environment Variable GARMIN_TOKENS_BASE64 (สำหรับ GitHub Actions)
    if token_b64_env:
        try:
            print("[1] โหลด Garmin Session จาก Environment Variable GARMIN_TOKENS_BASE64...")
            clean_token = "".join(token_b64_env.split()).strip("'\"")
            missing_padding = len(clean_token) % 4
            if missing_padding:
                clean_token += "=" * (4 - missing_padding)
            garth.client.loads(clean_token)
            loaded = True
            print("    โหลด Session สำเร็จ")
        except Exception as e:
            print(f"⚠️ โหลดจาก GARMIN_TOKENS_BASE64 ไม่สำเร็จ: {e}")

    # 2. โหลดจากโฟลเดอร์ไฟล์บนเครื่อง Local (สำหรับ Mac)
    if not loaded:
        token_dir = None
        for p in LOCAL_TOKEN_DIRS:
            if (p / "oauth1_token.json").exists():
                token_dir = p
                break
        if token_dir:
            try:
                print(f"[1] โหลด Garmin Session จากไฟล์ในเครื่อง: {token_dir}")
                garth.resume(str(token_dir))
                loaded = True
                print("    Resume สำเร็จ")
            except Exception as e:
                print(f"⚠️ ไม่สามารถ Resume token directory: {e}")

    # 3. ลอง Login ด้วย Email/Password ถ้ายังไม่มี Session
    if not loaded:
        email = os.getenv("GARMIN_EMAIL")
        password = os.getenv("GARMIN_PASSWORD")
        if email and password:
            try:
                print(f"[1] พยายามล็อกอินด้วย Email: {email}...")
                garth.login(email, password)
                loaded = True
                print("    ล็อกอินสำเร็จ")
            except Exception as e:
                print(f"❌ ล็อกอินด้วย Email/Password ไม่สำเร็จ: {e}")

    if not loaded or not garth.client.oauth1_token:
        print("❌ ไม่พบข้อมูล Garmin Token หรือไม่สามารถโหลด Session ได้")
        sys.exit(1)

    # 4. Refresh OAuth2 Token
    print("[2] ทำการ Refresh OAuth2 Token...")
    try:
        garth.client.refresh_oauth2()
        print(f"[3] Refresh สำเร็จ! หมดอายุ: {getattr(garth.client.oauth2_token, 'expires_at', 'N/A')}")
        for p in LOCAL_TOKEN_DIRS:
            if p.exists():
                try:
                    garth.save(str(p))
                except Exception:
                    pass
                break
    except Exception as e:
        print(f"⚠️ Refresh OAuth2 ไม่สำเร็จ ({e}) จะใช้ Token ที่มีอยู่ส่งต่อไป...")

    token_b64 = garth.client.dumps()

    update_endpoint = f"{RENDER_URL}/update-token"
    print(f"[4] กำลังส่ง Token ไปยัง Render: {update_endpoint}")
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
            sys.exit(1)
    except Exception as req_err:
        print(f"❌ เกิดข้อผิดพลาดในการเชื่อมต่อไปยัง Render: {req_err}")
        sys.exit(1)


if __name__ == "__main__":
    sync()
