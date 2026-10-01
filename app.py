#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, flash , Response
)
from werkzeug.security import check_password_hash, generate_password_hash
import mysql.connector
import math
from collections import defaultdict
from datetime import datetime, timedelta
import pytz
import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"
from secrets import token_urlsafe
import cv2
import time
import requests
import threading
from time import sleep
import urllib.parse  # for building alert link

# ----------------- Flask & DB 基本設定 -----------------

app = Flask(__name__)

db_config = {
    "host": "localhost",
    "user": "testuser",
    "password": "test123",
    "database": "ponddb_new"
}
# judge.txt
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
JUDGE_FILE = os.path.join(BASE_DIR, "judge.txt")
# 通知冷卻
last_alert_time = {}
ALERT_COOLDOWN = 60 * 30  # 30 分鐘

# 這台 Flask 的 base URL（給 Discord 連結用）
ALERT_BASE_URL = "http://192.168.1.116:5000"

# 登入 Session
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY') or ('dev-' + token_urlsafe(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False

# RTSP URL
RTSP_URLS = {
    'F1': '',
    'F2': '',
    'F3': '',
}
#Line環境變數
LINE_CHANNEL_ACCESS_TOKEN =  ""
LINE_TO_USER_ID =  ""

# =====================================================
# 共用工具：RTSP 串流
# =====================================================
latest_frames = {}        # {"F1": jpeg_bytes}
capture_threads = {}      # {"F1": thread}
capture_running = {}      # {"F1": True/False}

def rtsp_worker(cam_id, rtsp_url):
    while capture_running.get(cam_id, True):
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            print(f"[RTSP] {cam_id} 無法開啟，3 秒後重試...")
            time.sleep(3)
            continue

        while capture_running.get(cam_id, True):
            ok, frame = cap.read()
            if not ok:
                print(f"[RTSP] {cam_id} 讀取失敗，重新連接...")
                break

            ok2, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok2:
                latest_frames[cam_id] = buf.tobytes()

        cap.release()
        time.sleep(1)

def ensure_camera_thread(cam_id, rtsp_url):
    if cam_id in capture_threads and capture_threads[cam_id].is_alive():
        return
    capture_running[cam_id] = True
    t = threading.Thread(target=rtsp_worker, args=(cam_id, rtsp_url), daemon=True)
    capture_threads[cam_id] = t
    t.start()


def generate_frames(pond_id: str):
    """從 RTSP 讀取影像，轉成 MJPEG 串流"""
    rtsp_url = RTSP_URLS.get(pond_id)
    if not rtsp_url:
        print(f"[RTSP] 找不到 {pond_id} 的 RTSP URL")
        return

    while True:
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        print(f"[RTSP] 嘗試連接 {pond_id}: {rtsp_url}")

        if not cap.isOpened():
            print(f"[RTSP] 無法開啟 {pond_id} 串流，5 秒後重試...")
            cap.release()
            time.sleep(5)
            continue

        while True:
            success, frame = cap.read()
            if not success:
                print(f"[RTSP] {pond_id} 讀取失敗，重新連接...")
                cap.release()
                time.sleep(5)
                break

            ok, buffer = cv2.imencode('.jpg', frame)
            if not ok:
                continue

            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')


# =====================================================
# 登入 / 使用者工具
# =====================================================

def get_user_by_username(username: str):
    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE username=%s LIMIT 1", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def login_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            next_url = request.url
            return redirect(url_for("login", next=next_url))
        return view_func(*args, **kwargs)

    return wrapper


@app.context_processor
def inject_user():
    return {"current_user": session.get("user")}


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("realtime_status"))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        next_url = request.args.get("next") or url_for("realtime_status")

        user = get_user_by_username(username)
        if not user:
            error = "帳號或密碼錯誤"
        else:
            if check_password_hash(user["password_hash"], password):
                session["user"] = {
                    "id": user["id"],
                    "username": user["username"],
                    "role": user["role"],
                }
                return redirect(next_url)
            else:
                error = "帳號或密碼錯誤"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# =====================================================
# 門檻 & 異常判斷工具（含 ORP）
# =====================================================

def _threshold_defaults():
    # 若 thresholds 沒資料時的預設
    return {
        "temp_min": 18.0, "temp_max": 24.0,
        "salinity_min": 0.0, "salinity_max": 10.0,
        "ph_min": 5.5, "ph_max": 8.5,
        "do_min": 5.0,
        "orp_min": 150.0, "orp_max": 250.0,
    }


def get_thresholds(conn=None):
    """從 thresholds 表讀設定，沒有就用預設值並寫入一筆 id=1。"""
    should_close = False
    if conn is None:
        conn = mysql.connector.connect(**db_config)
        should_close = True

    defaults = _threshold_defaults()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM thresholds WHERE id=1")
        row = cur.fetchone()
        if not row:
            cols = ", ".join(defaults.keys())
            placeholders = ", ".join([f"%({k})s" for k in defaults])
            cur.execute(
                f"INSERT INTO thresholds (id, {cols}) VALUES (1, {placeholders})",
                defaults,
            )
            conn.commit()
            return defaults

        return {k: float(row.get(k, v)) for k, v in defaults.items()}
    finally:
        if should_close:
            conn.close()


def _load_thresholds_safe():
    """給 history / dashboard 等用的安全版（出錯就用預設值）"""
    base = _threshold_defaults()
    try:
        th = get_thresholds()
        if not th:
            return base
        out = base.copy()
        for k in out.keys():
            if k in th:
                try:
                    out[k] = float(th[k])
                except Exception:
                    pass
        return out
    except Exception:
        return base


def save_thresholds(th: dict):
    """寫 thresholds，並做硬限制 & 上下限排序"""
    HARD = {
        "temp": (-10, 50),
        "salinity": (0, 20),
        "ph": (0, 14),
        "do": (0, 40),
        "orp": (0, 500),
    }

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    th["temp_min"] = clamp(float(th["temp_min"]), *HARD["temp"])
    th["temp_max"] = clamp(float(th["temp_max"]), *HARD["temp"])
    th["salinity_min"] = clamp(float(th["salinity_min"]), *HARD["salinity"])
    th["salinity_max"] = clamp(float(th["salinity_max"]), *HARD["salinity"])
    th["ph_min"] = clamp(float(th["ph_min"]), *HARD["ph"])
    th["ph_max"] = clamp(float(th["ph_max"]), *HARD["ph"])
    th["do_min"] = clamp(float(th["do_min"]), *HARD["do"])
    th["orp_min"] = clamp(float(th["orp_min"]), *HARD["orp"])
    th["orp_max"] = clamp(float(th["orp_max"]), *HARD["orp"])

    if th["temp_max"] < th["temp_min"]:
        th["temp_min"], th["temp_max"] = th["temp_max"], th["temp_min"]
    if th["salinity_max"] < th["salinity_min"]:
        th["salinity_min"], th["salinity_max"] = th["salinity_max"], th["salinity_min"]
    if th["ph_max"] < th["ph_min"]:
        th["ph_min"], th["ph_max"] = th["ph_max"], th["ph_min"]
    if th["orp_max"] < th["orp_min"]:
        th["orp_min"], th["orp_max"] = th["orp_max"], th["orp_min"]

    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()
    set_clause = ", ".join([f"{k}=%({k})s" for k in th.keys()])
    cur.execute(f"UPDATE thresholds SET {set_clause} WHERE id=1", th)
    conn.commit()
    cur.close()
    conn.close()


def is_abnormal(temp, salinity, ph, do_level, orp, th=None):
    """判斷一筆資料是否異常"""
    if temp is None or salinity is None or ph is None or do_level is None or orp is None:
        return False
    if th is None:
        th = get_thresholds()
    return (
        temp < th["temp_min"] or temp > th["temp_max"] or
        salinity < th["salinity_min"] or salinity > th["salinity_max"] or
        ph < th["ph_min"] or ph > th["ph_max"] or
        do_level < th["do_min"] or
        orp < th["orp_min"] or orp > th["orp_max"]
    )


def _build_tags(temp, sal, ph, do_level, orp, th):
    """把一筆資料轉成 tag 標籤陣列（給前端顯示用）"""
    tags = []

    if temp is not None:
        if temp < th["temp_min"]:
            tags.append(("溫 低", "tag-warn"))
        elif temp > th["temp_max"]:
            tags.append(("溫 高", "tag-warn"))

    if sal is not None:
        if sal < th["salinity_min"] or sal > th["salinity_max"]:
            tags.append(("鹽", "tag-warn"))

    if ph is not None:
        if ph < th["ph_min"] or ph > th["ph_max"]:
            tags.append(("pH", "tag-warn"))

    if do_level is not None:
        if do_level < th["do_min"]:
            tags.append(("溶氧 低", "tag-warn"))

    if orp is not None:
        if orp < th["orp_min"] or orp > th["orp_max"]:
            tags.append(("ORP", "tag-warn"))

    if not tags:
        tags = [("正常", "tag-ok")]

    return tags


# =====================================================
# 儀表板 / 異常總覽 / 預測 / 即時狀態
# =====================================================

