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
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

# FastAPI
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

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

# Garth SSO Consumer Preset (ป้องกัน Timeout บน Cloud/Render จาก AWS S3)
try:
    import garth.sso
    garth.sso.OAUTH_CONSUMER = {
        "consumer_key": "fcad1b9b-81e8-495b-a24a-0a2c07cad970",
        "consumer_secret": "E08UhVydFXcVOAnNsuDczEuELF6nlCdgiGk",
    }
except Exception:
    pass

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
_SERVER_START_TIME = datetime.now(timezone(timedelta(hours=7)))


def handle_server_uptime() -> str:
    tz_bkk = timezone(timedelta(hours=7))
    now = datetime.now(tz_bkk)
    diff = now - _SERVER_START_TIME
    total_seconds = int(diff.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days} วัน")
    if hours > 0 or days > 0:
        parts.append(f"{hours} ชั่วโมง")
    parts.append(f"{minutes} นาที {seconds} วินาที")
    uptime_str = " ".join(parts)

    lines = [
        "🖥️ สถานะการทำงานของเซิร์ฟเวอร์ (Render)",
        "━━━━━━━━━━━━━━━━━━━",
        "🟢 สถานะ: กำลังทำงาน (Online / Live)",
        f"⏱️ ทำงานต่อเนื่องมาแล้ว: {uptime_str}",
        f"🚀 เริ่มสตาร์ตเมื่อ: {_SERVER_START_TIME.strftime('%d/%m/%Y %H:%M:%S')} (เวลาไทย)",
        "━━━━━━━━━━━━━━━━━━━",
        "💡 โควตา Free Tier ของ Render มี 750 ชั่วโมง/เดือน",
    ]
    return "\n".join(lines)


