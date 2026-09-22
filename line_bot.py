#!/usr/bin/env python3
"""
LINE Bot Webhook Server for Garmin Connect & AI Running Coach
รองรับ:
1. ดึงตารางซ้อมล่วงหน้า (Garmin Coach / Calendar Plan)
2. วิเคราะห์คำแนะนำการซ้อมประจำวันแบบสั้นๆ อิงจาก Training Readiness & Sleep จริง
3. ส่งแจ้งเตือนอัตโนมัติทุก 8 โมงเช้า (Push Message)
4. โต้ตอบตามสั่งทันทีเมื่อพิมพ์ "ขอตารางวันนี้", "ตารางซ้อม"
5. บันทึกน้ำหนักเข้า Garmin Connect
"""

import asyncio
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
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
    PushMessageRequest,
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
_last_daily_push_date = None


def get_garmin():
    global _garmin_client
    if _garmin_client is not None:
        return _garmin_client

    garmin = Garmin()

    # 1. เช็ก Token Base64 จาก Environment Variable
    tokens_base64 = os.getenv("GARMIN_TOKENS_BASE64") or os.getenv("GARMINTOKENS")
    if tokens_base64:
        clean_token = "".join(tokens_base64.split())
        missing_padding = len(clean_token) % 4
        if missing_padding:
            clean_token += "=" * (4 - missing_padding)

        if hasattr(garmin, "garth"):
            try:
                garmin.garth.loads(clean_token)
                if garmin.garth.profile:
                    garmin.display_name = garmin.garth.profile.get("displayName")
                    garmin.full_name = garmin.garth.profile.get("fullName")
                try:
                    settings = garmin.garth.connectapi(
                        "/userprofile-service/userprofile/user-settings"
                    )
                    garmin.unit_system = settings.get("userData", {}).get("measurementSystem")
                except Exception:
                    pass

                _garmin_client = garmin
                print(f"[AUTH] Successfully loaded Garmin tokens for {garmin.full_name} ({garmin.display_name})")
                return _garmin_client
            except Exception as e:
                print(f"[AUTH] Error loading Base64 token via garth: {e}")

    # 2. เช็กจากโฟลเดอร์ไฟล์ Token
    possible_dirs = [
        TOKEN_DIR,
        PROJECT_DIR,
        Path.cwd(),
        Path("/etc/secrets"),
        Path.home() / ".garminconnect",
    ]
    for p in possible_dirs:
        if (p / "oauth1_token.json").exists() or (p / "garmin_tokens.json").exists():
            try:
                garmin.login(str(p))
                _garmin_client = garmin
                print(f"[AUTH] Successfully loaded Garmin tokens from directory: {p}")
                return _garmin_client
            except Exception as e:
                print(f"[AUTH] Error loading from {p}: {e}")

    raise RuntimeError(
        "ไม่พบ Session Token กรุณาตรวจสอบการตั้งค่า GARMIN_TOKENS_BASE64 บน Render"
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


def push_line(to_user_id: str, text: str):
    if not to_user_id:
        print("[PUSH] Warning: LINE_ALLOWED_USER_ID is not configured, skipping push.")
        return
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=to_user_id,
                messages=[TextMessage(text=text.strip())],
            )
        )
    print(f"[PUSH] Sent daily notification to {to_user_id}")


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
            r = garmin.get_training_readiness(today)
            if isinstance(r, list) and r:
                readiness_text = f"{r[0].get('score', '-')} ({r[0].get('level', '')})"
            elif isinstance(r, dict):
                readiness_text = f"{r.get('score', '-')} ({r.get('level', '')})"
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


