#!/usr/bin/env python3
"""
LINE Bot Webhook Server for Garmin Connect & AI Running Coach
รองรับการบันทึกน้ำหนัก, สรุปข้อมูลสุขภาพประจำวัน, และตอบคำถามการซ้อมวิ่งด้วย Gemini AI
"""

import os
import re
import sys
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

# FastAPI
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# LINE Bot SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Garmin Connect
try:
    from garminconnect import Garmin
except ImportError:
    print("Error: garminconnect is not installed")
    sys.exit(1)

# Google GenAI
try:
    from google import genai
except ImportError:
    genai = None

# โหลดค่า Environment Variables
load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent
TOKEN_DIR = PROJECT_DIR / ".garminconnect"
if not TOKEN_DIR.exists():
    home_token = Path.home() / ".garminconnect"
    try:
        test_file = home_token / "oauth1_token.json"
        if test_file.exists():
            TOKEN_DIR = home_token
    except Exception:
        pass

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_ALLOWED_USER_ID = os.getenv("LINE_ALLOWED_USER_ID", "").strip()

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    print("Error: กรุณากำหนด LINE_CHANNEL_SECRET และ LINE_CHANNEL_ACCESS_TOKEN ในไฟล์ .env")
    sys.exit(1)

# เตรียม Client
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app = FastAPI(title="Garmin LINE Bot")

# Cache Garmin Client
_garmin_client = None


def get_garmin():
    global _garmin_client
    if _garmin_client is not None:
        return _garmin_client

    garmin = Garmin()

    # 1. เช็ก Token Base64 จาก Environment Variable (สำหรับ Cloud เช่น Render)
    tokens_base64 = os.getenv("GARMIN_TOKENS_BASE64") or os.getenv("GARMINTOKENS")
    if tokens_base64:
        try:
            garmin.garth.loads(tokens_base64)
            _garmin_client = garmin
            return _garmin_client
        except Exception:
            if os.path.exists(tokens_base64):
                garmin.login(tokens_base64)
                _garmin_client = garmin
                return _garmin_client

    # 2. เช็กจากโฟลเดอร์ Token ในเครื่อง
    if TOKEN_DIR.exists():
        garmin.login(str(TOKEN_DIR))
        _garmin_client = garmin
        return _garmin_client

    raise RuntimeError(
        f"ไม่พบ Session Token กรุณารัน 'python connect_garmin.py' ในเครื่อง หรือกำหนด GARMIN_TOKENS_BASE64 ใน Environment Variable"
    )


def reply_line(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text.strip())],
            )
        )


def handle_record_weight(user_text: str) -> str:
    pattern = r"(?:น้ำหนัก|หนัก|weight|wt)\s*[:=]?\s*(\d+(?:\.\d+)?)|^(\d{2,3}(?:\.\d+)?)\s*(?:kg|กก|กิโล)$"
    match = re.search(pattern, user_text.strip(), re.IGNORECASE)
    if not match:
        return None

    weight_str = match.group(1) or match.group(2)
    weight_val = float(weight_str)

    if weight_val < 20 or weight_val > 300:
        return "⚠️ ค่าน้ำหนักไม่อยู่ในช่วงปกติ (20 - 300 kg) กรุณาระบุใหม่"

    try:
        garmin = get_garmin()
        garmin.add_weigh_in(weight=weight_val, unitKey="kg")
        return f"✅ บันทึกน้ำหนัก {weight_val} kg ลงใน Garmin Connect สำเร็จแล้ว"
    except Exception as e:
        return f"❌ ไม่สามารถบันทึกน้ำหนักได้: {e}"