def get_garmin(force_refresh: bool = False):
    global _garmin_client
    if _garmin_client is not None and not force_refresh:
        # ตรวจสอบว่า OAuth2 Token ที่แคชไว้หมดอายุหรือไม่ ถ้าหมดให้ Auto-Refresh บน Cloud ทันที
        if hasattr(_garmin_client, "garth") and getattr(_garmin_client.garth, "oauth2_token", None):
            if _garmin_client.garth.oauth2_token.expired:
                print("[AUTH] Cached OAuth2 token expired. Auto-refreshing on cloud via OAuth1...")
                try:
                    _garmin_client.garth.refresh_oauth2()
                    print("[AUTH] Successfully refreshed cached OAuth2 token on cloud.")
                except Exception as ref_err:
                    print(f"[AUTH] Failed to refresh cached OAuth2: {ref_err}")
                    _garmin_client = None
        if _garmin_client is not None:
            return _garmin_client

    garmin = Garmin()

    auth_errors = []
    # 1. เช็ก Token Base64 จาก Environment Variable
    tokens_base64 = os.getenv("GARMIN_TOKENS_BASE64") or os.getenv("GARMINTOKENS")
    if tokens_base64:
        clean_token = "".join(tokens_base64.split()).strip("'\"")
        missing_padding = len(clean_token) % 4
        if missing_padding:
            clean_token += "=" * (4 - missing_padding)

        if hasattr(garmin, "garth"):
            try:
                garmin.garth.loads(clean_token)

                # ตรวจสอบและ Refresh ทันทีถ้า OAuth2 หมดอายุ โดยแลกเปลี่ยนผ่าน OAuth1 Token (อายุ 1 ปี)
                if garmin.garth.oauth2_token and garmin.garth.oauth2_token.expired:
                    print("[AUTH] OAuth2 token is expired. Auto-refreshing on cloud via OAuth1...")
                    garmin.garth.refresh_oauth2()
                    print("[AUTH] Successfully refreshed OAuth2 token on cloud via OAuth1 exchange!")

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
                auth_errors.append(f"Base64: {e}")
                print(f"[AUTH] Error loading Base64 token via garth: {e}")
        else:
            auth_errors.append("Library mismatch: 'Garmin' has no 'garth'")
    else:
        auth_errors.append("ไม่พบ Environment Variable 'GARMIN_TOKENS_BASE64'")

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
                auth_errors.append(f"Dir {p.name}: {e}")
                print(f"[AUTH] Error loading from {p}: {e}")

    detail = " | ".join(auth_errors) if auth_errors else "ไม่พบไฟล์ Token"
    raise RuntimeError(
        f"ไม่สามารถโหลด Session Garmin ได้ ({detail}) กรุณาตรวจสอบหรืออัปเดตค่า GARMIN_TOKENS_BASE64 บน Render"
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


ATHLETE_PROFILE_FILE = PROJECT_DIR / "athlete_profile.json"


def get_athlete_profile() -> dict:
    """โหลดข้อมูลผลทดสอบ Lactate และโซนการฝึกซ้อมของนักกีฬา (เฉพาะ account คุณพรเทพ)"""
    try:
        if ATHLETE_PROFILE_FILE.exists():
            with open(ATHLETE_PROFILE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[PROFILE] Failed to load athlete profile: {e}")
    return {}


def format_athlete_profile_summary(profile: dict) -> str:
    """จัดรูปแบบข้อมูล Lactate Thresholds และ Training Zones เป็นข้อความสำหรับ AI Prompt"""
    if not profile:
        return ""
    lt = profile.get("lactate_thresholds", {})
    lt1 = lt.get("LT1", {})
    lt2 = lt.get("LT2", {})
    return (
        f"[ข้อมูล Lactate Threshold & โซนการฝึกซ้อมเฉพาะบุคคลของคุณพรเทพ]:\n"
        f"• LT1 (Aerobic Threshold): Pace {lt1.get('pace_min_km', '05:27')} / HR {lt1.get('heart_rate_bpm', 172)} bpm | Lactate {lt1.get('lactate_mmol', 1.6)} mmol\n"
        f"• LT2 (Anaerobic / Threshold): Pace {lt2.get('pace_min_km', '04:37')} / HR {lt2.get('heart_rate_bpm', 187)} bpm | Lactate {lt2.get('lactate_mmol', 3.0)} mmol\n"
        f"• Easy Run / Recovery / Long Run: Pace 6:00 - 6:40 /km (หรือช้ากว่า 6:40), HR 160 - 170 bpm (< 172 bpm)\n"
        f"• Steady Run (ระหว่าง LT1-LT2): Pace 5:00 - 5:27 /km, HR 172 - 180 bpm\n"
        f"• Tempo / Threshold Run (จุด LT2): Pace 4:37 /km, HR 187 bpm\n"
        f"• High Intensity / Interval (เหนือ LT2): Pace < 4:17 /km, HR 192+ bpm\n"
        f"• Device HR Zones: Z1 < 172, Z2 172-186, Z3 187-191, Z4 192-199, Z5 >= 200 bpm\n"
    )


def handle_show_lactate_profile() -> str:
    """แสดงข้อมูลผลการทดสอบ Lactate และโซนการซ้อมของคุณพรเทพ"""
    profile = get_athlete_profile()
    if not profile:
        return "❌ ไม่พบข้อมูลผลทดสอบ Lactate ในระบบ"

    lt = profile.get("lactate_thresholds", {})
    lt1 = lt.get("LT1", {})
    lt2 = lt.get("LT2", {})
    hr_zones = profile.get("heart_rate_device_zones", {})

    lines = [
        f"🩸 ผลทดสอบ Lactate & โซนการซ้อม (คุณ{profile.get('athlete_name', 'พรเทพ')})",
        "━━━━━━━━━━━━━━━━━━━",
        "🎯 จุดเกณฑ์เปลี่ยนแลคเตท (Lactate Thresholds):",
        f"• LT1 (แอโรบิกพื้นฐาน): เพซ {lt1.get('pace_min_km')} | HR {lt1.get('heart_rate_bpm')} bpm ({lt1.get('lactate_mmol')} mmol)",
        f"• LT2 (Threshold / Tempo): เพซ {lt2.get('pace_min_km')} | HR {lt2.get('heart_rate_bpm')} bpm ({lt2.get('lactate_mmol')} mmol)",
        "",
        "🏃 โซนซ้อมตามวัตถุประสงค์ (Training Zones):",
        "1. Easy / Long Run (< LT1):",
        "   - เพซ: 6:00 - 6:40 /km (Warmup ช้ากว่า 6:40)",
        "   - HR: 160 - 170 bpm (< 172)",
        "2. Steady Run (LT1 - LT2):",
        "   - เพซ: 5:00 - 5:27 /km",
        "   - HR: 172 - 180 bpm",
        "3. Tempo / Threshold (ที่จุด LT2):",
        "   - เพซ: 4:37 /km",
        "   - HR: 187 bpm",
        "4. High Intensity / Interval (> LT2):",
        "   - เพซ: เร็วกว่า 4:17 /km",
        "   - HR: 192+ bpm",
        "",
        "⌚ Heart Rate Zones บนนาฬิกา Garmin:",
        f"• Z1 (Recovery): {hr_zones.get('zone_1', {}).get('range', '< 172 bpm')}",
        f"• Z2 (Aerobic Base): {hr_zones.get('zone_2', {}).get('range', '172 - 186 bpm')}",
        f"• Z3 (Threshold LT2): {hr_zones.get('zone_3', {}).get('range', '187 - 191 bpm')}",
        f"• Z4 (High Intensity): {hr_zones.get('zone_4', {}).get('range', '192 - 199 bpm')}",
        f"• Z5 (Maximal / VO2max): {hr_zones.get('zone_5', {}).get('range', '>= 200 bpm')}",
        "━━━━━━━━━━━━━━━━━━━",
        "💡 AI Coach ใช้ข้อมูลชุดนี้ในการวิเคราะห์และออกแบบตารางซ้อมเฉพาะบุคคลของคุณเสมอ",
    ]
    return "\n".join(lines)


def call_gemini_with_fallback(client, prompt: str) -> str:
    """เรียกใช้ Gemini API พร้อมระบบ Fallback Model และ Retry เพื่อป้องกันปัญหา 503 Overload"""
    candidate_models = [
        os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.5-flash-lite",
        "gemini-flash-latest",
    ]
    seen = set()
    models_to_try = [m for m in candidate_models if m and not (m in seen or seen.add(m))]

    last_err = None
    for model_name in models_to_try:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            text = resp.text or ""
            if not text and hasattr(resp, "candidates") and resp.candidates:
                for part in resp.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        text += part.text
            if text:
                return text.strip()
        except Exception as e:
            last_err = e
            err_str = str(e)
            print(f"[GEMINI] Model {model_name} failed: {err_str[:120]}")
            # ถ้าเจอ 503 หรือ 429 ให้ข้ามไปลองโมเดลถัดไปทันที
            continue
    raise RuntimeError(f"All Gemini models failed: {last_err}")


def format_speed_to_pace(val: float) -> str:
    """แปลงความเร็ว m/s หรือ sec/km เป็นเพซ mm:ss /km"""
    if not val or val <= 0:
        return ""
    if 1.0 <= val <= 10.0:
        sec_per_km = 1000.0 / val
    else:
        sec_per_km = val
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}"


def parse_step(step: dict) -> str:
    """แปลง Workout Step แต่ละขั้นเป็นข้อความภาษาไทยที่กระชับและเข้าใจง่าย"""
    step_type_obj = step.get("stepType") or {}
    type_key = step_type_obj.get("stepTypeKey", "")
    type_name_map = {
        "warmup": "วอร์มอัพ",
        "cooldown": "คูลดาวน์",
        "interval": "วิ่งหลัก",
        "recovery": "พัก/ฟื้นตัว",
        "rest": "พัก",
    }
    type_name = type_name_map.get(type_key, type_key or "ช่วงซ้อม")

    cond_obj = step.get("endCondition") or {}
    cond_key = cond_obj.get("conditionTypeKey", "")
    cond_val = step.get("endConditionValue") or 0

    dur_str = ""
    if cond_key == "distance":
        if cond_val >= 1000:
            dur_str = f"{cond_val / 1000:.2f} km"
        else:
            dur_str = f"{int(cond_val)} m"
    elif cond_key == "time":
        m, s = divmod(int(cond_val), 60)
        if m > 0 and s > 0:
            dur_str = f"{m} นาที {s} วิ"
        elif m > 0:
            dur_str = f"{m} นาที"
        else:
            dur_str = f"{s} วิ"
    elif cond_key == "lap.button":
        dur_str = "จนกดปุ่ม Lap"

    target_obj = step.get("targetType") or {}
    target_key = target_obj.get("workoutTargetTypeKey", "")
    target_str = ""
    t1 = step.get("targetValueOne")
    t2 = step.get("targetValueTwo")

    if target_key in ("pace.zone", "speed.zone") and t1 and t2:
        p1 = format_speed_to_pace(t1)
        p2 = format_speed_to_pace(t2)
        if p1 and p2:
            paces = sorted([p1, p2])
            target_str = f" [เพซ {paces[0]} - {paces[1]} /km]"
        elif p1:
            target_str = f" [เพซ {p1} /km]"
    elif target_key == "heart.rate.zone":
        if t1 and t2:
            target_str = f" [HR {int(min(t1, t2))}-{int(max(t1, t2))} bpm]"
        elif t1:
            target_str = f" [HR {int(t1)} bpm]"

    res = f"{type_name}"
    if dur_str:
        res += f" {dur_str}"
    if target_str:
        res += target_str
    return res


def summarize_workout_steps_short(detail: dict) -> str:
    """สรุปขั้นตอนการซ้อมจาก workout detail เป็นประโยคสั้นๆ อ่านง่าย"""
    if not detail or not isinstance(detail, dict):
        return ""
    segs = detail.get("workoutSegments", [])
    if not segs:
        return ""

    parts = []
    for seg in segs:
        steps = seg.get("workoutSteps", [])
        for step in steps:
            step_type = step.get("type", "")
            if step_type == "RepeatGroupDTO" or "numberOfIterations" in step:
                iters = step.get("numberOfIterations", 1)
                sub_steps = step.get("workoutSteps", [])
                sub_strs = [parse_step(st) for st in sub_steps]
                sub_combined = " ➔ ".join(sub_strs)
                parts.append(f"{iters}x [{sub_combined}]")
            else:
                s_str = parse_step(step)
                if s_str:
                    parts.append(s_str)

    return " ➔ ".join(parts)


def summarize_workout_detail(workout_item: dict, detail: dict = None) -> str:
    """สรุปข้อมูลการซ้อมครบถ้วนทั้งชื่อ รายละเอียด และขั้นตอนการซ้อมสำหรับ AI Prompt"""
    if not workout_item:
        return "พักผ่อน (Rest Day)"

    title = workout_item.get("title", "วิ่ง")
    desc = workout_item.get("description", "").strip()
    if not desc and detail:
        desc = detail.get("description", "").strip()

    steps_summary = ""
    if detail:
        steps_summary = summarize_workout_steps_short(detail)

    lines = [f"• ชื่อโปรแกรม: {title}"]
    if desc:
        lines.append(f"• คำอธิบาย/เป้าหมาย: {desc}")
    if steps_summary:
        lines.append(f"• ขั้นตอนการซ้อม (Steps): {steps_summary}")
    return "\n".join(lines)


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
                        i["detail"] = detail
                    except Exception:
                        i["description"] = ""
                        i["detail"] = None
                    upcoming.append(i)
                    if i.get("date") == today_str and not today_workout:
                        today_workout = i

        upcoming.sort(key=lambda x: x.get("date"))

        today_detail = today_workout.get("detail") if today_workout else None
        today_workout_detail_text = summarize_workout_detail(today_workout, today_detail)
        today_steps_short = summarize_workout_steps_short(today_detail) if today_detail else ""

        # ตารางซ้อมวันอื่นๆ ในสัปดาห์ เพื่อให้ AI เข้าใจบริบทภาพรวม
        weekly_summary_items = []
        for w in upcoming:
            w_d = w.get("date", "")
            if w_d != today_str:
                w_title = w.get("title", "Run")
                weekly_summary_items.append(f"{w_d}: {w_title}")
        weekly_context_str = ", ".join(weekly_summary_items) if weekly_summary_items else "ไม่มีรายการซ้อมอื่นใน 7 วันนี้"

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

        # 3. สร้างคำแนะนำโค้ช AI สั้นๆ จาก Gemini โดยอิงจากตารางซ้อมจริง
        advice = "พร้อมสำหรับการซ้อมวันนี้ คุมเพซตามแผนที่กำหนด"
        if GEMINI_API_KEY and genai:
            try:
                client = genai.Client(api_key=GEMINI_API_KEY)
                profile = get_athlete_profile()
                profile_context = format_athlete_profile_summary(profile)
                prompt = (
                    f"คุณคือ Personal Running Coach มืออาชีพ\n"
                    f"บริบทวันที่และตารางซ้อม:\n"
                    f"- วันนี้วันที่: {today_str}\n"
                    f"- รายละเอียดตารางซ้อมวันนี้จาก Garmin Connect:\n{today_workout_detail_text}\n"
                    f"- ตารางซ้อมวันอื่นๆ ในสัปดาห์นี้: {weekly_context_str}\n"
                    f"- สภาพร่างกายเช้านี้:\n"
                    f"  • Training Readiness: {readiness_score} ({readiness_level})\n"
                    f"  • การนอนหลับ: {sleep_text}\n\n"
                    f"{profile_context}\n"
                    f"กฎสำคัญในการให้คำแนะนำ:\n"
                    f"1. ตารางซ้อมใน Garmin คือ Single Source of Truth ห้ามเปลี่ยนโปรแกรมหรือเปลี่ยนประเภทการซ้อมเด็ดขาด\n"
                    f"2. หากวันนี้เป็น Rest Day หรือไม่มีแผนวิ่ง: ห้ามสั่งให้ไปวิ่งเด็ดขาด ให้แนะนำการฟื้นฟู ยืดเหยียด หรือพักผ่อน\n"
                    f"3. หากวันนี้เป็น Easy Run หรือ Recovery: กำชับให้คุมเพซและหัวใจให้อยู่ต่ำกว่า LT1 อย่างเคร่งครัด (เพซ 6:00-6:40 /km หรือ HR < 172 bpm) ห้ามแนะนำให้เร่งความเร็ว\n"
                    f"4. หาก Readiness ต่ำ หรือนอนน้อย: ให้แนะนำวิธีปรับความหนักเบา 'ภายใต้โปรแกรมเดิม' เช่น วิ่งที่ขอบช้าสุดของโซน (6:40 /km) หรือลดระยะทางเล็กน้อยเพื่อป้องกันอาการล้าสะสม\n"
                    f"5. คำนึงถึงโปรแกรมวันพรุ่งนี้/วันถัดไป เช่น หากพรุ่งนี้มีซ้อมหนัก วันนี้ต้องเน้นเก็บแรง\n"
                    f"ให้เขียนคำแนะนำการซ้อมสำหรับวันนี้แบบสั้น กระชับ ตรงประเด็น (ความยาว 2-3 บรรทัด):"
                )
                advice = call_gemini_with_fallback(client, prompt)
            except Exception as e:
                print(f"[AI] Daily advice generation failed: {e}")
                advice = "พร้อมสำหรับการซ้อมวันนี้ คุมเพซตามแผนที่กำหนดและสังเกตสัญญาณชีพจรของร่างกาย"

        # 4. ประกอบข้อความ
        today_info = "🛌 พักผ่อน (Rest Day)"
        if today_workout:
            desc = today_workout.get("description", "").strip()
            desc_str = f"\n  เป้าหมาย: {desc}" if desc else ""
            steps_str = f"\n  ขั้นตอน: {today_steps_short}" if today_steps_short else ""
            today_info = f"🏃 {today_workout.get('title')}{desc_str}{steps_str}"

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
    """สร้างรายงานตารางซ้อมของวันพรุ่งนี้ พร้อมนำข้อมูลของวันนี้/วันก่อนหน้ามาวิเคราะห์เตรียมตัว"""
    try:
        garmin = get_garmin()
        tz_bkk = timezone(timedelta(hours=7))
        now = datetime.now(tz_bkk)
        today = now.date()
        today_str = today.isoformat()
        yesterday = today - timedelta(days=1)
        yesterday_str = yesterday.isoformat()
        tomorrow = today + timedelta(days=1)
        tomorrow_str = tomorrow.isoformat()
        end_date = tomorrow + timedelta(days=7)

        # 1. ดึง Calendar Workouts สำหรับวันพรุ่งนี้ จาก Garmin
        cal1 = garmin.connectapi(f"/calendar-service/year/{tomorrow.year}/month/{tomorrow.month - 1}")
        items = cal1.get("calendarItems", [])
        if end_date.month != tomorrow.month:
            try:
                cal2 = garmin.connectapi(f"/calendar-service/year/{end_date.year}/month/{end_date.month - 1}")
                items.extend(cal2.get("calendarItems", []))
            except Exception:
                pass

        tomorrow_workout = None
        upcoming_after = []
        for i in items:
            if i.get("itemType") == "workout":
                w_d = i.get("date", "")
                if w_d == tomorrow_str and not tomorrow_workout:
                    wid = i.get("workoutId")
                    try:
                        detail = garmin.connectapi(f"/workout-service/workout/{wid}")
                        i["description"] = detail.get("description", "").strip()
                        i["detail"] = detail
                    except Exception:
                        i["description"] = ""
                        i["detail"] = None
                    tomorrow_workout = i
                elif tomorrow_str < w_d <= end_date.isoformat():
                    upcoming_after.append(f"{w_d}: {i.get('title', 'Run')}")

        tomorrow_detail = tomorrow_workout.get("detail") if tomorrow_workout else None
        tomorrow_workout_detail_text = summarize_workout_detail(tomorrow_workout, tomorrow_detail)
        tomorrow_steps_short = summarize_workout_steps_short(tomorrow_detail) if tomorrow_detail else ""
        upcoming_context_str = ", ".join(upcoming_after[:3]) if upcoming_after else "ไม่มี"

        # 2. ดึงข้อมูลการซ้อมและกิจกรรมของ วันนี้ หรือ วันก่อนหน้า
        activities = []
        try:
            activities = garmin.get_activities(0, 5)
        except Exception:
            pass

        today_acts = [a for a in activities if a.get("startTimeLocal", "")[:10] == today_str]
        yesterday_acts = [a for a in activities if a.get("startTimeLocal", "")[:10] == yesterday_str]

        today_activity_summary = "พักผ่อน (ไม่มีบันทึกกิจกรรม)"
        if today_acts:
            act_descs = []
            for a in today_acts:
                name = a.get("activityName", "Activity")
                dist = (a.get("distance") or 0) / 1000
                dur = (a.get("duration") or 0) / 60
                speed = a.get("averageSpeed", 0)
                pace_str = ""
                if speed > 0:
                    sec_per_km = 1000 / speed
                    p_min, p_sec = divmod(int(sec_per_km), 60)
                    pace_str = f" | Pace: {p_min}:{p_sec:02d}"
                act_descs.append(f"{name} ({dist:.2f} km, {dur:.0f} นาที{pace_str})")
            today_activity_summary = ", ".join(act_descs)
        elif yesterday_acts:
            act_descs = []
            for a in yesterday_acts:
                name = a.get("activityName", "Activity")
                dist = (a.get("distance") or 0) / 1000
                act_descs.append(f"{name} {dist:.2f} km")
            today_activity_summary = f"พักผ่อน (วันก่อนหน้าซ้อม: {', '.join(act_descs)})"

        # 3. ดึงสถานะความพร้อมร่างกายปัจจุบัน (Training Readiness & Sleep)
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

        # 4. สร้างคำแนะนำเตรียมตัวล่วงหน้าจาก Gemini โดยเชื่อมโยงข้อมูลวันนี้กับพรุ่งนี้
        advice = "พักผ่อนคืนนี้ให้เพียงพอและเตรียมพร้อมสำหรับตารางซ้อมพรุ่งนี้"
        if GEMINI_API_KEY and genai:
            try:
                client = genai.Client(api_key=GEMINI_API_KEY)
                profile = get_athlete_profile()
                profile_context = format_athlete_profile_summary(profile)
                prompt = (
                    f"คุณคือ Personal Running Coach มืออาชีพ\n"
                    f"ข้อมูลประกอบการวิเคราะห์:\n"
                    f"- วันนี้วันที่: {today_str}\n"
                    f"- กิจกรรมการซ้อมวันนี้: {today_activity_summary}\n"
                    f"- สภาพร่างกายปัจจุบัน: Training Readiness {readiness_score} ({readiness_level}), การนอนหลับ {sleep_text}\n"
                    f"- วันพรุ่งนี้วันที่: {tomorrow_str}\n"
                    f"- รายละเอียดตารางซ้อมวันพรุ่งนี้จาก Garmin Connect:\n{tomorrow_workout_detail_text}\n"
                    f"- รายการซ้อมถัดไปในสัปดาห์: {upcoming_context_str}\n\n"
                    f"{profile_context}\n"
                    f"กฎสำคัญในการให้คำแนะนำ:\n"
                    f"1. แผนซ้อมวันพรุ่งนี้ที่กำหนดใน Garmin คือ Single Source of Truth ห้ามเปลี่ยนโปรแกรมหรือประเภทการซ้อมเด็ดขาด\n"
                    f"2. หากพรุ่งนี้เป็น Rest Day หรือไม่มีแผนวิ่ง: แนะนำการพักผ่อนคืนนี้และวันพรุ่งนี้ ห้ามสั่งให้วิ่ง\n"
                    f"3. หากพรุ่งนี้เป็น Easy Run: ย้ำการคุมเพซต่ำกว่า LT1 (เพซ 6:00-6:40 /km หรือ HR < 172 bpm) ห้ามสั่งเร่งความเร็ว\n"
                    f"4. หากพรุ่งนี้เป็น Tempo / Interval / ซ้อมหนัก: แนะนำการเตรียมตัวล่วงหน้าคืนนี้ (การนอน โภชนาการ น้ำดื่ม) และกำหนดเพซเป้าหมายตามผลแลคเตท (LT2: 4:37, Interval: < 4:17)\n"
                    f"5. วิเคราะห์เชื่อมโยงกับกิจกรรมและความล้าของวันนี้ เพื่อแนะนำการฟื้นฟูและการปรับตัว\n"
                    f"ให้เขียนคำแนะนำเตรียมตัวสำหรับวันพรุ่งนี้แบบสั้น กระชับ ตรงประเด็น (ความยาว 2-3 บรรทัด):"
                )
                advice = call_gemini_with_fallback(client, prompt)
            except Exception as e:
                print(f"[AI] Tomorrow advice generation failed: {e}")
                advice = "เตรียมความพร้อมสำหรับการซ้อมพรุ่งนี้ พักผ่อนให้เพียงพอและสังเกตสภาพร่างกาย"

        tomorrow_info = "🛌 พักผ่อน (Rest Day)"
        if tomorrow_workout:
            desc = tomorrow_workout.get("description", "").strip()
            desc_str = f"\n  เป้าหมาย: {desc}" if desc else ""
            steps_str = f"\n  ขั้นตอน: {tomorrow_steps_short}" if tomorrow_steps_short else ""
            tomorrow_info = f"🏃 {tomorrow_workout.get('title')}{desc_str}{steps_str}"

        lines = [
            f"🌅 แผนการซ้อมวันพรุ่งนี้ ({tomorrow_str})",
            f"━━━━━━━━━━━━━━━━━━━",
            f"🎯 แผนซ้อมพรุ่งนี้:",
            f"{tomorrow_info}",
            f"",
            f"📊 สภาพร่างกาย & การซ้อมวันนี้ ({today_str}):",
            f"• การซ้อมวันนี้: {today_activity_summary}",
            f"• Training Readiness: {readiness_score} ({readiness_level})",
            f"• การนอนหลับ: {sleep_text}",
            f"",
            f"💡 คำแนะนำเตรียมตัวจากโค้ช AI:",
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
    tz_bkk = timezone(timedelta(hours=7))
    today_date = datetime.now(tz_bkk).date()
    today = today_date.isoformat()
    tomorrow_date = today_date + timedelta(days=1)
    tomorrow_str = tomorrow_date.isoformat()

    try:
        garmin = get_garmin()
        context["today_summary"] = garmin.get_user_summary(today)
        context["race_predictions"] = garmin.get_race_predictions()
        context["training_readiness"] = garmin.get_training_readiness(today)
        context["vo2max"] = garmin.get_max_metrics(today)

        # 1. ข้อมูลกิจกรรมการวิ่งและระยะสะสมจริง (คำนวณตามสัปดาห์ ไม่รวมกิจกรรมทั้งหมด)
        acts = garmin.get_activities(0, 20)
        running_acts = [
            a for a in acts
            if "run" in a.get("activityType", {}).get("typeKey", "").lower()
        ]

        start_of_week = today_date - timedelta(days=today_date.weekday())
        start_of_week_str = start_of_week.isoformat()
        seven_days_ago_str = (today_date - timedelta(days=7)).isoformat()

        this_week_runs = [a for a in running_acts if (a.get("startTimeLocal", "")[:10]) >= start_of_week_str]
        this_week_km = sum((a.get("distance") or 0) for a in this_week_runs) / 1000.0

        past_7d_runs = [a for a in running_acts if (a.get("startTimeLocal", "")[:10]) >= seven_days_ago_str]
        past_7d_km = sum((a.get("distance") or 0) for a in past_7d_runs) / 1000.0

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
        context["running_mileage_stats"] = {
            "this_week_km_since_monday": round(this_week_km, 2),
            "past_7_days_total_km": round(past_7d_km, 2),
            "recent_5_runs": recent_summaries
        }

        # 2. ดึงตารางซ้อมในปฏิทิน Garmin สัปดาห์นี้ เพื่อใช้เป็น Single Source of Truth
        try:
            cal = garmin.connectapi(f"/calendar-service/year/{today_date.year}/month/{today_date.month - 1}")
            items = cal.get("calendarItems", [])
            end_date = today_date + timedelta(days=7)
            if end_date.month != today_date.month:
                try:
                    cal2 = garmin.connectapi(f"/calendar-service/year/{end_date.year}/month/{end_date.month - 1}")
                    items.extend(cal2.get("calendarItems", []))
                except Exception:
                    pass
            scheduled_list = []
            for i in items:
                if i.get("itemType") == "workout":
                    w_d = i.get("date", "")
                    if today <= w_d <= end_date.isoformat():
                        wid = i.get("workoutId")
                        w_detail = None
                        try:
                            w_detail = garmin.connectapi(f"/workout-service/workout/{wid}")
                        except Exception:
                            pass
                        tag = ""
                        if w_d == today:
                            tag = " [วันนี้]"
                        elif w_d == tomorrow_str:
                            tag = " [พรุ่งนี้]"
                        scheduled_list.append({
                            "date": f"{w_d}{tag}",
                            "title": i.get("title", "Run"),
                            "detail": summarize_workout_detail(i, w_detail)
                        })
            scheduled_list.sort(key=lambda x: x.get("date"))
            context["calendar_scheduled_workouts"] = scheduled_list if scheduled_list else "ไม่มีตารางซ้อมที่ตั้งไว้ในปฏิทิน 7 วันนี้ (ถือเป็นวันพักผ่อนตามอัธยาศัย)"
        except Exception as e:
            context["calendar_scheduled_workouts_note"] = f"ไม่สามารถดึงตารางได้: {e}"

    except Exception as e:
        context["fetch_note"] = f"บางส่วนของข้อมูล Garmin ไม่พร้อมใช้งาน: {e}"

    athlete_profile = get_athlete_profile()
    if athlete_profile:
        context["athlete_lactate_profile"] = athlete_profile

    profile_summary = format_athlete_profile_summary(athlete_profile)

    system_prompt = (
        "คุณคือ Personal Running Coach มืออาชีพ ให้คำปรึกษาแผนการซ้อมวิ่ง วิเคราะห์สมรรถภาพ และการดูแลร่างกาย "
        "โดยอิงจากข้อมูลจริงจาก Garmin Connect และผลการทดสอบ Lactate Threshold (LT1, LT2) รวมถึงโซนหัวใจและเพซจริงของคุณพรเทพที่ให้มา\n"
        "กฎเหล็กสำคัญที่ต้องปฏิบัติตามอย่างเคร่งครัด:\n"
        "1. ตารางซ้อมในปฏิทิน Garmin (calendar_scheduled_workouts) คือแผนการซ้อมหลักที่ผู้ใช้กำหนดไว้ (Single Source of Truth) "
        "ห้ามคิดโปรแกรมใหม่ขึ้นมาขัดแย้งกับตารางเดิมเด็ดขาด หากในปฏิทินมีโปรแกรมอยู่แล้ว ให้แนะนำวิธีการปฏิบัติตามแผนนั้นให้สำเร็จอย่างปลอดภัย "
        "หรือหากในปฏิทินเป็น Rest Day หรือไม่มีแผนวิ่ง ห้ามสั่งให้ไปวิ่งเด็ดขาด ให้แนะนำการฟื้นฟู ยืดเหยียด หรือพักผ่อน\n"
        "2. ห้ามสับสนระยะวิ่งสะสม: หากจะพูดถึงระยะสะสมรอบสัปดาห์ ให้อ้างอิงจาก 'this_week_km_since_monday' หรือ 'past_7_days_total_km' เท่านั้น "
        "ห้ามนำผลรวมของประวัติการวิ่ง 20 รายการย้อนหลังมาเหมาว่าเป็นระยะสัปดาห์เด็ดขาด\n"
        "3. หากแผนซ้อมเป็น Easy Run หรือ Recovery: กำชับให้คุมเพซและ HR ให้อยู่ต่ำกว่า LT1 อย่างเคร่งครัด (เพซ 6:00-6:40 /km หรือ HR < 172 bpm) ห้ามแนะนำให้เร่งความเร็ว\n"
        "4. กำหนด Pace หรือ Heart Rate โดยยึดตามผล Lactate Test (LT1 เพซ 5:27 / HR 172, LT2 เพซ 4:37 / HR 187, Easy 6:00-6:40 /km) ของคุณพรเทพอย่างเคร่งครัด\n"
        "5. คำตอบต้องตรงประเด็น นำไปปฏิบัติได้จริง (Actionable) แบ่งหัวข้อให้อ่านง่ายในแชต LINE"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            f"{system_prompt}\n\n"
            f"{profile_summary}\n\n"
            f"[ข้อมูล Garmin Connect ล่าสุดของผู้ใช้]:\n{context}\n\n"
            f"[คำถามจากผู้ใช้]: {question}\n\n"
            f"กรุณาตอบคำแนะนำอย่างเป็นมืออาชีพและกระชับ:"
        )

        return call_gemini_with_fallback(client, prompt)
    except Exception as e:
        return f"❌ เกิดข้อผิดพลาดในการประมวลผลคำตอบจาก Gemini: {e}"


# -------------------------------------------------------------
# ระบบออกแบบตารางซ้อมแบบ Custom (AI Coach & Garmin Sync)
# -------------------------------------------------------------
DRAFT_PLAN_FILE = PROJECT_DIR / "latest_draft_plan.json"

THAI_MONTHS = {
    "ม.ค.": 1, "มกรา": 1, "มกราคม": 1,
    "ก.พ.": 2, "กุมภา": 2, "กุมภาพันธ์": 2,
    "มี.ค.": 3, "มีนา": 3, "มีนาคม": 3,
    "เม.ย.": 4, "เมษา": 4, "เมษายน": 4,
    "พ.ค.": 5, "พฤษภา": 5, "พฤษภาคม": 5,
    "มิ.ย.": 6, "มิถุนา": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กรกฎา": 7, "กรกฎาคม": 7,
    "ส.ค.": 8, "สิงหา": 8, "สิงหาคม": 8,
    "ก.ย.": 9, "กันยา": 9, "กันยายน": 9,
    "ต.ค.": 10, "ตุลา": 10, "ตุลาคม": 10,
    "พ.ย.": 11, "พฤศจิกา": 11, "พฤศจิกายน": 11,
    "ธ.ค.": 12, "ธันวา": 12, "ธันวาคม": 12,
}


def clear_draft_plan():
    """ลบแบบร่างตารางซ้อมที่รอการยืนยัน"""
    try:
        if DRAFT_PLAN_FILE.exists():
            DRAFT_PLAN_FILE.unlink()
            print("[PLAN] Cleared pending draft plan.")
    except Exception as e:
        print(f"[PLAN] Error clearing draft plan: {e}")


def save_draft_plan(user_id: str, plan_data: dict):
    """บันทึกแบบร่างตารางซ้อมลงไฟล์ JSON"""
    try:
        payload = {
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            "data": plan_data,
        }
        with open(DRAFT_PLAN_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[PLAN] Saved draft plan for user {user_id}")
    except Exception as e:
        print(f"[PLAN] Error saving draft plan: {e}")


def load_draft_plan(user_id: str = None) -> dict:
    """โหลดแบบร่างตารางซ้อมล่าสุด"""
    try:
        if not DRAFT_PLAN_FILE.exists():
            return None
        with open(DRAFT_PLAN_FILE, "r", encoding="utf-8") as f:
            content = json.load(f)

        if user_id and content.get("user_id") and content.get("user_id") != user_id:
            return None

        created_at_str = content.get("timestamp")
        if created_at_str:
            created_dt = datetime.fromisoformat(created_at_str)
            if (datetime.now() - created_dt).total_seconds() > 7200:
                clear_draft_plan()
                return None
        return content.get("data")
    except Exception as e:
        print(f"[PLAN] Error loading draft plan: {e}")
        return None


def parse_workout_redesign_request(text: str, current_date: date):
    """วิเคราะห์ข้อความเพื่อระบุเป้าหมายระยะทาง (มาราธอน, ฮาล์ฟ, 10k, 5k) และวันสิ้นสุด"""
    text_lower = text.lower()

    # 1. ตรวจจับเป้าหมายระยะทาง
    target_goal = "การพัฒนาความฟิตทั่วไป (General Fitness)"
    if any(k in text_lower for k in ["มาราธอน", "ฟูลมาราธอน", "marathon", "full marathon", "42.195", "42k", "42 k"]):
        if not any(k in text_lower for k in ["ฮาล์ฟ", "ฮาฟ", "half", "มินิ", "mini"]):
            target_goal = "ฟูลมาราธอน (Full Marathon 42.195 km)"
    if any(k in text_lower for k in ["ฮาล์ฟ", "ฮาฟ", "half", "half marathon", "21.1", "21k", "21 k"]):
        target_goal = "ฮาล์ฟมาราธอน (Half Marathon 21.1 km)"
    elif any(k in text_lower for k in ["10k", "10 k", "10km", "มินิ", "มินิมาราธอน", "mini marathon", "10 กิโล", "10กิโล"]):
        target_goal = "มินิมาราธอน (Mini Marathon 10 km)"
    elif any(k in text_lower for k in ["5k", "5 k", "5km", "5 กิโล", "5กิโล"]):
        target_goal = "ระยะ 5 กิโลเมตร (5 km Run)"

    # 2. ตรวจจับวันสิ้นสุด (ค่าเริ่มต้น 7 วัน รวมวันนี้)
    end_date = current_date + timedelta(days=6)

    # 2.1 แบบระบุวันและชื่อเดือนไทย เช่น "ถึง 10 ต.ค.", "ถึงวันที่ 10 ตุลาคม"
    month_pattern = "|".join(re.escape(k) for k in sorted(THAI_MONTHS.keys(), key=len, reverse=True))
    date_match = re.search(r'(?:ถึง|จนถึง|ก่อน)\s*(?:วันที่)?\s*(\d{1,2})\s*(' + month_pattern + r')(?:\s*(\d{2,4}))?', text_lower)

    if date_match:
        day = int(date_match.group(1))
        month_str = date_match.group(2)
        month = THAI_MONTHS.get(month_str, current_date.month)
        year = current_date.year
        if date_match.group(3):
            y_val = int(date_match.group(3))
            year = y_val - 543 if y_val > 2400 else y_val
        else:
            if month < current_date.month or (month == current_date.month and day < current_date.day):
                year += 1
        try:
            target_dt = date(year, month, day)
            if target_dt >= current_date:
                end_date = target_dt
        except ValueError:
            pass
    else:
        # 2.2 แบบตัวเลข เช่น "ถึง 10/10"
        num_date_match = re.search(r'(?:ถึง|จนถึง)\s*(?:วันที่)?\s*(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?', text_lower)
        if num_date_match:
            day = int(num_date_match.group(1))
            month = int(num_date_match.group(2))
            year = current_date.year
            if num_date_match.group(3):
                y_val = int(num_date_match.group(3))
                year = y_val - 543 if y_val > 2400 else y_val
            else:
                if month < current_date.month or (month == current_date.month and day < current_date.day):
                    year += 1
            try:
                target_dt = date(year, month, day)
                if target_dt >= current_date:
                    end_date = target_dt
            except ValueError:
                pass
        else:
            # 2.3 แบบระบุระยะเวลา เช่น "2 สัปดาห์", "10 วัน"
            dur_match = re.search(r'(\d{1,2})\s*(วัน|สัปดาห์|week)', text_lower)
            if dur_match:
                val = int(dur_match.group(1))
                unit = dur_match.group(2)
                if "สัปดาห์" in unit or "week" in unit:
                    end_date = current_date + timedelta(days=(val * 7) - 1)
                else:
                    end_date = current_date + timedelta(days=max(1, val) - 1)

    # จำกัดวันให้อยู่ในช่วง 1 - 28 วันเพื่อความเหมาะสมของ API
    days_diff = (end_date - current_date).days + 1
    if days_diff > 28:
        end_date = current_date + timedelta(days=27)
        days_diff = 28
    elif days_diff < 1:
        end_date = current_date
        days_diff = 1

    return target_goal, end_date, days_diff


def generate_custom_training_plan(user_id: str, user_text: str) -> str:
    """สร้างแผนการซ้อมวิ่งแบบ Custom จากข้อมูลจริงของ Garmin Connect ร่วมกับ Gemini AI"""
    if not GEMINI_API_KEY:
        return "💡 ไม่พบ GEMINI_API_KEY กรุณากำหนดในไฟล์ .env เพื่อใช้งานการออกแบบตารางซ้อม"

    tz_bkk = timezone(timedelta(hours=7))
    current_dt = datetime.now(tz_bkk).date()
    target_goal, end_date, total_days = parse_workout_redesign_request(user_text, current_dt)
    start_date_str = current_dt.isoformat()
    end_date_str = end_date.isoformat()

    context = {}
    try:
        garmin = get_garmin()
        context["today_summary"] = garmin.get_user_summary(start_date_str)
        context["race_predictions"] = garmin.get_race_predictions()
        context["training_readiness"] = garmin.get_training_readiness(start_date_str)
        context["vo2max"] = garmin.get_max_metrics(start_date_str)
        acts = garmin.get_activities(0, 10)
        running_acts = [
            a for a in acts
            if "run" in a.get("activityType", {}).get("typeKey", "").lower()
        ]
        context["recent_runs"] = [
            {
                "date": a.get("startTimeLocal", "")[:10],
                "distance_km": round((a.get("distance") or 0) / 1000, 2),
                "duration_min": round((a.get("duration") or 0) / 60, 1),
                "avg_hr": a.get("averageHR"),
            }
            for a in running_acts[:5]
        ]
    except Exception as e:
        context["note"] = f"ไม่สามารถดึงข้อมูล Garmin บางส่วนได้: {e}"

    athlete_profile = get_athlete_profile()
    profile_prompt_part = ""
    if athlete_profile:
        profile_prompt_part = f"\n{format_athlete_profile_summary(athlete_profile)}\n"

    prompt = f"""คุณคือ Personal Running Coach มืออาชีพระดับโอลิมปิก
ภารกิจ: ออกแบบตารางซ้อมวิ่งแบบเฉพาะบุคคลใหม่ทั้งหมด ตั้งแต่วันที่ {start_date_str} ถึง {end_date_str} (รวม {total_days} วัน)
เป้าหมายการแข่งขัน/ฝึกซ้อม: {target_goal}

{profile_prompt_part}
[ข้อมูลสมรรถภาพทางกายจริงจาก Garmin Connect]:
{json.dumps(context, ensure_ascii=False, indent=2)}

หลักเกณฑ์การออกแบบตารางซ้อม:
1. อิงหลักการฝึกซ้อม 80/20 (Easy Run 80% และ Quality Session เช่น Tempo/Interval/Long Run 20%)
2. ใช้เกณฑ์เพซและโซนหัวใจจากผลทดสอบ Lactate ของคุณพรเทพอย่างเคร่งครัด:
   - Easy Run / Recovery / Long Run: ต้องอยู่ต่ำกว่า LT1 (Pace 6:00 - 6:40 /km หรือช้ากว่า 6:40, HR 160-170 bpm หรือ < 172 bpm)
   - Steady Run: อยู่ระหว่าง LT1 และ LT2 (Pace 5:00 - 5:27 /km, HR 172-180 bpm)
   - Tempo / Threshold Run: อยู่ที่จุด LT2 (Pace 4:37 /km, HR 187 bpm)
   - Interval / VO2max: เร็วกว่า LT2 (Pace < 4:17 /km, HR 192+ bpm)
3. คำนึงถึงสมรรถภาพจริง (VO2 Max, เพซ และ Training Readiness ปัจจุบัน)
4. ต้องมีวันพัก (Rest Day) 1-2 วันต่อสัปดาห์เพื่อการฟื้นตัวอย่างมีประสิทธิภาพ
5. ระบุระยะทาง (km), เพซเป้าหมาย (อิงตามเกณฑ์แลคเตทข้างต้น) และโครงสร้างการวิ่ง (Warmup, Main, Cooldown) ชัดเจน
6. ตอบกลับเฉพาะโครงสร้าง JSON array ที่ถูกต้อง (Valid JSON array) เท่านั้น ห้ามใส่คำทักทายหรือ markdown code block อื่น นอกเหนือจาก JSON array:

[
  {{
    "date": "YYYY-MM-DD",
    "day_name": "วันอังคาร",
    "workout_type": "Easy Run" | "Tempo Run" | "Interval" | "Long Run" | "Recovery Run" | "Rest Day",
    "title": "Easy Run 5K",
    "distance_km": 5.0,
    "target_pace": "6:15 - 6:30 /km",
    "warmup_sec": 300,
    "cooldown_sec": 300,
    "notes": "วิ่งสบายๆ โซน 2 คุมการหายใจ"
  }}
]"""

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        raw_text = call_gemini_with_fallback(client, prompt)
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)

        workout_list = json.loads(raw_text)

        plan_data = {
            "target_goal": target_goal,
            "start_date": start_date_str,
            "end_date": end_date_str,
            "total_days": total_days,
            "workouts": workout_list,
        }

        save_draft_plan(user_id, plan_data)
        return format_custom_plan_for_chat(plan_data)
    except Exception as e:
        return f"❌ เกิดข้อผิดพลาดในการออกแบบตารางซ้อมจาก AI: {e}"


def format_custom_plan_for_chat(plan_data: dict) -> str:
    """จัดรูปแบบข้อความตารางซ้อมสำหรับแสดงในแชต LINE"""
    goal = plan_data.get("target_goal", "")
    start_date = plan_data.get("start_date", "")
    end_date = plan_data.get("end_date", "")
    workouts = plan_data.get("workouts", [])

    lines = [
        "🏃 แผนซ้อมวิ่งใหม่ (AI Coach Redesign)",
        "━━━━━━━━━━━━━━━━━━━",
        f"🎯 เป้าหมาย: {goal}",
        f"📅 ช่วงเวลา: {start_date} ถึง {end_date} ({len(workouts)} วัน)",
        "━━━━━━━━━━━━━━━━━━━",
    ]

    type_emojis = {
        "easy run": "🟢",
        "recovery run": "🟢",
        "tempo run": "🟡",
        "tempo": "🟡",
        "interval": "🔴",
        "intervals": "🔴",
        "long run": "🟣",
        "rest day": "🛌",
        "rest": "🛌",
    }

    for w in workouts:
        w_date = w.get("date", "")
        day_name = w.get("day_name", "")
        w_type = w.get("workout_type", "Run")
        w_title = w.get("title", w_type)
        dist = w.get("distance_km", 0)
        pace = w.get("target_pace", "-")
        notes = w.get("notes", "").strip()

        emoji = type_emojis.get(w_type.lower(), "🏃")

        if "rest" in w_type.lower() or dist == 0:
            lines.append(f"📅 [{w_date}] {day_name}: {emoji} {w_title}")
            if notes:
                lines.append(f"   💡 {notes}")
        else:
            lines.append(f"📅 [{w_date}] {day_name}: {emoji} {w_title}")
            lines.append(f"   • ระยะทาง: {dist} km | เพซ: {pace}")
            if notes:
                lines.append(f"   • รายละเอียด: {notes}")
        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━",
        "❓ ต้องการอัปเดตตารางนี้เข้า Garmin Connect Calendar เลยไหม?",
        "👉 พิมพ์ 'ยืนยันอัปเดตตาราง' เพื่อส่งเข้า Garmin ทันที",
        "(หากพิมพ์ถามเรื่องอื่น จะถือว่ายกเลิกแบบร่างนี้อัตโนมัติ)",
    ])

    return "\n".join(lines)