def handle_daily_workout_report() -> str:
    """สร้างรายงานตารางซ้อมล่วงหน้า + คำแนะนำของวันนั้นจริงแบบสั้นๆ"""
    try:
        garmin = get_garmin()
        tz_bkk = timezone(timedelta(hours=7))
        today = datetime.now(tz_bkk).date()
        today_str = today.isoformat()
        end_date = today + timedelta(days=7)

        # 1. ดึง Calendar Workouts จาก Garmin
        cal1 = garmin.connectapi(f"/calendar-service/year/{today.year}/month/{today.month - 1}")
        items = cal1.get("calendarItems", [])
        if end_date.month != today.month:
            try:
                cal2 = garmin.connectapi(f"/calendar-service/year/{end_date.year}/month/{end_date.month - 1}")
                items.extend(cal2.get("calendarItems", []))
            except Exception:
                pass

        seen = set()
        upcoming = []
        today_workout = None

        for i in items:
            if i.get("itemType") == "workout" and today_str <= i.get("date", "") <= end_date.isoformat():
                wid = i.get("workoutId")
                if wid and wid not in seen:
                    seen.add(wid)
                    try:
                        detail = garmin.connectapi(f"/workout-service/workout/{wid}")
                        i["description"] = detail.get("description", "").strip()
                    except Exception:
                        i["description"] = ""
                    upcoming.append(i)
                    if i.get("date") == today_str and not today_workout:
                        today_workout = i

        upcoming.sort(key=lambda x: x.get("date"))

        # 2. ดึงสถานะความพร้อมวันนี้
        readiness_score = "-"
        readiness_level = "-"
        try:
            r = garmin.get_training_readiness(today_str)
            if isinstance(r, list) and r:
                readiness_score = r[0].get("score", "-")
                readiness_level = r[0].get("level", "-")
            elif isinstance(r, dict):
                readiness_score = r.get("score", "-")
                readiness_level = r.get("level", "-")
        except Exception:
            pass

        sleep_text = "-"
        try:
            s = garmin.get_sleep_data(today_str)
            dto = s.get("dailySleepDTO", {})
            sec = dto.get("sleepTimeSeconds", 0)
            score = dto.get("sleepScores", {}).get("overall", {}).get("value", "-")
            if sec > 0:
                sleep_text = f"{round(sec / 3600, 1)} ชม. (Score: {score})"
        except Exception:
            pass

        # 3. สร้างคำแนะนำโค้ช AI สั้นๆ จาก Gemini
        advice = "พร้อมสำหรับการซ้อมวันนี้ คุมเพซตามแผนที่กำหนด"
        if GEMINI_API_KEY and genai:
            try:
                client = genai.Client(api_key=GEMINI_API_KEY)
                prompt = (
                    f"คุณคือ Personal Running Coach มืออาชีพ\n"
                    f"วันนี้วันที่: {today_str}\n"
                    f"แผนซ้อมวันนี้จาก Garmin: {today_workout.get('title') if today_workout else 'พักผ่อน (Rest Day)'}\n"
                    f"รายละเอียดเป้าหมายเพซ/ระยะ: {today_workout.get('description') if today_workout else 'ไม่มี'}\n"
                    f"ความพร้อมร่างกายเช้านี้:\n"
                    f"- Training Readiness: {readiness_score} ({readiness_level})\n"
                    f"- การนอนหลับ: {sleep_text}\n\n"
                    f"ให้เขียนคำแนะนำการซ้อมสำหรับวันนี้แบบสั้น กระชับ ตรงประเด็น (ความยาว 2-3 บรรทัด) "
                    f"ประเมินว่าควรวิ่งตามแผนปกติ หรือควรระวัง/ปรับลดเรื่องใด (เช่น หาก Readiness ต่ำ หรือนอนน้อย ควรเน้นอะไร):"
                )
                resp = client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=prompt,
                )
                advice = resp.text.strip()
            except Exception as e:
                advice = f"คุมเพซตามแผนที่กำหนด ({e})"

        # 4. ประกอบข้อความ
        today_info = "🛌 พักผ่อน (Rest Day)"
        if today_workout:
            desc = today_workout.get("description", "").strip()
            desc_str = f"\n  เป้าหมาย: {desc}" if desc else ""
            today_info = f"🏃 {today_workout.get('title')}{desc_str}"

        lines = [
            f"☀️ ตารางซ้อม & ความพร้อมวันนี้ ({today_str})",
            f"━━━━━━━━━━━━━━━━━━━",
            f"🎯 แผนซ้อมวันนี้:",
            f"{today_info}",
            f"",
            f"💡 คำแนะนำจากโค้ช AI:",
            f"{advice}",
            f"",
            f"📊 สภาพร่างกายเช้านี้:",
            f"• Training Readiness: {readiness_score} ({readiness_level})",
            f"• การนอนหลับ: {sleep_text}",
            f"",
            f"━━━━━━━━━━━━━━━━━━━",
            f"👉 พิมพ์ \"ตารางล่วงหน้า 7 วัน\" เพื่อดูแผนซ้อมสัปดาห์นี้",
        ]

        return "\n".join(lines)
    except Exception as e:
        return f"❌ ไม่สามารถดึงตารางซ้อมได้: {e}"