def handle_today_summary() -> str:
    try:
        garmin = get_garmin()
        today = date.today().isoformat()
        summary = garmin.get_user_summary(today)

        steps = summary.get("totalSteps", 0)
        step_goal = summary.get("dailyStepGoal", 0)
        distance_km = (summary.get("totalDistanceMeters") or 0) / 1000
        calories = summary.get("totalKilocalories", 0)
        resting_hr = summary.get("restingHeartRate", "-")
        stress = summary.get("averageStressLevel", "-")

        sleep_text = "-"
        try:
            sleep = garmin.get_sleep_data(today)
            dto = sleep.get("dailySleepDTO", {})
            sec = dto.get("sleepTimeSeconds", 0)
            score = dto.get("sleepScores", {}).get("overall", {}).get("value", "-")
            if sec > 0:
                hours = round(sec / 3600, 1)
                sleep_text = f"{hours} ชม. (Score: {score})"
        except Exception:
            pass

        readiness_text = "-"
        try:
            readiness = garmin.get_training_readiness(today)
            if readiness and "score" in readiness:
                readiness_text = f"{readiness['score']} ({readiness.get('level', '')})"
        except Exception:
            pass

        return (
            f"📊 สรุปข้อมูลสุขภาพประจำวัน ({today})\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🚶 จำนวนก้าว: {steps:,} / {step_goal:,} ก้าว\n"
            f"📍 ระยะทางรวม: {distance_km:.2f} km\n"
            f"🔥 แคลอรี: {calories:,} kcal\n"
            f"❤️ Resting HR: {resting_hr} bpm\n"
            f"😴 การนอนหลับ: {sleep_text}\n"
            f"⚡ ความเครียดเฉลี่ย: {stress}\n"
            f"🎯 Training Readiness: {readiness_text}"
        )
    except Exception as e:
        return f"❌ ไม่สามารถดึงข้อมูลสรุปได้: {e}"


def handle_recent_runs() -> str:
    try:
        garmin = get_garmin()
        activities = garmin.get_activities(0, 15)
        runs = [
            a for a in activities
            if "run" in a.get("activityType", {}).get("typeKey", "").lower()
        ][:5]

        if not runs:
            return "ไม่พบประวัติกิจกรรมการวิ่งล่าสุด"

        lines = ["🏃 ประวัติการวิ่ง 5 ครั้งล่าสุด\n━━━━━━━━━━━━━━━━━━━"]
        for r in runs:
            name = r.get("activityName", "Run")
            start = r.get("startTimeLocal", "")[:10]
            dist = (r.get("distance") or 0) / 1000
            dur = (r.get("duration") or 0) / 60
            speed = r.get("averageSpeed", 0)
            pace_str = "N/A"
            if speed > 0:
                sec_per_km = 1000 / speed
                p_min, p_sec = divmod(int(sec_per_km), 60)
                pace_str = f"{p_min}:{p_sec:02d}"

            hr = r.get("averageHR", "-")
            lines.append(f"• [{start}] {name}\n  ระยะทาง: {dist:.2f} km | เวลา: {dur:.1f} น. | Pace: {pace_str} | HR: {hr}")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ ไม่สามารถดึงประวัติการวิ่งได้: {e}"


def ask_gemini_coach(question: str) -> str:
    if not GEMINI_API_KEY:
        return (
            "💡 ได้รับคำถามแล้ว แต่ยังไม่ได้กำหนด GEMINI_API_KEY ในไฟล์ .env\n"
            "กรุณานำ API Key จาก Google AI Studio มาใส่เพื่อเปิดใช้งานระบบวิเคราะห์แผนซ้อมวิ่งด้วย AI"
        )

    # ดึง Context จาก Garmin
    context = {}
    today = date.today().isoformat()
    try:
        garmin = get_garmin()
        context["today_summary"] = garmin.get_user_summary(today)
        context["race_predictions"] = garmin.get_race_predictions()
        context["training_readiness"] = garmin.get_training_readiness(today)
        context["vo2max"] = garmin.get_max_metrics(today)

        acts = garmin.get_activities(0, 20)
        running_acts = [
            a for a in acts
            if "run" in a.get("activityType", {}).get("typeKey", "").lower()
        ]
        total_km = sum(a.get("distance", 0) for a in running_acts) / 1000
        recent_summaries = []
        for a in running_acts[:5]:
            dist = (a.get("distance") or 0) / 1000
            dur = (a.get("duration") or 0) / 60
            speed = a.get("averageSpeed", 0)
            pace = "N/A"
            if speed > 0:
                p_min, p_sec = divmod(int(1000 / speed), 60)
                pace = f"{p_min}:{p_sec:02d}"
            recent_summaries.append({
                "date": a.get("startTimeLocal", "")[:10],
                "distance_km": round(dist, 2),
                "duration_min": round(dur, 1),
                "pace": pace,
                "avg_hr": a.get("averageHR")
            })
        context["running_history"] = {
            "total_recent_km": round(total_km, 2),
            "recent_runs": recent_summaries
        }
    except Exception as e:
        context["fetch_note"] = f"บางส่วนของข้อมูล Garmin ไม่พร้อมใช้งาน: {e}"

    system_prompt = (
        "คุณคือ Personal Running Coach มืออาชีพ ให้คำปรึกษาแผนการซ้อมวิ่ง วิเคราะห์สมรรถภาพ และการดูแลร่างกาย "
        "โดยอิงจากข้อมูลจริงจาก Garmin Connect ของผู้ใช้ที่ให้มา "
        "คำตอบต้องตรงประเด็น นำไปปฏิบัติได้จริง (Actionable) แบ่งหัวข้อให้อ่านง่ายในแชต LINE "
        "และกำหนด Pace หรือระยะทางโดยอิงจากสมรรถภาพปัจจุบันของผู้ใช้จริง"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            f"{system_prompt}\n\n"
            f"[ข้อมูล Garmin Connect ล่าสุดของผู้ใช้]:\n{context}\n\n"
            f"[คำถามจากผู้ใช้]: {question}\n\n"
            f"กรุณาตอบคำแนะนำอย่างเป็นมืออาชีพและกระชับ:"
        )

        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        return f"❌ เกิดข้อผิดพลาดในการประมวลผลคำตอบจาก Gemini: {e}"