def push_plan_to_garmin(user_id: str) -> str:
    """บันทึกแบบร่างตารางซ้อมเข้าสู่ Garmin Connect (สร้าง Workout และ Schedule ลง Calendar)"""
    plan_data = load_draft_plan(user_id)
    if not plan_data:
        return "⚠️ ไม่พบแบบร่างตารางซ้อมล่าสุด กรุณาพิมพ์ 'อยากให้ออกแบบตารางซ้อมสัปดาห์นี้ใหม่' เพื่อให้ AI วิเคราะห์ก่อน"

    try:
        garmin = get_garmin()
    except Exception as e:
        return f"❌ ไม่สามารถเชื่อมต่อ Garmin Connect ได้: {e}"

    workouts = plan_data.get("workouts", [])
    created_count = 0
    success_list = []
    fail_list = []

    for w in workouts:
        w_type = w.get("workout_type", "")
        dist_km = float(w.get("distance_km") or 0)
        w_date = w.get("date", "")

        # ข้ามวันพัก
        if "rest" in w_type.lower() or dist_km <= 0:
            continue

        raw_title = w.get("title", "Run")
        w_title = f"AI: {raw_title}"[:45]
        warmup_sec = int(w.get("warmup_sec") or 300)
        cooldown_sec = int(w.get("cooldown_sec") or 300)
        dist_meters = dist_km * 1000
        desc = f"{w.get('target_pace', '')} | {w.get('notes', '')}".strip()[:200]

        steps = []
        order = 1
        if warmup_sec > 0:
            steps.append({
                "type": "ExecutableStepDTO",
                "stepOrder": order,
                "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": float(warmup_sec),
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            })
            order += 1

        steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": order,
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
            "endCondition": {"conditionTypeId": 1, "conditionTypeKey": "distance"},
            "endConditionValue": float(dist_meters),
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
        })
        order += 1

        if cooldown_sec > 0:
            steps.append({
                "type": "ExecutableStepDTO",
                "stepOrder": order,
                "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": float(cooldown_sec),
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            })

        payload = {
            "workoutName": w_title,
            "description": desc,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
            "workoutSegments": [
                {
                    "segmentOrder": 1,
                    "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
                    "workoutSteps": steps,
                }
            ],
        }

        try:
            workout_res = garmin.connectapi("/workout-service/workout", method="POST", json=payload)
            workout_id = workout_res.get("workoutId") or workout_res.get("workout_id")
            if workout_id:
                garmin.connectapi(f"/workout-service/schedule/{workout_id}", method="POST", json={"date": w_date})
                created_count += 1
                success_list.append(f"• [{w_date}] {w_title} ({dist_km} km)")
            else:
                fail_list.append(f"• [{w_date}] ไม่ได้รับ workoutId จาก Garmin")
        except Exception as e:
            print(f"[GARMIN API] Failed to schedule workout for {w_date}: {e}")
            fail_list.append(f"• [{w_date}] เกิดข้อผิดพลาด: {e}")

    # ล้างแบบร่างหลังดำเนินการ
    clear_draft_plan()

    lines = [
        "✅ บันทึกตารางซ้อมเข้าสู่ Garmin Connect เรียบร้อยแล้ว!" if created_count > 0 else "❌ ไม่สามารถบันทึกตารางซ้อมเข้า Garmin ได้",
        "━━━━━━━━━━━━━━━━━━━",
        f"📅 เพิ่มลง Calendar สำเร็จ {created_count} รายการ:",
    ]
    if success_list:
        lines.extend(success_list)
    if fail_list:
        lines.append("\n⚠️ รายการที่พบปัญหา:")
        lines.extend(fail_list)

    lines.extend([
        "",
        "📱 ตรวจสอบและ Sync ตารางซ้อมเข้านาฬิกาผ่านแอป Garmin Connect ได้ทันที",
    ])
    return "\n".join(lines)


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
@app.head("/")
def index():
    return PlainTextResponse("OK", status_code=200)