def handle_tomorrow_workout_report() -> str:
    """สร้างรายงานตารางซ้อมของวันพรุ่งนี้ พร้อมคำแนะนำเตรียมตัวล่วงหน้า"""
    try:
        garmin = get_garmin()
        tz_bkk = timezone(timedelta(hours=7))
        now = datetime.now(tz_bkk)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        tomorrow_str = tomorrow.isoformat()
        end_date = tomorrow + timedelta(days=7)

        # 1. ดึง Calendar Workouts จาก Garmin
        cal1 = garmin.connectapi(f"/calendar-service/year/{tomorrow.year}/month/{tomorrow.month - 1}")
        items = cal1.get("calendarItems", [])
        if end_date.month != tomorrow.month:
            try:
                cal2 = garmin.connectapi(f"/calendar-service/year/{end_date.year}/month/{end_date.month - 1}")
                items.extend(cal2.get("calendarItems", []))
            except Exception:
                pass

        tomorrow_workout = None
        for i in items:
            if i.get("itemType") == "workout" and i.get("date") == tomorrow_str:
                wid = i.get("workoutId")
                try:
                    detail = garmin.connectapi(f"/workout-service/workout/{wid}")
                    i["description"] = detail.get("description", "").strip()
                except Exception:
                    i["description"] = ""
                tomorrow_workout = i
                break

        # 2. สร้างคำแนะนำเตรียมตัวล่วงหน้าจาก Gemini
        advice = "พักผ่อนคืนนี้ให้เพียงพอและเตรียมพร้อมสำหรับตารางซ้อมพรุ่งนี้"
        if GEMINI_API_KEY and genai:
            try:
                client = genai.Client(api_key=GEMINI_API_KEY)
                prompt = (
                    f"คุณคือ Personal Running Coach มืออาชีพ\n"
                    f"วันพรุ่งนี้วันที่: {tomorrow_str}\n"
                    f"แผนซ้อมพรุ่งนี้จาก Garmin: {tomorrow_workout.get('title') if tomorrow_workout else 'พักผ่อน (Rest Day)'}\n"
                    f"รายละเอียดเป้าหมายเพซ/ระยะ: {tomorrow_workout.get('description') if tomorrow_workout else 'ไม่มี'}\n\n"
                    f"ให้เขียนคำแนะนำเตรียมตัวล่วงหน้าสำหรับคืนนี้และก่อนซ้อมพรุ่งนี้แบบสั้น กระชับ ตรงประเด็น (ความยาว 2-3 บรรทัด) "
                    f"เช่น การเตรียมโภชนาการ การนอน หรือการวอร์มอัพเฉพาะสำหรับเซสชันนี้:"
                )
                resp = client.models.generate_content(
                    model="gemini-3.6-flash",
                    contents=prompt,
                )
                advice = resp.text.strip()
            except Exception as e:
                advice = f"เตรียมความพร้อมตามแผนการซ้อม ({e})"

        tomorrow_info = "🛌 พักผ่อน (Rest Day)"
        if tomorrow_workout:
            desc = tomorrow_workout.get("description", "").strip()
            desc_str = f"\n  เป้าหมาย: {desc}" if desc else ""
            tomorrow_info = f"🏃 {tomorrow_workout.get('title')}{desc_str}"

        lines = [
            f"🌅 แผนการซ้อมวันพรุ่งนี้ ({tomorrow_str})",
            f"━━━━━━━━━━━━━━━━━━━",
            f"🎯 แผนซ้อมพรุ่งนี้:",
            f"{tomorrow_info}",
            f"",
            f"💡 คำแนะนำเตรียมตัวล่วงหน้า:",
            f"{advice}",
            f"",
            f"━━━━━━━━━━━━━━━━━━━",
            f"👉 พิมพ์ \"ตารางล่วงหน้า 7 วัน\" เพื่อดูแผนซ้อมสัปดาห์นี้",
        ]

        return "\n".join(lines)
    except Exception as e:
        return f"❌ ไม่สามารถดึงตารางซ้อมพรุ่งนี้ได้: {e}"