@app.get("/")
def index():
    return {"status": "ok", "message": "Garmin LINE Bot Server is running"}


@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature")

    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        handler.handle(body_str, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return JSONResponse(content={"status": "success"})


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    print(f"\n[LINE Webhook] User ID: {user_id} | Message: {user_text}")

    # ตรวจสอบ Whitelist (หากกำหนดไว้)
    if LINE_ALLOWED_USER_ID:
        if user_id != LINE_ALLOWED_USER_ID:
            print(f"[SECURITY] Ignored message from unauthorized user: {user_id}")
            return
    else:
        print(f"👉 TIP: สามารถนำ User ID นี้ '{user_id}' ไปใส่ใน LINE_ALLOWED_USER_ID ใน .env เพื่อล็อกให้บอทตอบเฉพาะคุณได้")

    # 1. เช็กคำสั่ง Help / เมนู
    if user_text.lower() in ["help", "วิธีใช้", "เมนู", "menu"]:
        help_msg = (
            "📋 เมนูคำสั่งของ Garmin Assistant:\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "⚖️ บันทึกน้ำหนัก:\n"
            "  พิมพ์เช่น: 'น้ำหนัก 68.5' หรือ 'หนัก 70'\n\n"
            "📊 สรุปสุขภาพวันนี้:\n"
            "  พิมพ์: 'สถานะ', 'วันนี้', หรือ 'สรุป'\n\n"
            "🏃 ประวัติการวิ่ง:\n"
            "  พิมพ์: 'ประวัติวิ่ง' หรือ 'วิ่งล่าสุด'\n\n"
            "💬 ปรึกษาโค้ช AI:\n"
            "  พิมพ์คำถามทั่วไปได้ทันที เช่น:\n"
            "  - 'เดือนหน้าจะแข่ง 10k ต้องเตรียมตัวอย่างไร'\n"
            "  - 'วันนี้ควรซ้อมวิ่งระยะเท่าไหร่ดี'\n"
            "  - 'สภาพร่างกายตอนนี้พร้อมซ้อมหนักไหม'"
        )
        reply_line(event.reply_token, help_msg)
        return

    # 2. เช็กคำสั่งบันทึกน้ำหนัก
    weight_res = handle_record_weight(user_text)
    if weight_res:
        reply_line(event.reply_token, weight_res)
        return

    # 3. เช็กคำสั่งสรุปข้อมูลสุขภาพประจำวัน
    if user_text.lower() in ["สถานะ", "วันนี้", "สรุป", "status", "today"]:
        summary_res = handle_today_summary()
        reply_line(event.reply_token, summary_res)
        return

    # 4. เช็กคำสั่งประวัติการวิ่ง
    if user_text.lower() in ["ประวัติวิ่ง", "วิ่งล่าสุด", "runs", "activities"]:
        runs_res = handle_recent_runs()
        reply_line(event.reply_token, runs_res)
        return

    # 5. ถามคำถามทั่วไป (Gemini Coach วิเคราะห์ร่วมกับข้อมูล Garmin)
    coach_reply = ask_gemini_coach(user_text)
    reply_line(event.reply_token, coach_reply)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 เริ่มการทำงาน Garmin LINE Bot Server ที่พอร์ต {port}...")
    uvicorn.run("line_bot:app", host="0.0.0.0", port=port, reload=True)