@app.get("/uptime")
@app.head("/uptime")
def uptime_endpoint():
    return PlainTextResponse(handle_server_uptime(), status_code=200)


def _run_morning_report_task(today_str: str):
    global _last_daily_push_date
    try:
        print(f"[CRON] Running background morning report for {today_str}...")
        report_text = handle_daily_workout_report()
        if LINE_ALLOWED_USER_ID:
            push_line(LINE_ALLOWED_USER_ID, report_text)
            _last_daily_push_date = today_str
            print(f"[CRON] Daily report pushed successfully to {LINE_ALLOWED_USER_ID}")
    except Exception as e:
        print(f"[CRON] Error in background morning report: {e}")


@app.get("/cron/daily-workout")
@app.post("/cron/daily-workout")
@app.head("/cron/daily-workout")
def cron_daily_workout(background_tasks: BackgroundTasks):
    """Endpoint สำหรับให้ภายนอก (เช่น cron-job.org) เรียกยิงส่งข้อความตอน 8 โมงเช้า เพื่อปลุก Render"""
    tz_bkk = timezone(timedelta(hours=7))
    today_str = datetime.now(tz_bkk).date().isoformat()

    background_tasks.add_task(_run_morning_report_task, today_str)
    return PlainTextResponse("OK", status_code=200)


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

    # 1. เช็กคำสั่งยืนยันบันทึกตารางซ้อมเข้า Garmin
    confirm_triggers = [
        "ยืนยันอัปเดตตาราง",
        "ยืนยันอัพเดตตาราง",
        "ยืนยันตาราง",
        "อัปเดตเข้า garmin",
        "อัพเดตเข้า garmin",
        "อัปเดตเข้าgarmin",
        "อัพเดตเข้าgarmin",
        "บันทึกตาราง",
        "confirm plan",
    ]
    if any(k == user_text.lower() or k in user_text.lower() for k in confirm_triggers):
        push_res = push_plan_to_garmin(user_id)
        reply_line(event.reply_token, push_res)
        return

    # 2. เช็กคำสั่งยกเลิกแบบร่างตารางซ้อม
    cancel_triggers = ["ยกเลิกตาราง", "ยกเลิกแบบร่าง", "ยกเลิก"]
    if user_text.lower() in cancel_triggers:
        clear_draft_plan()
        reply_line(event.reply_token, "ยกเลิกแบบร่างตารางซ้อมเรียบร้อยแล้ว")
        return

    # 3. หากพิมพ์ข้อความอื่นใด ให้ถือว่ายกเลิกแบบร่างตารางซ้อมเดิมอัตโนมัติ
    clear_draft_plan()

    # 4. เช็กคำสั่งออกแบบตารางซ้อมใหม่ (AI Coach)
    redesign_triggers = [
        "ออกแบบตาราง",
        "จัดตาราง",
        "วางตาราง",
        "ตารางซ้อมใหม่",
        "ออกแบบซ้อม",
    ]
    if any(k in user_text.lower() for k in redesign_triggers):
        custom_plan_msg = generate_custom_training_plan(user_id, user_text)
        reply_line(event.reply_token, custom_plan_msg)
        return

    # 5. เช็กคำสั่ง Help / เมนู
    if user_text.lower() in ["help", "วิธีใช้", "เมนู", "menu"]:
        help_msg = (
            "📋 เมนูคำสั่ง Garmin Assistant:\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "🏃 เช็กตารางซ้อม:\n"
            "  • วันนี้: พิมพ์ 'วันนี้ซ้อมอะไร' หรือ 'ขอตารางวันนี้'\n"
            "  • พรุ่งนี้: พิมพ์ 'พน.ซ้อมอะไร' หรือ 'พรุ่งนี้ซ้อมอะไร'\n"
            "  • ล่วงหน้า 7 วัน: พิมพ์ 'ตารางล่วงหน้า 7 วัน'\n\n"
            "✨ ออกแบบตารางซ้อมใหม่ (AI Coach):\n"
            "  • 'อยากให้ออกแบบตารางซ้อมสัปดาห์นี้ใหม่'\n"
            "  • 'ออกแบบตารางซ้อม ฮาล์ฟ มาราธอน ถึง 10ต.ค.'\n"
            "  • 'จัดตารางซ้อม 10k 2 สัปดาห์'\n"
            "  (รองรับ: มาราธอน, ฮาล์ฟมาราธอน, 10k, 5k)\n\n"
            "📊 สรุปสุขภาพวันนี้:\n"
            "  พิมพ์: 'สถานะ', 'วันนี้', หรือ 'สรุป'\n\n"
            "⚖️ บันทึกน้ำหนัก:\n"
            "  พิมพ์: 'น้ำหนัก 68.5' หรือ 'หนัก 70'\n\n"
            "🏃 ประวัติการวิ่ง:\n"
            "  พิมพ์: 'ประวัติวิ่ง' หรือ 'วิ่งล่าสุด'\n\n"
            "💬 ปรึกษาโค้ช AI:\n"
            "  พิมพ์คำถามทั่วไปได้ทันที เช่น:\n"
            "  - 'เมื่อคืนนอนน้อย วันนี้ควรซ้อมไหม'\n"
            "  - 'HR Zone 2 สำหรับฉันควรอยู่ที่เท่าไร'"
        )
        reply_line(event.reply_token, help_msg)
        return

    # 6. เช็กคำขอตารางซ้อมล่วงหน้า 7 วัน
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

    # 7. เช็กคำขอตารางซ้อมวันพรุ่งนี้
    tomorrow_workout_triggers = [
        "วันพน.ซ้อมอะไร",
        "วันพน ซ้อมอะไร",
        "วันพรุ่งนี้ซ้อมอะไร",
        "วันพรุ่งนี้ ซ้อมอะไร",
        "พน.ซ้อมอะไร",
        "พน ซ้อมอะไร",
        "พน. ซ้อมอะไร",
        "พรุ่งนี้ซ้อมอะไร",
        "พรุ่งนี้ ซ้อมอะไร",
        "ตารางพรุ่งนี้",
        "ขอตารางพรุ่งนี้",
        "พรุ่งนี้วิ่งอะไร",
        "พน วิ่งอะไร",
        "พน.วิ่งอะไร",
        "คำแนะนำพรุ่งนี้",
        "แนะนำพรุ่งนี้",
        "พรุ่งนี้ควรวิ่งยังไง",
        "tomorrow workout",
    ]
    if any(k in user_text.lower() for k in tomorrow_workout_triggers):
        tomorrow_report = handle_tomorrow_workout_report()
        reply_line(event.reply_token, tomorrow_report)
        return

    # 8. เช็กคำขอตารางซ้อมวันนี้
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
        "คำแนะนำวันนี้",
        "คำแนะนำการซ้อม",
        "แนะนำวันนี้",
        "คำแนะนำ",
        "วันนี้ควรวิ่งยังไง",
        "วันนี้ควรซ้อมยังไง",
        "ตารางซ้อมวันนี้",
        "โค้ชวันนี้",
        "workout",
        "workouts",
        "plan",
        "schedule",
    ]
    if any(k in user_text.lower() for k in today_workout_triggers):
        workout_report = handle_daily_workout_report()
        reply_line(event.reply_token, workout_report)
        return

    # 9. เช็กคำสั่งบันทึกน้ำหนัก
    weight_res = handle_record_weight(user_text)
    if weight_res:
        reply_line(event.reply_token, weight_res)
        return

    # 10. เช็กคำสั่งสรุปข้อมูลสุขภาพประจำวัน
    if user_text.lower() in ["สถานะ", "วันนี้", "สรุป", "status", "today"]:
        summary_res = handle_today_summary()
        reply_line(event.reply_token, summary_res)
        return

    # 11. เช็กคำสั่งประวัติการวิ่ง
    if user_text.lower() in ["ประวัติวิ่ง", "วิ่งล่าสุด", "runs", "activities"]:
        runs_res = handle_recent_runs()
        reply_line(event.reply_token, runs_res)
        return

    # 12. เช็กคำสั่งดูข้อมูล Lactate Profile & โซนหัวใจ
    if user_text.lower() in [
        "โซนวิ่ง",
        "โซนหัวใจ",
        "แลคเตท",
        "lactate",
        "zone",
        "zones",
        "profile",
        "ผลแลคเตท",
        "lt1",
        "lt2",
    ]:
        profile_res = handle_show_lactate_profile()
        reply_line(event.reply_token, profile_res)
        return

    # 13. เช็กคำสั่งดูสถานะ Uptime ของเซิร์ฟเวอร์
    if user_text.lower() in [
        "uptime",
        "สถานะเซิร์ฟเวอร์",
        "เซิร์ฟเวอร์",
        "server",
        "render",
        "บอททำงานกี่ชม",
        "รันมากี่ชม",
    ]:
        uptime_res = handle_server_uptime()
        reply_line(event.reply_token, uptime_res)
        return

    # 14. ถามคำถามทั่วไป (Gemini Coach วิเคราะห์ร่วมกับข้อมูล Garmin และ Lactate Profile)
    coach_reply = ask_gemini_coach(user_text)
    reply_line(event.reply_token, coach_reply)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 เริ่มการทำงาน Garmin LINE Bot Server ที่พอร์ต {port}...")
    uvicorn.run("line_bot:app", host="0.0.0.0", port=port, reload=True)