def handle_upcoming_7days_report() -> str:
    """สร้างรายงานตารางซ้อมล่วงหน้า 7 วัน"""
    try:
        garmin = get_garmin()
        tz_bkk = timezone(timedelta(hours=7))
        today = datetime.now(tz_bkk).date()
        today_str = today.isoformat()
        tomorrow_str = (today + timedelta(days=1)).isoformat()
        end_date = today + timedelta(days=7)

        cal1 = garmin.connectapi(f"/calendar-service/year/{today.year}/month/{today.month - 1}")
        items = cal1.get("calendarItems", [])
        if end_date.month != today.month:
            try:
                cal2 = garmin.connectapi(f"/calendar-service/year/{end_date.year}/month/{end_date.month - 1}")
                items.extend(cal2.get("calendarItems", []))
            except Exception:
                pass

        seen = set()
        upcoming = []

        for i in items:
            if i.get("itemType") == "workout" and today_str <= i.get("date", "") <= end_date.isoformat():
                wid = i.get("workoutId")
                if wid and wid not in seen:
                    seen.add(wid)
                    try:
                        detail = garmin.connectapi(f"/workout-service/workout/{wid}")
                        i["description"] = detail.get("description", "").strip()
                    except Exception:
                        i["description"] = ""
                    upcoming.append(i)

        upcoming.sort(key=lambda x: x.get("date"))

        lines = [
            f"📅 ตารางซ้อมล่วงหน้า 7 วัน ({today_str} ถึง {end_date.isoformat()})",
            f"━━━━━━━━━━━━━━━━━━━",
        ]

        if not upcoming:
            lines.append("• ไม่พบตารางซ้อมในปฏิทิน 7 วันนี้")
        else:
            for w in upcoming:
                w_date = w.get("date", "")
                w_title = w.get("title", "Run")
                w_desc = w.get("description", "").replace("\n", " ").strip()
                short_desc = f" ({w_desc[:45]}...)" if len(w_desc) > 45 else (f" ({w_desc})" if w_desc else "")
                tag = ""
                if w_date == today_str:
                    tag = " (วันนี้)"
                elif w_date == tomorrow_str:
                    tag = " (พรุ่งนี้)"
                lines.append(f"• {w_date}{tag}: {w_title}{short_desc}")

        lines.append("")
        lines.append("👉 พิมพ์ 'วันนี้ซ้อมอะไร' หรือ 'พน.ซ้อมอะไร' เพื่อดูรายละเอียดและคำแนะนำ")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ ไม่สามารถดึงตารางซ้อมล่วงหน้าได้: {e}"


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


# Background Scheduler สำหรับส่ง 08:00 AM ทุกวัน
async def morning_push_scheduler():
    global _last_daily_push_date
    tz_bkk = timezone(timedelta(hours=7))
    print("[SCHEDULER] Daily 08:00 AM morning push notification worker started.")

    while True:
        try:
            now = datetime.now(tz_bkk)
            today_str = now.date().isoformat()

            # ส่งเฉพาะช่วง 08:00 - 08:05 น. และยังไม่ได้ส่งวันนี้
            if now.hour == 8 and now.minute < 5:
                if _last_daily_push_date != today_str:
                    print(f"[SCHEDULER] Triggering morning report for {today_str}...")
                    report_text = handle_daily_workout_report()
                    if LINE_ALLOWED_USER_ID:
                        push_line(LINE_ALLOWED_USER_ID, report_text)
                        _last_daily_push_date = today_str
        except Exception as e:
            print(f"[SCHEDULER] Error in scheduler loop: {e}")

        await asyncio.sleep(60)


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(morning_push_scheduler())


@app.get("/")
def index():
    return {"status": "ok", "message": "Garmin LINE Bot Server is running"}


