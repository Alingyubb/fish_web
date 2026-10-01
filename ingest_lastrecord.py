#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ingest_lastrecord.py  (SensorData 版)
- 每 60 秒抓三個 GetLastRecord（eid=30091/92/93），寫入 MySQL: ponddb.pond_data
- 回傳沒有 timestamp → 以擷取當下的台北時間作為 timestamp（naive DATETIME）
- 需要 UNIQUE(pond_id, timestamp) 防重
"""

import time
import json
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import requests
import mysql.connector
import pytz
import re

# ========= 基本設定 =========
DB_CONFIG = {
    "host": "localhost",
    "user": "testuser",
    "password": "test123",
    "database": "ponddb_new"
}
API_BASE = "http://colin.hr-env.com/System/Record/GetLastRecord"

# eid ↔ pond_id 對照
EID_TO_POND = {
    30091: "Pond A",
    30092: "Pond B",
    30093: "Pond C",
}

# 輪詢間隔（秒）
POLL_INTERVAL = 60

# HTTP 請求設定
HTTP_TIMEOUT = 5  # 秒
HTTP_RETRIES = 2  # 額外重試次數（總共 = 1 + retries）

# 判定資料停滯（同一池連續幾次拿到同一 timestamp）
STALE_THRESHOLD = 3

# 合理範圍（入庫前檢查；超出範圍時回 None）
VALID_RANGES = {
    "temperature": (-10, 50),
    "salinity": (0, 50),
    "ph": (0, 14),
    "do_level": (0, 40),
    "orp": (0, 500),
}
COERCE_OUT_OF_RANGE_TO_NONE = True

# SensorData -> 我們的欄位 對照規則（比對 T 的文字，忽略大小寫與空白）
SENSOR_T_PATTERNS = [
    (re.compile(r"^\s*temp(erature)?\s*$", re.I), "temperature"),
    (re.compile(r"^\s*p\s*H\s*$", re.I), "ph"),
    (re.compile(r"^\s*do(\s*|_)?(level)?\s*$", re.I), "do_level"),
    (re.compile(r"^\s*oxygen|dissolved\s*oxygen\s*$", re.I), "do_level"),
    (re.compile(r"^\s*salin(i|)ty\s*$", re.I), "salinity"),   # 容錯 "Salini..."
    (re.compile(r"^\s*orp|oxidation|redox\s*$", re.I), "orp"),
]

# ========= 記錄器 =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ingestor")

# ========= 時區 =========
TZ_TAIPEI = pytz.timezone("Asia/Taipei")


def clamp_or_none(name: str, value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except Exception:
        return None
    lo, hi = VALID_RANGES.get(name, (-1e18, 1e18))
    if (f < lo or f > hi) and COERCE_OUT_OF_RANGE_TO_NONE:
        return None
    return f


def fetch_last_record(eid: int) -> Optional[Dict[str, Any]]:
    url = f"{API_BASE}?eid={eid}"
    last_err = None
    for _ in range(1 + HTTP_RETRIES):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                time.sleep(0.5)
                continue
            return r.json()
        except Exception as e:
            last_err = str(e)
            time.sleep(0.5)
    log.error(f"[eid={eid}] 取資料失敗: {last_err}")
    return None


def map_sensor_T_to_field(t_text: str) -> Optional[str]:
    for pat, field in SENSOR_T_PATTERNS:
        if pat.search(t_text or ""):
            return field
    return None


def transform_record_from_sensordata(raw: Dict[str, Any]) -> Tuple[datetime, Dict[str, Optional[float]]]:
    """
    來源沒有 timestamp → 以「擷取當下台北時間」作為 timestamp。
    從 raw['SensorData'] 解析各指標（以 T 映射、取 V）。
    """
    # 1) 時間：用擷取當下台北時間（naive）
    now_tpe = datetime.now(TZ_TAIPEI).replace(tzinfo=None)

    fields = {
        "temperature": None,
        "salinity": None,
        "ph": None,
        "do_level": None,
        "orp": None,
    }

    sd = raw.get("SensorData")
    if not isinstance(sd, list):
        log.warning(f"SensorData 缺失或格式錯誤: {json.dumps(raw, ensure_ascii=False)[:200]}")
        return now_tpe, fields

    for item in sd:
        if not isinstance(item, dict):
            continue
        t_text = str(item.get("T") or "")
        v = item.get("V")
        field = map_sensor_T_to_field(t_text)
        if field:
            fields[field] = clamp_or_none(field, v)

    return now_tpe, fields


def upsert_record(conn, pond_id: str, ts: datetime, fields: Dict[str, Optional[float]]):
    sql = """
    INSERT INTO pond_data
      (pond_id, timestamp, temperature, salinity, ph, do_level, orp)
    VALUES
      (%s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
      temperature = VALUES(temperature),
      salinity    = VALUES(salinity),
      ph          = VALUES(ph),
      do_level    = VALUES(do_level),
      orp         = VALUES(orp)
    """
    args = (
        pond_id,
        ts,  # naive DATETIME（視為台北時間）
        fields["temperature"],
        fields["salinity"],
        fields["ph"],
        fields["do_level"],
        fields["orp"],
    )
    cur = conn.cursor()
    cur.execute(sql, args)
    cur.close()


def ingest_once(state: Dict[str, Any]) -> None:
    conn = mysql.connector.connect(**DB_CONFIG)
    inserted = 0
    try:
        for eid, pond_id in EID_TO_POND.items():
            raw = fetch_last_record(eid)
            if not raw:
                continue

            # 有些系統會包一層 data；你的回傳看起來沒有，但保留容錯
            payload = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw

            ts, fields = transform_record_from_sensordata(payload)

            # 偵測停滯（以擷取時間等於上一輪 → 倘若 API 真的是同一瞬間回來也會不同；
            # 若你未來拿到真正的來源時間，再改成用來源時間做停滯偵測）
            last_ts = state.get(pond_id, {}).get("last_ts")
            stale_cnt = state.get(pond_id, {}).get("stale_cnt", 0)
            if last_ts and last_ts == ts:
                stale_cnt += 1
            else:
                stale_cnt = 0
            state[pond_id] = {"last_ts": ts, "stale_cnt": stale_cnt}
            if stale_cnt >= STALE_THRESHOLD:
                log.warning(f"[{pond_id}] 資料停滯（連續 {stale_cnt+1} 次相同 timestamp: {ts}）")

            upsert_record(conn, pond_id, ts, fields)
            inserted += 1

        conn.commit()
    except Exception as e:
        log.exception(f"寫入發生錯誤：{e}")
        conn.rollback()
    finally:
        conn.close()

    log.info(f"本輪完成：{inserted} 筆 upsert")


def main():
    log.info("啟動 ingestor（SensorData 版）：每 60 秒拉取 GetLastRecord → 寫入 ponddb_new.pond_data")
    log.info("提醒：請先在 DB 建立唯一鍵：ALTER TABLE pond_data ADD UNIQUE KEY uniq_pond_time (pond_id, timestamp);")
    state: Dict[str, Any] = {}
    while True:
        start = time.time()
        try:
            ingest_once(state)
        except Exception:
            log.exception("本輪 ingest 發生未捕捉的例外")
        elapsed = time.time() - start
        sleep_sec = max(0.0, POLL_INTERVAL - elapsed)
        time.sleep(sleep_sec)


if __name__ == "__main__":
    main()