@app.route("/pond_cycle")
@login_required
def dashboard():
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)

    def _parse_dt(s):
        if not s:
            return None
        s = s.strip().replace('T', ' ')
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return tz.localize(datetime.strptime(s, fmt))
            except Exception:
                pass
        return None

    start_q = _parse_dt(request.args.get("start"))
    end_q = _parse_dt(request.args.get("end"))

    if not start_q or not end_q or end_q <= start_q:
        end_time, start_time = now, now - timedelta(hours=24)
    else:
        start_time, end_time = start_q, end_q

    # ✅ 起點對齊到整分鐘（秒=0, 微秒=0）
    start_time = start_time.replace(second=0, microsecond=0)

    # （可選）終點也對齊到整分鐘：避免邊界造成 +1/-1 筆
    # end_time = end_time.replace(second=0, microsecond=0)

    # ✅ DB 存的是 naive（台北時間），查詢也要用 naive
    start_db = start_time.replace(tzinfo=None)
    end_db   = end_time.replace(tzinfo=None)

    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()

    pond_q = (request.args.get("pond_id") or "").strip()
    if pond_q:
        ponds = [pond_q]
    else:
        cur.execute("SELECT DISTINCT pond_id FROM pond_data ORDER BY pond_id")
        ponds = [r[0] for r in cur.fetchall()]
    # 目前 ponds 可能是 [pond_q] 或所有 ponds
    cur.execute("SELECT DISTINCT pond_id FROM pond_data ORDER BY pond_id")
    ponds_all = [r[0] for r in cur.fetchall()]
    selected_pond = pond_q if pond_q else (ponds_all[0] if ponds_all else "")

    chart_by_pond = {}

    def downsample_to_24(labels, *series, max_points=24):
        n = len(labels)
        if n <= max_points:
            return (labels, *series)
        step = int(math.ceil(n / max_points))
        idxs = list(range(0, n, step))
        if idxs[-1] != n - 1:
            idxs.append(n - 1)
        while len(idxs) > max_points:
            del idxs[-2]
        pick = lambda arr: [arr[i] for i in idxs]
        return (pick(labels), *(pick(s) for s in series))

    # ✅ 先算每池狀態（最後更新時間）
    cur2 = conn.cursor()
    pond_status = {}
    now_naive = now.replace(tzinfo=None)  # 台北時間但 naive

    for pond in ponds:
        cur2.execute("SELECT MAX(timestamp) FROM pond_data WHERE pond_id=%s", (pond,))
        last_ts = cur2.fetchone()[0]

        if last_ts is None:
            pond_status[pond] = {"state": "no_data", "last_ts": None, "minutes_ago": None}
            continue

        minutes_ago = int((now_naive - last_ts).total_seconds() // 60)

        if minutes_ago <= 3:
            state = "ok"
        elif minutes_ago <= 30:
            state = "stale"
        else:
            state = "offline"

        pond_status[pond] = {
            "state": state,
            "last_ts": last_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "minutes_ago": minutes_ago
        }

    cur2.close()

    # ✅ 再拉圖表資料
    for pond in ponds:
        cur.execute("""
            SELECT timestamp, temperature, salinity, ph, do_level, orp
            FROM pond_data
            WHERE pond_id=%s AND timestamp BETWEEN %s AND %s
            ORDER BY timestamp ASC
        """, (pond, start_db, end_db))
        rows = cur.fetchall()
        # ✅ 若區間內完全沒資料：改抓最接近該區間的一筆
        fallback_used = False
        fallback_note = None

        if not rows:
            # 1) 區間前一筆（<= start_db 的最後一筆）
            cur.execute("""
                SELECT timestamp, temperature, salinity, ph, do_level, orp
                FROM pond_data
                WHERE pond_id=%s AND timestamp <= %s
                ORDER BY timestamp DESC
                LIMIT 1
            """, (pond, start_db))
            prev_row = cur.fetchone()

            # 2) 區間後一筆（>= end_db 的第一筆）
            cur.execute("""
                SELECT timestamp, temperature, salinity, ph, do_level, orp
                FROM pond_data
                WHERE pond_id=%s AND timestamp >= %s
                ORDER BY timestamp ASC
                LIMIT 1
            """, (pond, end_db))
            next_row = cur.fetchone()

            # 3) 選比較靠近「區間」的那一筆
            chosen = None
            if prev_row and next_row:
                prev_ts = prev_row[0]
                next_ts = next_row[0]
                d_prev = (start_db - prev_ts).total_seconds()
                d_next = (next_ts - end_db).total_seconds()
                chosen = prev_row if d_prev <= d_next else next_row
            else:
                chosen = prev_row or next_row

            if chosen:
                rows = [chosen]
                fallback_used = True
                fallback_note = f"區間內無資料，改用最接近區間的資料點：{chosen[0].strftime('%Y-%m-%d %H:%M:%S')}"
            else:
                rows = []  # DB 真的完全沒資料

        labels, temps, sals, phs, dos, orps = [], [], [], [], [], []
        for ts, t, s, p, d, o in rows:
            # ✅ ts 若是 naive，就 localize；若已帶 tz，再 astimezone
            if getattr(ts, "tzinfo", None) is None:
                local_ts = tz.localize(ts)
            else:
                local_ts = ts.astimezone(tz)

            labels.append(local_ts.strftime("%m-%d %H:%M"))
            temps.append(t); sals.append(s); phs.append(p); dos.append(d); orps.append(o)

        labels, temps, sals, phs, dos, orps = downsample_to_24(
            labels, temps, sals, phs, dos, orps, max_points=24
        )

        chart_by_pond[pond] = {
            "timestamp": labels,
            "temperature": temps,
            "salinity": sals,
            "ph": phs,
            "do_level": dos,
            "orp": orps,
            "fallback_used": fallback_used,
            "fallback_note": fallback_note,
        }

    cur.close()
    conn.close()

    # ✅ pond_status 要當成獨立變數傳給模板（不是塞進 range_info）
    return render_template(
        "dashboard.html",
        chart_data=chart_by_pond,
        pond_status=pond_status,
        ponds_all=ponds_all,
        selected_pond=selected_pond,
        range_info={
            "start": start_time.strftime("%Y-%m-%d %H:%M"),
            "end": end_time.strftime("%Y-%m-%d %H:%M"),
        },
    )



@app.route("/abnormal_summary")
@login_required
def abnormal_summary():
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    tz = pytz.timezone('Asia/Taipei')
    th = get_thresholds()

    cursor.execute("""
        SELECT pond_id, timestamp, temperature, salinity, ph, do_level, orp
        FROM pond_data
        WHERE timestamp >= NOW() - INTERVAL 1 DAY
        ORDER BY pond_id, timestamp
    """)
    rows = cursor.fetchall()

    summary_data = defaultdict(lambda: {
        "timestamps": [],
        "abnormal_counts": defaultdict(int),
    })

    for pond_id, ts, temp, salinity, ph, do_level, orp in rows:
        local_ts = ts.astimezone(tz)
        hour_key = local_ts.replace(minute=0, second=0, microsecond=0)
        hour_str = hour_key.strftime("%Y-%m-%dT%H:%M:%S")

        if is_abnormal(temp, salinity, ph, do_level, orp, th):
            summary_data[pond_id]["abnormal_counts"][hour_str] += 1
        else:
            summary_data[pond_id]["abnormal_counts"].setdefault(hour_str, 0)

    for pond_id in summary_data:
        counts_dict = summary_data[pond_id]["abnormal_counts"]
        sorted_hours = sorted(counts_dict.keys())
        summary_data[pond_id]["timestamps"] = sorted_hours
        summary_data[pond_id]["abnormal_counts"] = [
            counts_dict[ts] for ts in sorted_hours
        ]
        summary_data[pond_id]["all_zero"] = all(
            v == 0 for v in summary_data[pond_id]["abnormal_counts"]
        )

    cursor.close()
    conn.close()

    return render_template("abnormal_summary.html", summary_data=summary_data)


@app.route('/video_feed/<pond_id>')
@login_required
def video_feed(pond_id):
    rtsp_url = RTSP_URLS.get(pond_id)
    if not rtsp_url:
        return Response("bad pond_id", status=404)

    # ✅ 確保同一台攝影機只會啟動一條背景 RTSP 連線
    ensure_camera_thread(pond_id, rtsp_url)

    def gen():
        # 只負責把「最新影像」吐給前端，不再自己連 RTSP
        while True:
            frame = latest_frames.get(pond_id)
            if frame is None:
                time.sleep(0.1)
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

            # 控制輸出頻率（避免佔用太高 CPU）
            time.sleep(0.05)  # 約 20 FPS，可自行調

    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')



def build_prediction_chart_data(pond_id: str,
                                real_window_minutes: int = 120,
                                pred_step_minutes: int = 10,
                                pred_points: int = 6,
                                tz_name: str = "Asia/Taipei"):
    """
    回傳 prediction 圖表需要的資料結構（labels / real / pred），並給出 futureStartIndex 供前端做預測區著色。
    - real：過去 real_window_minutes
    - pred：未來 pred_step_minutes * pred_points
    """
    tz = pytz.timezone(tz_name)

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()

    # 1) 真實資料：過去 N 分鐘
    cursor.execute(f"""
        SELECT timestamp, temperature, salinity, ph, do_level, orp
        FROM pond_data
        WHERE pond_id = %s
          AND timestamp >= NOW() - INTERVAL {real_window_minutes} MINUTE
        ORDER BY timestamp ASC
    """, (pond_id,))
    real_rows = list(cursor.fetchall())

    # 2) 預測資料：只抓未來（避免抓到過去的 prediction）
    cursor.execute(f"""
        SELECT timestamp, temperature, salinity, ph, do_level, orp
        FROM prediction_results
        WHERE pond_id = %s
          AND timestamp > NOW()
        ORDER BY timestamp ASC
        LIMIT {pred_points}
    """, (pond_id,))
    pred_rows = list(cursor.fetchall())

    cursor.close()
    conn.close()

    def to_map(rows):
        labels = []
        maps = {"temperature": {}, "ph": {}, "do_level": {}, "orp": {}}
        for ts, temp, sal, ph, do_level, orp in rows:
            if getattr(ts, "tzinfo", None) is None:
                local = tz.localize(ts)
            else:
                local = ts.astimezone(tz)

            label = local.strftime("%m-%d %H:%M")
            labels.append(label)

            maps["temperature"][label] = float(temp) if temp is not None else None
            maps["ph"][label] = float(ph) if ph is not None else None
            maps["do_level"][label] = float(do_level) if do_level is not None else None
            maps["orp"][label] = float(orp) if orp is not None else None

        return labels, maps

    def downsample_rows(rows, target_len):
        if target_len <= 0 or len(rows) <= target_len:
            return rows
        n = len(rows)
        indices = [int(i * (n - 1) / (target_len - 1)) for i in range(target_len)]
        return [rows[i] for i in indices]

    # 控制「實際 : 預測 ≈ 2 : 1」（避免實際點數太多把預測擠成一小段）
    real_count = len(real_rows)
    pred_count = len(pred_rows)
    if real_count > 0 and pred_count > 0:
        target_real_count = min(real_count, 2 * pred_count)
        real_rows = downsample_rows(real_rows, target_real_count)

    real_labels, real_map = to_map(real_rows)
    pred_labels, pred_map = to_map(pred_rows)

    # 沒真實資料：直接用預測 labels
    if not real_labels:
        all_labels = pred_labels
        future_start_index = 0 if pred_labels else -1
        pred_labels_fixed = pred_labels
    else:
        # 用「真實最後一筆」當基準，往後推 pred_step_minutes
        last_real_label = real_labels[-1]
        t0 = datetime.strptime(last_real_label, "%m-%d %H:%M")
        pred_labels_fixed = [(t0 + timedelta(minutes=pred_step_minutes * i)).strftime("%m-%d %H:%M")
                             for i in range(1, len(pred_rows) + 1)]
        all_labels = real_labels + pred_labels_fixed
        future_start_index = len(real_labels) if pred_rows else -1

    # real 對齊 all_labels（未來補 None）
    real_series = {}
    for key in real_map:
        real_series[key] = [real_map[key].get(lbl) for lbl in all_labels]

    # pred 對齊 all_labels（過去補 None，只在未來填值）
    pred_series = {}
    for key in pred_map:
        past_none_list = [None] * (len(real_labels) if real_labels else 0)

        future_vals = []
        for row in pred_rows:
            val = None
            if key == "temperature": val = row[1]
            elif key == "ph": val = row[3]
            elif key == "do_level": val = row[4]
            elif key == "orp": val = row[5]
            future_vals.append(float(val) if val is not None else None)

        pred_series[key] = past_none_list + future_vals if real_labels else future_vals

    chart_data = {"labels": all_labels, "real": real_series, "pred": pred_series}
    return chart_data, future_start_index


# ========= 預測 =========
@app.route("/data_prediction")
def data_prediction():
    pond_id = request.args.get("pond_id", "Pond A")

    # 捕捉魚池列表
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT pond_id FROM pond_data ORDER BY pond_id")
    pond_ids = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()

    chart_data, future_start_index = build_prediction_chart_data(pond_id)

    return render_template(
        "data_prediction.html",
        chart_data=chart_data,
        pond_ids=pond_ids,
        current_pond=pond_id,
        future_start_index=future_start_index,)
    
@app.route("/api/prediction")
@login_required
def api_prediction():
    pond_id = request.args.get("pond_id", "Pond A")
    try:
        chart_data, future_start_index = build_prediction_chart_data(pond_id)
        return jsonify({
            "chart_data": chart_data,
            "futureStartIndex": future_start_index
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




# ======即時======
@app.route("/realtime_status")
def realtime_status():
    """
    即時狀態頁：左邊 RTSP / 右邊 IoTtalk 即時
    這裡只提供初始數值（之後由前端 IoTtalk 覆蓋）
    """
    default_pond = "Pond A"

    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()
    cur.execute("""
        SELECT timestamp, temperature, salinity, ph, do_level, orp
        FROM pond_data
        WHERE pond_id = %s
        ORDER BY timestamp DESC
        LIMIT 1
    """, (default_pond,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row:
        ts, temp, sal, ph, do_level, orp = row
        latest_data = {
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "temperature": temp,
            "salinity": sal,
            "ph": ph,
            "do_level": do_level,
            "orp": orp,
        }
    else:
        latest_data = {
            "timestamp": "-",
            "temperature": "-",
            "salinity": "-",
            "ph": "-",
            "do_level": "-",
            "orp": "-",
        }
    th = get_thresholds()
    return render_template("realtime_status.html", latest_data=latest_data,thresholds=th,)

@app.route("/api/behavior_status")
def api_behavior_status():
    """
    回傳某魚池最近是否有行為異常：
    - status: "normal" / "abnormal"
    - last_event_time: 最後一筆事件時間（字串）
    """
    pond_id = request.args.get("pond_id", "Pond A")

    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()
    cur.execute("""
        SELECT event_time
        FROM behavior_events
        WHERE pond_id = %s
        ORDER BY event_time DESC
        LIMIT 1
    """, (pond_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({
            "pond_id": pond_id,
            "status": "normal",
            "has_event": False,
            "last_event_time": None,
        })

    last_time = row[0]
    # 定義「最近」的範圍，這裡先抓 10 分鐘內算是異常狀態
    now = datetime.now(last_time.tzinfo) if getattr(last_time, "tzinfo", None) else datetime.now()
    is_recent = (now - last_time) <= timedelta(seconds=5)

    return jsonify({
        "pond_id": pond_id,
        "status": "abnormal" if is_recent else "normal",
        "has_event": is_recent,
        "last_event_time": last_time.strftime("%Y-%m-%d %H:%M:%S"),
    })
@app.route("/api/leftover_status")
def api_leftover_status():
    """
    回傳某魚池最近是否有殘料/飼料異常：
    - status: "normal" / "abnormal"
    - last_event_time: 最後一筆事件時間（字串）
    """
    pond_id = request.args.get("pond_id", "Pond A")

    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()
    cur.execute("""
        SELECT event_time
        FROM leftover_events
        WHERE pond_id = %s
        ORDER BY event_time DESC
        LIMIT 1
    """, (pond_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return jsonify({
            "pond_id": pond_id,
            "status": "normal",
            "has_event": False,
            "last_event_time": None,
        })

    last_time = row[0]
    now = datetime.now(last_time.tzinfo) if getattr(last_time, "tzinfo", None) else datetime.now()
    is_recent = (now - last_time) <= timedelta(seconds=5)

    return jsonify({
        "pond_id": pond_id,
        "status": "abnormal" if is_recent else "normal",
        "has_event": is_recent,
        "last_event_time": last_time.strftime("%Y-%m-%d %H:%M:%S"),
    })

# =====================================================
# 頁數變紅
# =====================================================
def _pages_with_unhandled(status_list, page_size: int):
    """
    status_list: 依照『畫面排序』(DESC) 的整份清單，每筆是一個狀態字串
    回傳：set({頁碼, ...})
    """
    pages = set()
    for idx, st in enumerate(status_list):
        st = (st or "未處理")
        if st == "未處理":
            pages.add(idx // page_size + 1)
    return pages

# =====================================================
# 歷史查詢 / 數值查看
# =====================================================

@app.route('/history_result', methods=['GET', 'POST'])
@login_required
def history_result():
    # ---------- 1. POST：表單送出 ----------
    if request.method == 'POST':
        pond_id = (request.values.get('pond_id') or '').strip()
        start   = (request.values.get('start') or '').strip()
        end     = (request.values.get('end') or '').strip()
        abnormal_type = (request.values.get('abnormal_type') or 'all').strip()

        if not (start and end):
            date_str   = request.values.get('date')
            start_hour = int(request.values.get('start_hour', 0))
            end_hour   = int(request.values.get('end_hour', 23))
            if not date_str:
                return redirect(url_for('dashboard'))
            start = f"{date_str} {start_hour:02d}:00:00"
            end   = f"{date_str} {end_hour:02d}:59:59"

        page_size = int(request.values.get('page_size', 10))

        return redirect(url_for(
            'history_result',
            pond_id=pond_id or None,
            start=start,
            end=end,
            page=1,
            page_size=page_size,
            abnormal_type=abnormal_type,
        ))

    # ---------- 2. GET 參數 ----------
    pond_id   = (request.args.get('pond_id') or '').strip()
    start_str = request.args.get('start')
    end_str   = request.args.get('end')
    abnormal_type = (request.args.get('abnormal_type') or 'all').strip()
    from_alert    = request.args.get('from_alert')

    # 從 Discord 點連結，只帶 pond_id、from_alert=1 → 給預設時間區間 & 預設 data
    if not (start_str and end_str):
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.now(tz)
        start_dt = now - timedelta(hours=1)
        end_dt   = now
        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str   = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    if from_alert == '1' and not request.args.get('abnormal_type'):
        abnormal_type = 'data'

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1

    # ✅ 讀 page_size（只允許 3 或 10）
    try:
        page_size = int(request.args.get('page_size', 10))
    except (ValueError, TypeError):
        page_size = 10

    if page_size not in (5, 10):
        page_size = 10

    offset = (page - 1) * page_size

    # =====================================================
    # A) 行為異常：behavior_events
    # =====================================================
    if abnormal_type == 'behavior':
        conn = mysql.connector.connect(**db_config)
        # ⭐ 用 dictionary=True 方便用欄位名稱取值
        cur  = conn.cursor(dictionary=True)

        params = [start_str, end_str]
        where  = "WHERE event_time BETWEEN %s AND %s"
        if pond_id:
            where += " AND pond_id = %s"
            params.append(pond_id)

        # 先算總筆數
        cur.execute(f"SELECT COUNT(*) AS cnt FROM behavior_events {where}", params)
        total_count = int(cur.fetchone()["cnt"])
        total_pages = max(1, math.ceil(total_count / page_size))

        if page > total_pages:
            page   = total_pages
            offset = (page - 1) * page_size

        params_data = params + [page_size, offset]

        # ⭐ 把 id, status 也撈出來
        cur.execute(
            f"""
            SELECT id, pond_id, event_time, event_type, status, handler, handle_start_at, handle_end_at
            FROM behavior_events
            {where}
            ORDER BY event_time DESC
            LIMIT %s OFFSET %s
            """,
            params_data
        )
        rows = cur.fetchall()

        # ⭐ 把 id, status 也撈出來（當頁）
        cur.execute(f"""
            SELECT id, pond_id, event_time, event_type, status, handler, handle_start_at, handle_end_at
            FROM behavior_events
            {where}
            ORDER BY event_time DESC
            LIMIT %s OFFSET %s
        """, params_data)
        rows = cur.fetchall()

        # === 新增：計算哪些頁碼有未處理 ===
        cur.execute(f"SELECT status FROM behavior_events {where} ORDER BY event_time DESC", params)
        all_status_rows = cur.fetchall()
        # dictionary=True，所以每列長這樣：{"status": "..."}
        status_list = [(r.get("status") or "未處理") for r in all_status_rows]
        pages_with_unhandled = _pages_with_unhandled(status_list, page_size)
        cur.close()
        conn.close()

        processed = []
        for row in rows:
            ts = row["event_time"]
            processed.append({
                "id":    row["id"],                          # ⭐ 給前端用 data-id
                "ts":    ts.strftime("%Y-%m-%d %H:%M:%S"),
                "pond":  row["pond_id"],
                "tags":  [("行為異常", "tag-warn")],
                "video": "",                                 # demo 用前端自行對應
                "notified": "已通知",
                "status": row.get("status") or "未處理",      # DB 沒值就當未處理
                "kind":  "behavior",                         # ⭐ 告訴前端這是行為異常
                "handler": row.get("handler"),
                "handle_start_at": row.get("handle_start_at").strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_start_at") else None,
                "handle_end_at": row.get("handle_end_at").strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_end_at") else None,
            })

        return render_template(
            "history_result.html",
            pond_id=pond_id or "（全部魚池）",
            start=start_str,
            end=end_str,
            rows2=processed,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            total_count=total_count,
            abnormal_type=abnormal_type,
            pages_with_unhandled=pages_with_unhandled,
            highlight_ts=request.args.get("highlight_ts"),
        )
    # =====================================================
    # B) 之後如果要做殘料異常 leftover_events，可以比照 A 再開一個分支
    # =====================================================
    if abnormal_type == 'leftover':
        conn = mysql.connector.connect(**db_config)
        cur  = conn.cursor(dictionary=True)

        params = [start_str, end_str]
        where  = "WHERE event_time BETWEEN %s AND %s"
        if pond_id:
            where += " AND pond_id = %s"
            params.append(pond_id)

        # 先算總筆數
        cur.execute(f"SELECT COUNT(*) AS cnt FROM leftover_events {where}", params)
        total_count = int(cur.fetchone()["cnt"])
        total_pages = max(1, math.ceil(total_count / page_size))

        if page > total_pages:
            page = total_pages
        offset = (page - 1) * page_size

        # 撈當頁資料
        cur.execute(
            f"""
            SELECT id, pond_id, event_time, event_type, status, handler, video_path, handle_start_at, handle_end_at
            FROM leftover_events
            {where}
            ORDER BY event_time DESC
            LIMIT %s OFFSET %s
            """,
            params + [page_size, offset]
        )
        rows = cur.fetchall()

        # 計算哪些頁碼有未處理
        cur.execute(f"SELECT status FROM leftover_events {where} ORDER BY event_time DESC", params)
        all_status_rows = cur.fetchall()
        status_list = [(r.get("status") or "未處理") for r in all_status_rows]
        pages_with_unhandled = _pages_with_unhandled(status_list, page_size)

        cur.close()
        conn.close()

        processed = []
        for row in rows:
            ts = row["event_time"]
            processed.append({
                "id": row["id"],
                "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "pond": row["pond_id"],
                "tags": [("殘料異常", "tag-warn")],
                "video": row.get("video_path") or "",   # 你要播影片再用得到
                "notified": "已通知",                   # 若你有 notified 欄位也可改成讀 DB
                "status": row.get("status") or "未處理",
                "kind": "leftover",                     # ✅ 給 history_result.html 的 data-kind 用
                "handler": row.get("handler"),
                "handle_start_at": row["handle_start_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_start_at") else None,
                "handle_end_at":   row["handle_end_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_end_at") else None,
            })

        return render_template(
            "history_result.html",
            pond_id=pond_id or "（全部魚池）",
            start=start_str,
            end=end_str,
            rows2=processed,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            total_count=total_count,
            abnormal_type=abnormal_type,
            pages_with_unhandled=pages_with_unhandled,
            highlight_ts=request.args.get("highlight_ts"),
        )
    # =====================================================
    # C) 全部顯示：把「水質 alerts」+「behavior_events (+ leftover)」合起來
    # =====================================================
    if abnormal_type == 'all':
        combined = []

# ---------- C1. 水質異常（alerts 事件列表 + pond_data） ----------
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        params_alert = [start_str, end_str]
        where_alert  = "WHERE a.timestamp BETWEEN %s AND %s"
        if pond_id:
            where_alert += " AND a.pond_id = %s"
            params_alert.append(pond_id)

        alert_sql = f"""
            SELECT
                MIN(a.id) AS id,
                a.pond_id,
                a.timestamp,
                GROUP_CONCAT(DISTINCT a.parameter ORDER BY a.parameter SEPARATOR ',') AS parameters,
                MAX(a.notified) AS notified,
                MAX(a.status)   AS status,
                MAX(a.handler)  AS handler,
                MIN(a.handle_start_at) AS handle_start_at,
                MAX(a.handle_end_at)   AS handle_end_at,
                p.temperature, p.salinity, p.ph, p.do_level, p.orp
            FROM alerts a
            LEFT JOIN pond_data p
            ON p.pond_id = a.pond_id AND p.timestamp = a.timestamp
            {where_alert}
            GROUP BY a.pond_id, a.timestamp
            ORDER BY a.timestamp DESC
        """
        cursor.execute(alert_sql, params_alert)
        alert_rows = cursor.fetchall()
        cursor.close()
        conn.close()

        th = _load_thresholds_safe()

        for r in (alert_rows or []):
            ts_a = r["timestamp"]

            # tags：用同一時間點的 pond_data 值去算
            tags = _build_tags(
                r.get("temperature"),
                r.get("salinity"),
                r.get("ph"),
                r.get("do_level"),
                r.get("orp"),
                th
            )

            n = r.get("notified")
            if n == 1:
                notified_txt = "已通知"
            elif n == 0:
                notified_txt = "未通知"
            else:
                notified_txt = "—"

            combined.append({
                "id": r["id"],                # ✅ 關鍵：每筆都有 alerts.id
                "event_type": "data",
                "kind": "data",               # ✅ 關鍵：前端會用 kind 判斷
                "ts": ts_a.strftime("%Y-%m-%d %H:%M:%S"),
                "ts_dt": ts_a,
                "pond": r["pond_id"],
                "tags": tags,
                "video": "",
                "notified": notified_txt,
                "status": r.get("status") or "未處理",   # ✅ 直接顯示 alerts.status
                "handler": r.get("handler"),            # ✅ 直接顯示 alerts.handler
                "handle_start_at": r["handle_start_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("handle_start_at") else None,
                "handle_end_at":   r["handle_end_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("handle_end_at") else None,
            })

        # ---------- C2. 行為異常（behavior_events） ----------
        conn_b = mysql.connector.connect(**db_config)
        cur_b  = conn_b.cursor(dictionary=True)

        params_beh = [start_str, end_str]
        where_beh  = "WHERE event_time BETWEEN %s AND %s"
        if pond_id:
            where_beh += " AND pond_id = %s"
            params_beh.append(pond_id)

        cur_b.execute(
            f"""
            SELECT id, pond_id, event_time, event_type, status, handler, video_path, handle_start_at, handle_end_at
            FROM behavior_events
            {where_beh}
            ORDER BY event_time DESC
            """,
            params_beh
        )
        beh_rows = cur_b.fetchall()
        cur_b.close()
        conn_b.close()

        for row in beh_rows:
            ts = row["event_time"]
            combined.append({
                "id":   row["id"],
                "event_type": "behavior",
                "kind": "behavior",
                "ts":   ts.strftime("%Y-%m-%d %H:%M:%S"),
                "ts_dt": ts,
                "pond": row["pond_id"],
                "tags": [("行為異常", "tag-warn")],
                "video": row.get("video_path") or "/static/sample_video.mp4",
                "notified": "已通知",
                "status": row.get("status") or "未處理",
                "handler": row.get("handler"),
                "handle_start_at": row["handle_start_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_start_at") else None,
                "handle_end_at":   row["handle_end_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_end_at") else None,
            })

        # ---------- C2-2. 殘料異常（leftover_events） ----------
        conn_l = mysql.connector.connect(**db_config)
        cur_l  = conn_l.cursor(dictionary=True)

        params_lo = [start_str, end_str]
        where_lo  = "WHERE event_time BETWEEN %s AND %s"
        if pond_id:
            where_lo += " AND pond_id = %s"
            params_lo.append(pond_id)

        cur_l.execute(
            f"""
            SELECT id, pond_id, event_time, event_type, status, handler, video_path, handle_start_at, handle_end_at
            FROM leftover_events
            {where_lo}
            ORDER BY event_time DESC
            """,
            params_lo
        )
        lo_rows = cur_l.fetchall()
        cur_l.close()
        conn_l.close()

        for row in lo_rows:
            ts = row["event_time"]
            combined.append({
                "id": row["id"],
                "event_type": "leftover",
                "kind": "leftover",
                "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "ts_dt": ts,
                "pond": row["pond_id"],
                "tags": [("殘料異常", "tag-warn")],
                "video": row.get("video_path") or "",
                "notified": "已通知",
                "status": row.get("status") or "未處理",
                "handler": row.get("handler"),
                "handle_start_at": row["handle_start_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_start_at") else None,
                "handle_end_at":   row["handle_end_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("handle_end_at") else None,
            })
        # ---------- C3. 合併後排序 + 分頁 ----------
        # 有些水質異常沒有 ts_dt，就先補回 datetime 版本（理論上都會有）
        for r in combined:
            if "ts_dt" not in r or r["ts_dt"] is None:
                try:
                    r["ts_dt"] = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    r["ts_dt"] = datetime.min

        combined.sort(key=lambda x: x["ts_dt"], reverse=True)

        total_count = len(combined)
        total_pages = max(1, math.ceil(total_count / page_size))
        if page > total_pages:
            page = total_pages

        start_idx = (page - 1) * page_size
        end_idx   = start_idx + page_size
        page_rows = combined[start_idx:end_idx]

        # 不需要再把 ts_dt 傳給前端
        for r in page_rows:
            r.pop("ts_dt", None)

        status_list = [(r.get("status") or "未處理") for r in combined]
        pages_with_unhandled = _pages_with_unhandled(status_list, page_size)

        return render_template(
            "history_result.html",
            pond_id=pond_id or "（全部魚池）",
            start=start_str,
            end=end_str,
            rows2=page_rows,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            total_count=total_count,
            abnormal_type=abnormal_type,
            pages_with_unhandled=pages_with_unhandled,
            highlight_ts=request.args.get("highlight_ts"),
        )

    # =====================================================
    # D) 只看「數據異常」（alerts 事件列表）
    # =====================================================
    conn = mysql.connector.connect(**db_config)
    cur  = conn.cursor(dictionary=True)

    params = [start_str, end_str]
    where  = "WHERE a.timestamp BETWEEN %s AND %s"
    if pond_id:
        where += " AND a.pond_id = %s"
        params.append(pond_id)

    # 1) count（用 alerts 筆數當總數）
    cur.execute(f"SELECT COUNT(DISTINCT a.pond_id, a.timestamp) AS cnt FROM alerts a {where}", params)
    total_count = int(cur.fetchone()["cnt"])
    total_pages = max(1, math.ceil(total_count / page_size))

    if page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size

    # 2) data（每筆 alert 一列，含 status/handler + 對應 pond_data 值）
    sql = f"""
        SELECT
            MIN(a.id) AS id,
            a.pond_id,
            a.timestamp,
            GROUP_CONCAT(DISTINCT a.parameter ORDER BY a.parameter SEPARATOR ',') AS parameters,
            MAX(a.notified) AS notified,
            MAX(a.status)   AS status,
            MAX(a.handler)  AS handler,
            MIN(a.handle_start_at) AS handle_start_at,
            MAX(a.handle_end_at)   AS handle_end_at,
            p.temperature, p.salinity, p.ph, p.do_level, p.orp
        FROM alerts a
        LEFT JOIN pond_data p
        ON p.pond_id = a.pond_id AND p.timestamp = a.timestamp
        {where}
        GROUP BY a.pond_id, a.timestamp
        ORDER BY a.timestamp DESC
        LIMIT %s OFFSET %s
    """
    cur.execute(sql, params + [page_size, offset])
    rows = cur.fetchall()
    th = _load_thresholds_safe()
    processed = []

    # === 新增：計算哪些頁碼有未處理（用同排序的 grouped events）===
    cur.execute(f"""
        SELECT
            a.pond_id,
            a.timestamp,
            MAX(a.status) AS status
        FROM alerts a
        {where}
        GROUP BY a.pond_id, a.timestamp
        ORDER BY a.timestamp DESC
    """, params)
    grouped_all = cur.fetchall()
    status_list = [(r.get("status") or "未處理") for r in grouped_all]
    pages_with_unhandled = _pages_with_unhandled(status_list, page_size)

    cur.close()
    conn.close()

    for r in rows:
        ts_a = r["timestamp"]

        params_str = r.get("parameters") or ""
        params_list = [x.strip() for x in params_str.split(",") if x.strip()]
        tags = []
        for p in params_list:
            if p.lower() == "ph":
                tags.append(("pH", "tag-warn"))
            elif p.lower() == "orp":
                tags.append(("ORP", "tag-warn"))
            elif p.lower() in ("do_level", "do"):
                tags.append(("溶氧 低", "tag-warn"))
            elif p.lower() in ("temperature", "temp"):
                tags.append(("溫 高", "tag-warn"))  # 或你想顯示「溫度」
            elif p.lower() == "salinity":
                tags.append(("鹽", "tag-warn"))
            else:
                tags.append((p, "tag-warn"))

        n = r.get("notified")
        if n == 1:
            notified_txt = "已通知"
        elif n == 0:
            notified_txt = "未通知"
        else:
            notified_txt = "—"

        processed.append({
            "id": r["id"],
            "kind": "data",
            "event_type": "data",
            "ts": ts_a.strftime("%Y-%m-%d %H:%M:%S"),
            "pond": r["pond_id"],
            "tags": tags,
            "video": "",
            "notified": notified_txt,
            "status": r.get("status") or "未處理",
            "handler": r.get("handler"),
            "handle_start_at": r["handle_start_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("handle_start_at") else None,
            "handle_end_at":   r["handle_end_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("handle_end_at") else None,
        })

    return render_template(
        "history_result.html",
        pond_id=pond_id or "（全部魚池）",
        start=start_str,
        end=end_str,
        rows2=processed,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        total_count=total_count,
        abnormal_type=abnormal_type,
        pages_with_unhandled=pages_with_unhandled,
        highlight_ts=request.args.get("highlight_ts"),
    )


@app.route("/api/mark_event_handled", methods=["POST"])
@login_required
def api_mark_event_handled():
    data = request.get_json(silent=True) or {}

    pond_id = (data.get("pond_id") or "").strip()
    ts_str  = (data.get("ts") or "").strip()

    evt_id   = data.get("id")  # behavior/leftover 用
    evt_type = (data.get("type") or "").strip()

    handler_name = (data.get("handler_name") or (session.get("user") or {}).get("username") or "").strip()

    # 舊版相容：handled=true/false
    handled = data.get("handled", None)
    next_status = (data.get("next_status") or "").strip()

    if evt_type not in ("behavior", "leftover", "data"):
        return jsonify({"ok": False, "error": "unsupported type"}), 400

    # === 決定 next_status（兼容舊前端）===
    if not next_status:
        if handled is True:
            next_status = "已處理"
        elif handled is False:
            next_status = "未處理"
        else:
            return jsonify({"ok": False, "error": "missing next_status"}), 400

    if next_status not in ("未處理", "處理中", "已處理"):
        return jsonify({"ok": False, "error": "bad next_status"}), 400

    # 規則：處理中 / 已處理 必須有 handler
    if next_status in ("處理中", "已處理") and not handler_name:
        return jsonify({"ok": False, "error": "handler_name required"}), 400

    # === data 走 pond_id + ts；其他走 id ===
    if evt_type == "data":
        if not pond_id or not ts_str:
            return jsonify({"ok": False, "error": "missing pond_id/ts for data"}), 400
        try:
            ts_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return jsonify({"ok": False, "error": "bad ts format"}), 400

        table = "alerts"
        where_sql = "WHERE pond_id=%s AND timestamp=%s"
        where_params = (pond_id, ts_dt)

    else:
        if evt_id is None:
            return jsonify({"ok": False, "error": "missing id"}), 400
        try:
            evt_id = int(evt_id)
        except Exception:
            return jsonify({"ok": False, "error": "id must be int"}), 400

        table = "behavior_events" if evt_type == "behavior" else "leftover_events"
        where_sql = "WHERE id=%s"
        where_params = (evt_id,)

    conn = None
    cur = None
    try:
        conn = mysql.connector.connect(**db_config)
        cur = conn.cursor(dictionary=True)

        # 先確認事件存在（data 是「整組」→ 看任一筆是否存在）
        cur.execute(
            f"SELECT id FROM {table} {where_sql} LIMIT 1",
            where_params
        )
        exists = cur.fetchone()
        if not exists:
            return jsonify({"ok": False, "error": "event not found"}), 404

        # === 更新規則 ===
        if next_status == "未處理":
            # reset：清空 handler + 時間
            cur.execute(
                f"""
                UPDATE {table}
                SET status=%s,
                    handler=NULL,
                    handle_start_at=NULL,
                    handle_end_at=NULL
                {where_sql}
                """,
                ("未處理", *where_params)
            )

        elif next_status == "處理中":
            # start：只在第一次開始時寫入 start_at（避免重按覆蓋）
            cur.execute(
                f"""
                UPDATE {table}
                SET status=%s,
                    handler=%s,
                    handle_start_at=COALESCE(handle_start_at, NOW()),
                    handle_end_at=NULL
                {where_sql}
                """,
                ("處理中", handler_name, *where_params)
            )

        else:  # 已處理
            # finish：若還沒 start_at，就補 start_at；寫 end_at
            cur.execute(
                f"""
                UPDATE {table}
                SET status=%s,
                    handler=%s,
                    handle_start_at=COALESCE(handle_start_at, NOW()),
                    handle_end_at=NOW()
                {where_sql}
                """,
                ("已處理", handler_name, *where_params)
            )

        conn.commit()

        # === 回傳更新後的資料（前端更新按鈕 + dashboard 連結用）===
        # data：同一組可能多筆 → 用 MAX/MIN 彙總成一筆回傳
        if evt_type == "data":
            cur.execute(
                f"""
                SELECT
                    MIN(id) AS id,
                    MAX(status) AS status,
                    MAX(handler) AS handler,
                    MIN(handle_start_at) AS handle_start_at,
                    MAX(handle_end_at) AS handle_end_at
                FROM {table}
                {where_sql}
                """,
                where_params
            )
        else:
            cur.execute(
                f"""
                SELECT
                    id,
                    status,
                    handler,
                    handle_start_at,
                    handle_end_at
                FROM {table}
                {where_sql}
                LIMIT 1
                """,
                where_params
            )

        after = cur.fetchone() or {}

        def fmt_dt(x):
            return x.strftime("%Y-%m-%d %H:%M:%S") if x else None

        out_id = after.get("id") if evt_type != "data" else int(after.get("id") or 0)

        status = after.get("status") or next_status
        handler = after.get("handler")
        start_at = fmt_dt(after.get("handle_start_at"))
        end_at = fmt_dt(after.get("handle_end_at"))

        display = status
        if status == "已處理" and handler:
            display = f"{handler} 已處理"
        elif status == "處理中" and handler:
            display = f"{handler} 處理中"

        return jsonify({
            "ok": True,
            "id": out_id,
            "type": evt_type,
            "status": status,
            "handler": handler,
            "handle_start_at": start_at,
            "handle_end_at": end_at,
            "display": display,

            # ✅ data 專用：讓你前端如果需要，可以知道這次更新的是哪一組
            "pond_id": pond_id if evt_type == "data" else None,
            "ts": ts_str if evt_type == "data" else None,
        })

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# =====================================================
# 人員處理狀況
# =====================================================
@app.route('/api/handler_stats', methods=['GET'])
@login_required
def api_handler_stats():
    pond_id = (request.args.get('pond_id') or '').strip()
    start   = (request.args.get('start') or '').strip()
    end     = (request.args.get('end') or '').strip()
    abnormal_type = (request.args.get('abnormal_type') or 'all').strip()

    if not (start and end):
        return jsonify({"ok": False, "error": "missing start/end"}), 400

    # 每種事件對應：資料表 & 時間欄位
    SOURCES = {
        "data":     ("alerts", "timestamp"),
        "behavior": ("behavior_events", "event_time"),
        "leftover": ("leftover_events", "event_time"),
    }

    # 決定要統計哪些來源
    if abnormal_type in ("data", "behavior", "leftover"):
        selected = [abnormal_type]
    else:  # all
        selected = ["data", "behavior", "leftover"]

    subqueries = []
    params = []

    for k in selected:
        table, time_col = SOURCES[k]

        pond_clause = ""
        one_params = [start, end]
        if pond_id:
            pond_clause = " AND pond_id = %s"
            one_params.append(pond_id)

        if k == "data":
    # ✅ alerts：用「事件」計數（同 pond_id + timestamp 算一次）
            subqueries.append(f"""
                SELECT handler, COUNT(*) AS cnt
                FROM (
                    SELECT pond_id, timestamp, handler
                    FROM alerts
                    WHERE timestamp BETWEEN %s AND %s
                    {pond_clause}
                    AND status = '已處理'
                    AND handler IS NOT NULL
                    AND handler <> ''
                    GROUP BY pond_id, timestamp, handler
                ) t
                GROUP BY handler
            """.strip())
        else:
            # ✅ behavior / leftover：一列就是一事件，維持原本 COUNT(*)
            subqueries.append(f"""
                SELECT handler, COUNT(*) AS cnt
                FROM {table}
                WHERE {time_col} BETWEEN %s AND %s
                {pond_clause}
                AND status = '已處理'
                AND handler IS NOT NULL
                AND handler <> ''
                GROUP BY handler
            """.strip())

        params.extend(one_params)

    union_sql = " UNION ALL ".join([f"({q})" for q in subqueries])

    final_sql = f"""
        SELECT handler, SUM(cnt) AS cnt
        FROM ({union_sql}) AS x
        GROUP BY handler
        ORDER BY cnt DESC
    """

    try:
        conn = mysql.connector.connect(**db_config)
        cur  = conn.cursor()
        cur.execute(final_sql, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        labels, values, total = [], [], 0
        for handler, cnt in rows:
            cnt = int(cnt or 0)
            if cnt <= 0:
                continue
            labels.append(handler)
            values.append(cnt)
            total += cnt

        return jsonify({"ok": True, "labels": labels, "values": values, "total": total})

    except Exception as e:
        print("api_handler_stats error:", e)
        return jsonify({"ok": False, "error": "db_error"}), 500

# =====================================================
# 系統設定（門檻）
# =====================================================
@app.route('/system_settings')
@login_required
def system_settings():
    th = get_thresholds()
    return render_template('system_settings.html', thresholds=th)


@app.route('/save_settings', methods=['POST'])
@login_required
def save_settings():
    payload = {
        "temp_min": request.form.get("temp_min", type=float),
        "temp_max": request.form.get("temp_max", type=float),
        "salinity_min": request.form.get("salinity_min", type=float),
        "salinity_max": request.form.get("salinity_max", type=float),
        "ph_min": request.form.get("ph_min", type=float),
        "ph_max": request.form.get("ph_max", type=float),
        "do_min": request.form.get("do_min", type=float),
        "orp_min": request.form.get("orp_min", type=float),
        "orp_max": request.form.get("orp_max", type=float),
    }
    if any(v is None for v in payload.values()):
        return redirect(url_for('system_settings'))

    save_thresholds(payload)
    return redirect(url_for('system_settings'))

# ===== LINE 設定（建議放環境變數） =====
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"
def send_line_push(text: str) -> bool:
    """
    推播一則文字訊息到 LINE（推給指定 userId）
    成功回 True，失敗回 False
    """
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TO_USER_ID:
        print("[LINE] Missing LINE_CHANNEL_ACCESS_TOKEN or LINE_TO_USER_ID")
        return False

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": LINE_TO_USER_ID,
        "messages": [{"type": "text", "text": text}],
    }

    try:
        r = requests.post(LINE_PUSH_API, headers=headers, json=payload, timeout=8)
        if r.ok:
            return True
        else:
            print("[LINE] Push failed:", r.status_code, r.text)
            return False
    except Exception as e:
        print("[LINE] Push error:", e)
        return False


def send_line_water_alert(pond_id: str, field: str, value, min_v, max_v, history_url: str) -> bool:
    """
    水質異常 LINE 通知（配合你原本 Discord 的內容）
    """
    FIELD_INFO = {
        "temperature": ("溫度", "°C"),
        "salinity":    ("鹽度", "PSU"),
        "ph":          ("pH",   ""),
        "do_level":    ("溶氧", "mg/L"),
        "orp":         ("ORP",  "mV"),
    }
    label, unit = FIELD_INFO.get(field, (field, ""))

    def fmt(v):
        return f"{v}{unit}" if unit else str(v)

    if max_v is None:
        range_text = f">= {fmt(min_v)}"
    else:
        range_text = f"{fmt(min_v)} ~ {fmt(max_v)}"

    msg = (
        f"⚠️ 魚池異常警告！\n"
        f"池子：{pond_id}\n"
        f"項目：{label}\n"
        f"當前值：{fmt(value)}\n"
        f"正常範圍：{range_text}\n"
        f"查看數值紀錄：{history_url}"
    )
    return send_line_push(msg)


def send_line_event_alert(event_kind: str, pond_id: str, event_time: datetime, history_url: str, extra: str = "") -> bool:
    """
    行為/殘料異常 LINE 通知
    event_kind: "behavior" / "leftover"
    """
    kind_name = {
        "behavior": "行為異常",
        "leftover": "殘料異常",
    }.get(event_kind, event_kind)

    t = event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time else "-"

    msg = (
        f"⚠️ 魚池異常警告！\n"
        f"池子：{pond_id}\n"
        f"類型：{kind_name}\n"
        f"時間：{t}\n"
        f"{('說明：' + extra + '\\n') if extra else ''}"
        f"查看異常紀錄：{history_url}"
    )
    return send_line_push(msg)

# =====================================================
# Discord 通知 & alerts 寫入
# =====================================================

DISCORD_WEBHOOK_URL = ""


def _build_history_link(pond_id: str, abnormal_type: str = None) -> str:
    payload = {
        "pond_id": pond_id,
        "from_alert": "1",
    }
    if abnormal_type:
        payload["abnormal_type"] = abnormal_type
    qs = urllib.parse.urlencode(payload)
    return f"{ALERT_BASE_URL}/history_result?{qs}"



def send_discord_alert(pond_id, field, value, min_v, max_v) -> bool:
    """
    送 Discord，成功回傳 True，失敗 False。
    會自動把欄位改成中文名稱並加上單位。
    """
    history_url = _build_history_link(pond_id)

    # 欄位對應：英文欄位名 → (中文名稱, 單位)
    FIELD_INFO = {
        "temperature": ("溫度", "°C"),
        "salinity":    ("鹽度", "PSU"),
        "ph":          ("pH",   ""),
        "do_level":    ("溶氧", "mg/L"),
        "orp":         ("ORP",  "mV"),
    }
    label, unit = FIELD_INFO.get(field, (field, ""))

    # 幫數值加上單位
    def fmt(v):
        return f"{v}{unit}" if unit else str(v)

    # 只有下限（例如溶氧）的情況
    if max_v is None:
        range_text = f">= {fmt(min_v)}"
    else:
        range_text = f"{fmt(min_v)} ~ {fmt(max_v)}"

    message = (
        f"⚠️ 魚池異常警告！\n"
        f"池子：{pond_id}\n"
        f"項目：{label}\n"
        f"當前值：{fmt(value)}\n"
        f"正常範圍：{range_text}\n"
        f"查看數值紀錄：{history_url}"
    )

    try:
        r = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=5,
        )
        if r.ok:
            return True
        else:
            print("Discord 回傳錯誤狀態碼:", r.status_code, r.text)
            return False
    except Exception as e:
        print("Discord 通知錯誤：", e)
        return False

def send_discord_event_alert(event_kind: str, pond_id: str, event_time: datetime, extra: str = "") -> bool:
    """event_kind: behavior / leftover"""
    history_url = _build_history_link(pond_id, abnormal_type=event_kind)

    kind_name = {
        "behavior": "行為異常",
        "leftover": "殘料異常",
    }.get(event_kind, event_kind)

    t = event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time else "-"

    message = (
        f"⚠️ 魚池異常警告！\n"
        f"池子：{pond_id}\n"
        f"類型：{kind_name}\n"
        f"時間：{t}\n"
        f"{('說明：' + extra + '\\n') if extra else ''}"
        f"查看異常紀錄：{history_url}"
    )

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
        return bool(r.ok)
    except Exception as e:
        print("Discord 通知錯誤：", e)
        return False



def add_alert(
    pond_id,
    param,
    value,
    th_min,
    th_max,
    message,
    notify_success=None,   # 1 / 0 / None
    ts=None,               # 對齊 pond_data.timestamp
):
    """
    寫入 alerts：
    - pond_id, parameter, value, threshold_min/max, message
    - notified: 1=已通知, 0=通知失敗, NULL=沒送
    - timestamp: 優先用 ts，沒有就 NOW()
    """
    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()

    notified_value = None
    if notify_success is not None:
        notified_value = 1 if notify_success else 0

    if ts is None:
        cur.execute(
            """
            INSERT INTO alerts
                (pond_id, timestamp, parameter, value,
                 threshold_min, threshold_max, message, notified)
            VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s)
            """,
            (pond_id, param, value, th_min, th_max, message, notified_value),
        )
    else:
        cur.execute(
            """
            INSERT INTO alerts
                (pond_id, timestamp, parameter, value,
                 threshold_min, threshold_max, message, notified)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (pond_id, ts, param, value, th_min, th_max, message, notified_value),
        )

    conn.commit()
    cur.close()
    conn.close()


def already_notified_recently(pond_id, param):
    """30 分鐘內同池同參數是否已經有 alert 記錄"""
    conn = mysql.connector.connect(**db_config)
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*)
        FROM alerts
        WHERE pond_id=%s
          AND parameter=%s
          AND timestamp >= NOW() - INTERVAL 30 MINUTE
    """, (pond_id, param))
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count > 0


def check_and_record_alerts(pond_id, data, th):
    """
    - 檢查一筆最新資料是否異常
    - 寫入 alerts
    - 發 Discord 通知，並把成功/失敗記錄到 notified
    data: {
      "timestamp": "YYYY-MM-DD HH:MM:SS",
      "temperature": ...,
      "salinity": ...,
      "ph": ...,
      "do_level": ...,
      "orp": ...
    }
    """
    checks = [
        ("temperature", data["temperature"], th["temp_min"], th["temp_max"]),
        ("salinity", data["salinity"], th["salinity_min"], th["salinity_max"]),
        ("ph", data["ph"], th["ph_min"], th["ph_max"]),
        ("do_level", data["do_level"], th["do_min"], None),
        ("orp", data["orp"], th["orp_min"], th["orp_max"]),
    ]

    ts_str = data.get("timestamp")
    alert_ts = None
    if ts_str:
        try:
            alert_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            alert_ts = None

    for param, value, th_min, th_max in checks:
        if value is None:
            continue

        if param == "do_level":
            is_abn = value < th_min
        else:
            is_abn = (value < th_min) or (value > th_max)

        if not is_abn:
            continue

        msg = f"{pond_id} {param} 異常：{value}"

        if already_notified_recently(pond_id, param):
            # 30 分鐘內已通知過 → 不重送，但記錄一筆 notified=NULL
            add_alert(
                pond_id, param, value,
                th_min, th_max, msg,
                notify_success=None,
                ts=alert_ts,
            )
        else:
            ok = send_discord_alert(pond_id, param, value, th_min, th_max)
            add_alert(
                pond_id, param, value,
                th_min, th_max, msg,
                notify_success=ok,
                ts=alert_ts,
            )


# =====================================================
# 其他 API：最新狀態 / live chart / 異常明細 / 類型統計
# =====================================================

@app.route("/api/latest_status")
def latest_status():
    pond_id = request.args.get("pond_id", "Pond A")

    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT timestamp, temperature, salinity, ph, do_level, orp
        FROM pond_data
        WHERE pond_id = %s
        ORDER BY timestamp DESC
        LIMIT 1
    """, (pond_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if row:
        ts, temp, salinity, ph, do_level, orp = row
        data = {
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "temperature": temp,
            "salinity": salinity,
            "ph": ph,
            "do_level": do_level,
            "orp": orp,
        }

        #th = get_thresholds()
        #check_and_record_alerts(pond_id, data, th)

        return jsonify(data)
    else:
        return jsonify({"error": "No data found"}), 404


@app.route("/api/live_data")
@login_required
def live_data():
    pond_id = request.args.get("pond_id", "Pond A")
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT timestamp, temperature
        FROM pond_data
        WHERE pond_id = %s
        ORDER BY timestamp DESC
        LIMIT 20
    """, (pond_id,))
    rows = cursor.fetchall()
    timestamps = [row[0].strftime("%H:%M:%S") for row in reversed(rows)]
    values = [row[1] for row in reversed(rows)]
    cursor.close()
    conn.close()
    return jsonify({"timestamps": timestamps, "values": values})


@app.route("/abnormal_detail/<pond_id>/<timestamp>")
@login_required
def abnormal_detail(pond_id, timestamp):
    conn = mysql.connector.connect(**db_config)
    th = get_thresholds(conn)
    cursor = conn.cursor()

    dt_start = datetime.fromisoformat(timestamp)
    dt_end = dt_start + timedelta(hours=1)

    cursor.execute("""
        SELECT timestamp, temperature, salinity, ph, do_level, orp
        FROM pond_data
        WHERE pond_id = %s AND timestamp >= %s AND timestamp < %s
        ORDER BY timestamp
    """, (pond_id, dt_start, dt_end))
    rows = cursor.fetchall()
    conn.close()

    abnormal_rows = []
    for ts, temp, salinity, ph, do_level, orp in rows:
        abnormal = is_abnormal(temp, salinity, ph, do_level, orp, th)
        abnormal_rows.append({
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "temperature": temp,
            "salinity": salinity,
            "ph": ph,
            "do_level": do_level,
            "orp": orp,
            "is_abnormal": abnormal,
        })

    return render_template(
        "abnormal_detail.html",
        pond_id=pond_id,
        timestamp=timestamp,
        rows=abnormal_rows,
        thresholds=th,
    )


@app.route("/abnormal_type_stats")
@login_required
def abnormal_type_stats():
    try:
        pond_id = request.args.get("pond_id", default="Pond A")
        conn = mysql.connector.connect(**db_config)
        th = get_thresholds(conn)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT temperature, salinity, ph, do_level, orp
            FROM pond_data
            WHERE timestamp >= NOW() - INTERVAL 1 DAY
              AND pond_id = %s
        """, (pond_id,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        abnormal_type_count = {
            "temperature": 0,
            "salinity": 0,
            "pH": 0,
            "do_level": 0,
            "orp": 0,
        }

        for temp, salinity, ph, do_level, orp in rows:
            if temp is not None and (temp < th["temp_min"] or temp > th["temp_max"]):
                abnormal_type_count["temperature"] += 1
            if salinity is not None and (
                salinity < th["salinity_min"] or salinity > th["salinity_max"]
            ):
                abnormal_type_count["salinity"] += 1
            if ph is not None and (ph < th["ph_min"] or ph > th["ph_max"]):
                abnormal_type_count["pH"] += 1
            if do_level is not None and (do_level < th["do_min"]):
                abnormal_type_count["do_level"] += 1
            if orp is not None and (orp < th["orp_min"] or orp > th["orp_max"]):
                abnormal_type_count["orp"] += 1

        return jsonify(abnormal_type_count)
    except Exception as e:
        print("Error in /abnormal_type_stats:", e)
        return jsonify({"error": str(e)}), 500


# =====================================================
# AR 遠端協作系統
# =====================================================

@app.route("/ar_remote")
@login_required
def ar_remote():
    """展示 AR 遠端專家控制台"""
    return render_template("ar_monitor.html")


# MQTT 全局變量（延遲初始化）
mqtt_client = None
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "myproject/screen/test1"

def init_mqtt():
    """初始化 MQTT 連線"""
    global mqtt_client
    if mqtt_client is None:
        try:
            import paho.mqtt.client as mqtt
            mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_start()
            print(f"✅ MQTT 已連線至: {MQTT_BROKER}")
        except Exception as e:
            print(f"⚠️ MQTT 連線失敗: {e}")
            return False
    return True


@app.route('/api/ar_upload', methods=['POST'])
@login_required
def ar_upload():
    """接收從 AR 介面傳來的圖片，並透過 MQTT 轉發給手機"""
    try:
        # 1. 收到網頁傳來的原始圖片數據 (Binary)
        img_data = request.data
        
        # 2. 初始化 MQTT
        if not init_mqtt():
            return jsonify({"error": "MQTT 連線失敗"}), 500
        
        # 3. 透過 MQTT 轉發圖片
        if len(img_data) > 0:
            mqtt_client.publish(MQTT_TOPIC, img_data)
            print(f"🚀 已轉發圖片至 MQTT，大小: {len(img_data)} bytes")
            return jsonify({"status": "success", "size": len(img_data)}), 200
        else:
            return jsonify({"error": "圖片資料為空"}), 400
            
    except Exception as e:
        print(f"❌ AR 上傳錯誤: {e}")
        return jsonify({"error": str(e)}), 500


# =====================================================
# 背景監控：定期巡檢最新資料並送通知
# =====================================================
def _scan_and_notify_events(cursor, table: str, time_col: str, event_kind: str):
    cursor.execute(f"""
        SELECT id, pond_id, {time_col}
        FROM {table}
        WHERE {time_col} >= NOW() - INTERVAL 1 HOUR
          AND notified IS NULL
        ORDER BY {time_col} DESC
        LIMIT 20
    """)
    rows = cursor.fetchall()

    for r in rows:
        evt_id = r["id"]
        pond   = r["pond_id"]
        t      = r[time_col]

        # ✅ 產生對應種類的 history link
        history_url = _build_history_link(pond, abnormal_type=event_kind)

        # ✅ 同時送 Discord + LINE
        ok_discord = send_discord_event_alert(event_kind, pond, t)
        ok_line    = send_line_event_alert(event_kind, pond, t, history_url)

        ok_any = ok_discord or ok_line

        # ✅ 寫回 notified：1=至少一個成功, 0=都失敗
        cursor.execute(
            f"UPDATE {table} SET notified=%s WHERE id=%s",
            (1 if ok_any else 0, evt_id)
        )

def background_monitor():
    print(" 後端背景監控啟動中...")

    while True:
        conn = None
        cursor = None
        try:
            conn = mysql.connector.connect(**db_config)
            cursor = conn.cursor(dictionary=True)

            ponds = ["Pond A", "Pond B", "Pond C"]

            for pond in ponds:
                cursor.execute("""
                    SELECT *
                    FROM pond_data
                    WHERE pond_id = %s
                    ORDER BY timestamp DESC
                    LIMIT 1
                """, (pond,))
                row = cursor.fetchone()
                if not row:
                    continue

                th = get_thresholds()

                checks = {
                    "temperature": (row["temperature"], th["temp_min"], th["temp_max"]),
                    "salinity": (row["salinity"], th["salinity_min"], th["salinity_max"]),
                    "ph": (row["ph"], th["ph_min"], th["ph_max"]),
                    "do_level": (row["do_level"], th["do_min"], None),
                    "orp": (row["orp"], th["orp_min"], th["orp_max"]),
                }

                for field, (value, min_v, max_v) in checks.items():
                    if value is None:
                        continue

                    is_bad = (value < min_v) if field == "do_level" else (value < min_v or value > max_v)
                    if not is_bad:
                        continue

                    key = f"{pond}_{field}"
                    now = datetime.now()

                    if key not in last_alert_time or (now - last_alert_time[key]).total_seconds() > ALERT_COOLDOWN:
                        last_alert_time[key] = now

                        msg = f"{pond} {field} 異常：{value}"
                        history_url = _build_history_link(pond)  # 水質異常不帶 abnormal_type

                        ok_discord = send_discord_alert(pond, field, value, min_v, max_v)
                        ok_line    = send_line_water_alert(pond, field, value, min_v, max_v, history_url)
                        success = ok_discord or ok_line

                        add_alert(
                            pond, field, value, min_v, max_v, msg,
                            notify_success=success,
                            ts=row["timestamp"],
                        )

                        status_text = "成功" if success else "失敗"
                        print(f"⚠️ 水質通知{status_text}：{pond}, {field}")

            # ✅ 行為 + 殘料：掃描 notified IS NULL 的事件
            _scan_and_notify_events(cursor, "behavior_events", "event_time", "behavior")
            _scan_and_notify_events(cursor, "leftover_events", "event_time", "leftover")

            conn.commit()

        except Exception as e:
            print("背景監控錯誤：", e)
            if conn:
                conn.rollback()

        finally:
            try:
                if cursor: cursor.close()
            except: pass
            try:
                if conn: conn.close()
            except: pass

        sleep(10)



# =====================================================
# main
# =====================================================

if __name__ == "__main__":
    # 避免 reloader 啟動兩次 thread
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        monitor_thread = threading.Thread(target=background_monitor, daemon=True)
        monitor_thread.start()

    app.run(host="0.0.0.0", port=5000, debug=True)