@app.get("/cron/daily-workout")
@app.post("/cron/daily-workout")
def cron_daily_workout():
    """Endpoint สำหรับให้ภายนอก (เช่น cron-job.org) เรียกยิงส่งข้อความตอน 8 โมงเช้า เพื่อปลุก Render"""
    global _last_daily_push_date
    tz_bkk = timezone(timedelta(hours=7))
    today_str = datetime.now(tz_bkk).date().isoformat()

    report_text = handle_daily_workout_report()
    if LINE_ALLOWED_USER_ID:
        push_line(LINE_ALLOWED_USER_ID, report_text)
        _last_daily_push_date = today_str
        return {
            "status": "success",
            "message": "Daily workout report pushed successfully",
            "date": today_str,
            "recipient": LINE_ALLOWED_USER_ID,
        }
    return {
        "status": "warning",
        "message": "Report generated but LINE_ALLOWED_USER_ID not configured",
        "report_preview": report_text[:100],
    }


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
            "📋 เมนูคำสั่ง Garmin Assistant:\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "🏃 เช็กตารางซ้อม:\n"
            "  • วันนี้: พิมพ์ 'วันนี้ซ้อมอะไร' หรือ 'ขอตารางวันนี้'\n"
            "  • พรุ่งนี้: พิมพ์ 'พน.ซ้อมอะไร' หรือ 'พรุ่งนี้ซ้อมอะไร'\n\n"
            "📊 สรุปสุขภาพวันนี้:\n"
            "  พิมพ์: 'สถานะ', 'วันนี้', หรือ 'สรุป'\n\n"
            "⚖️ บันทึกน้ำหนัก:\n"
            "  พิมพ์: 'น้ำหนัก 68.5' หรือ 'หนัก 70'\n\n"
            "🏃 ประวัติการวิ่ง:\n"
            "  พิมพ์: 'ประวัติวิ่ง' หรือ 'วิ่งล่าสุด'\n\n"
            "💬 ปรึกษาโค้ช AI:\n"
            "  พิมพ์คำถามทั่วไปได้ทันที เช่น:\n"
            "  - 'เดือนหน้าจะแข่ง 10k ต้องเตรียมตัวอย่างไร'\n"
            "  - 'เมื่อคืนนอนน้อย วันนี้ควรซ้อมไหม'"
        )
        reply_line(event.reply_token, help_msg)
        return

    # 2. เช็กคำขอตารางซ้อมวันพรุ่งนี้ (ตรวจจับก่อนคำว่าซ้อมอะไรทั่วไป)
    tomorrow_workout_triggers = [
        "พน.ซ้อมอะไร",
        "พน ซ้อมอะไร",
        "พรุ่งนี้ซ้อมอะไร",
        "ตารางพรุ่งนี้",
        "ขอตารางพรุ่งนี้",
        "พรุ่งนี้วิ่งอะไร",
        "พน วิ่งอะไร",
        "พน.วิ่งอะไร",
        "tomorrow workout",
    ]
    # 2. เช็กคำขอตารางซ้อมล่วงหน้า 7 วัน
    upcoming_triggers = [
        "ตารางล่วงหน้า 7 วัน",
        "ตารางล่วงหน้า7วัน",
        "ตารางล่วงหน้า",
        "ตาราง 7 วัน",
        "ตาราง7วัน",
        "ตารางซ้อม 7 วัน",
        "ตารางซ้อม7วัน",
        "7 วัน",
        "7วัน",
    ]
    if any(k in user_text.lower() for k in upcoming_triggers):
        upcoming_report = handle_upcoming_7days_report()
        reply_line(event.reply_token, upcoming_report)
        return

    # 3. เช็กคำขอตารางซ้อมวันพรุ่งนี้
    tomorrow_workout_triggers = [
        "พน.ซ้อมอะไร",
        "พน ซ้อมอะไร",
        "พรุ่งนี้ซ้อมอะไร",
        "ตารางพรุ่งนี้",
        "ขอตารางพรุ่งนี้",
        "พรุ่งนี้วิ่งอะไร",
        "พน วิ่งอะไร",
        "พน.วิ่งอะไร",
        "tomorrow workout",
    ]
    if any(k in user_text.lower() for k in tomorrow_workout_triggers):
        tomorrow_report = handle_tomorrow_workout_report()
        reply_line(event.reply_token, tomorrow_report)
        return

    # 4. เช็กคำขอตารางซ้อมวันนี้
    today_workout_triggers = [
        "วันนี้ซ้อมอะไร",
        "ซ้อมอะไรวันนี้",
        "วันนี้วิ่งอะไร",
        "วิ่งอะไรวันนี้",
        "ขอตารางวันนี้",
        "ตารางวันนี้",
        "ตารางซ้อม",
        "ซ้อมอะไร",
        "วิ่งอะไร",
        "workout",
        "workouts",
        "plan",
        "schedule",
    ]
    if any(k in user_text.lower() for k in today_workout_triggers):
        workout_report = handle_daily_workout_report()
        reply_line(event.reply_token, workout_report)
        return

    # 3. เช็กคำสั่งบันทึกน้ำหนัก
    weight_res = handle_record_weight(user_text)
    if weight_res:
        reply_line(event.reply_token, weight_res)
        return

    # 4. เช็กคำสั่งสรุปข้อมูลสุขภาพประจำวัน
    if user_text.lower() in ["สถานะ", "วันนี้", "สรุป", "status", "today"]:
        summary_res = handle_today_summary()
        reply_line(event.reply_token, summary_res)
        return

    # 5. เช็กคำสั่งประวัติการวิ่ง
    if user_text.lower() in ["ประวัติวิ่ง", "วิ่งล่าสุด", "runs", "activities"]:
        runs_res = handle_recent_runs()
        reply_line(event.reply_token, runs_res)
        return

    # 6. ถามคำถามทั่วไป (Gemini Coach วิเคราะห์ร่วมกับข้อมูล Garmin)
    coach_reply = ask_gemini_coach(user_text)
    reply_line(event.reply_token, coach_reply)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 เริ่มการทำงาน Garmin LINE Bot Server ที่พอร์ต {port}...")
    uvicorn.run("line_bot:app", host="0.0.0.0", port=port, reload=True)
