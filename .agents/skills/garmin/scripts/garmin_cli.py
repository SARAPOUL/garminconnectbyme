#!/usr/bin/env python3
"""
Garmin CLI Helper for Antigravity Skill
ใช้สำหรับดึงข้อมูลสรุปสุขภาพ, กิจกรรม และบันทึกน้ำหนักเข้าสู่ Garmin Connect
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ปิด warning ของ urllib3
import warnings
warnings.filterwarnings("ignore")

try:
    from garminconnect import Garmin
except ImportError:
    print(json.dumps({
        "status": "error",
        "message": "Library garminconnect not installed. Please run: pip install -r requirements.txt"
    }, ensure_ascii=False))
    sys.exit(1)

# หาโฟลเดอร์โปรเจกต์ (ขึ้นไป 4 ลำดับจาก scripts/)
PROJECT_DIR = Path(__file__).resolve().parents[4]
TOKEN_DIR = PROJECT_DIR / ".garminconnect"
if not TOKEN_DIR.exists():
    try:
        home_token = Path.home() / ".garminconnect"
        # ทดสอบอ่านไฟล์เพื่อดูว่ามีสิทธิ์เข้าถึงหรือไม่
        test_file = home_token / "oauth1_token.json"
        if test_file.exists() and test_file.is_file():
            with open(test_file, "r") as f:
                f.read(1)
            TOKEN_DIR = home_token
    except Exception:
        pass


def get_authenticated_client():
    if not TOKEN_DIR.exists():
        print(json.dumps({
            "status": "error",
            "message": f"Token directory {TOKEN_DIR} not found. Please run 'python connect_garmin.py' to login and generate tokens first."
        }, ensure_ascii=False))
        sys.exit(1)

    garmin = Garmin()
    try:
        garmin.login(str(TOKEN_DIR))
        return garmin
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "message": f"Failed to login with cached token: {e}. Please re-authenticate via 'python connect_garmin.py'."
        }, ensure_ascii=False))
        sys.exit(1)


def get_today_summary(client):
    today = date.today().isoformat()
    result = {
        "date": today,
        "user": {
            "full_name": client.full_name,
            "display_name": client.display_name,
        },
        "daily_wellness": {},
        "training_readiness": {},
        "training_status": {},
        "vo2max": {},
        "race_predictions": {},
        "recent_running_overview": {}
    }

    # 1. Daily Wellness Summary
    try:
        summary = client.get_user_summary(today)
        result["daily_wellness"] = {
            "steps": summary.get("totalSteps", 0),
            "step_goal": summary.get("dailyStepGoal", 0),
            "distance_km": round((summary.get("totalDistanceMeters") or 0) / 1000, 2),
            "calories": summary.get("totalKilocalories", 0),
            "resting_hr": summary.get("restingHeartRate", None),
            "stress_level": summary.get("averageStressLevel", None),
        }
    except Exception:
        pass

    # 2. Sleep Data
    try:
        sleep = client.get_sleep_data(today)
        dto = sleep.get("dailySleepDTO", {})
        sleep_seconds = dto.get("sleepTimeSeconds", 0)
        result["daily_wellness"]["sleep"] = {
            "duration_hours": round(sleep_seconds / 3600, 1),
            "sleep_score": dto.get("sleepScores", {}).get("overall", {}).get("value", None),
            "sleep_quality": dto.get("sleepScores", {}).get("overall", {}).get("qualifierKey", None)
        }
    except Exception:
        pass

    # 3. Training Readiness & Status
    try:
        readiness = client.get_training_readiness(today)
        if readiness:
            result["training_readiness"] = {
                "score": readiness.get("score", None),
                "level": readiness.get("level", None)
            }
    except Exception:
        pass

    try:
        status = client.get_training_status(today)
        if status:
            result["training_status"] = status
    except Exception:
        pass

    # 4. VO2 Max
    try:
        metrics = client.get_max_metrics(today)
        if metrics:
            result["vo2max"] = metrics
    except Exception:
        pass

    # 5. Race Predictions
    try:
        predictions = client.get_race_predictions()
        if predictions:
            formatted_predictions = {}
            for item in predictions:
                name = item.get("raceType") or item.get("name")
                time_sec = item.get("time") or item.get("predictedTime")
                if time_sec:
                    m, s = divmod(int(time_sec), 60)
                    h, m = divmod(m, 60)
                    formatted_time = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                    formatted_predictions[name] = {
                        "seconds": time_sec,
                        "formatted": formatted_time
                    }
            result["race_predictions"] = formatted_predictions if formatted_predictions else predictions
    except Exception:
        pass

    # 6. Recent Running Activities (Past 4 Weeks)
    try:
        activities = client.get_activities(0, 30)
        running_acts = [
            act for act in activities
            if "run" in act.get("activityType", {}).get("typeKey", "").lower()
        ]

        total_distance = sum(act.get("distance", 0) for act in running_acts) / 1000
        total_time_sec = sum(act.get("duration", 0) for act in running_acts)
        runs_count = len(running_acts)

        recent_5_runs = []
        for act in running_acts[:5]:
            dist_km = (act.get("distance", 0)) / 1000
            dur_sec = act.get("duration", 0)
            avg_speed_mps = act.get("averageSpeed", 0)
            pace_str = "N/A"
            if avg_speed_mps > 0:
                pace_sec_per_km = 1000 / avg_speed_mps
                p_min, p_sec = divmod(int(pace_sec_per_km), 60)
                pace_str = f"{p_min}:{p_sec:02d}/km"

            recent_5_runs.append({
                "activity_id": act.get("activityId"),
                "name": act.get("activityName"),
                "start_time": act.get("startTimeLocal"),
                "distance_km": round(dist_km, 2),
                "duration_min": round(dur_sec / 60, 1),
                "pace": pace_str,
                "avg_hr": act.get("averageHR", "N/A")
            })

        result["recent_running_overview"] = {
            "total_runs": runs_count,
            "total_distance_km": round(total_distance, 2),
            "recent_runs": recent_5_runs
        }
    except Exception:
        pass

    print(json.dumps(result, ensure_ascii=False, indent=2))


def add_weight(client, weight_val):
    try:
        val = float(weight_val)
        res = client.add_weigh_in(weight=val, unitKey="kg")
        print(json.dumps({
            "status": "success",
            "message": f"Successfully logged weight {val} kg to Garmin Connect",
            "weight": val,
            "unit": "kg",
            "response": str(res)
        }, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "message": f"Failed to log weight: {e}"
        }, ensure_ascii=False))
        sys.exit(1)


def get_activities_list(client, count=10):
    try:
        activities = client.get_activities(0, count)
        clean_list = []
        for act in activities:
            dist_km = (act.get("distance") or 0) / 1000
            dur_sec = act.get("duration") or 0
            avg_speed = act.get("averageSpeed") or 0
            pace_str = "N/A"
            if avg_speed > 0:
                sec_per_km = 1000 / avg_speed
                p_min, p_sec = divmod(int(sec_per_km), 60)
                pace_str = f"{p_min}:{p_sec:02d}/km"

            clean_list.append({
                "id": act.get("activityId"),
                "name": act.get("activityName"),
                "type": act.get("activityType", {}).get("typeKey"),
                "date": act.get("startTimeLocal"),
                "distance_km": round(dist_km, 2),
                "duration_min": round(dur_sec / 60, 1),
                "pace": pace_str,
                "avg_hr": act.get("averageHR"),
                "calories": act.get("calories")
            })
        print(json.dumps({"activities": clean_list}, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "message": f"Failed to fetch activities: {e}"
        }, ensure_ascii=False))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Garmin Skill CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: summary
    subparsers.add_parser("summary", help="Get summary of user fitness, training readiness, and recent runs")

    # Subcommand: activities
    act_parser = subparsers.add_parser("activities", help="Get recent activities")
    act_parser.add_argument("--count", type=int, default=10, help="Number of activities to fetch")

    # Subcommand: weight
    weight_parser = subparsers.add_parser("weight", help="Log weight in kg")
    weight_parser.add_argument("value", type=float, help="Weight in kg (e.g. 68.5)")

    args = parser.parse_args()
    client = get_authenticated_client()

    if args.command == "summary":
        get_today_summary(client)
    elif args.command == "activities":
        get_activities_list(client, count=args.count)
    elif args.command == "weight":
        add_weight(client, args.value)


if __name__ == "__main__":
    main()
