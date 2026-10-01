#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import mysql.connector
from datetime import datetime
import random

# 連線設定（跟你 app.py 一樣）
DB_CONFIG = {
    "host": "localhost",
    "user": "testuser",
    "password": "test123",
    "database": "ponddb_new",
}

# 可以隨機選的魚池 & 事件類型
PONDS = ["Pond A", "Pond B", "Pond C"]
EVENT_TYPES = ["翻身異常", "游動異常", "群聚異常"]  # 你也可以只留一個類型


def insert_behavior_event(pond_id, event_type, video_path=None, event_time=None):
    """插入一筆行為異常到 behavior_events"""
    if event_time is None:
        event_time = datetime.now()

    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()

    sql = """
        INSERT INTO behavior_events (pond_id, event_type, event_time, video_path)
        VALUES (%s, %s, %s, %s)
    """
    cur.execute(sql, (pond_id, event_type, event_time, video_path))
    conn.commit()

    print(f"[OK] 插入行為異常：{pond_id} / {event_type} / {event_time}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    # ✅ 這裡決定要插哪一個魚池、哪一種異常
    pond = "Pond A"       # 想指定就改成 pond = "Pond A"
    etype = "翻身異常"
  # 想指定就改成 etype = "翻身異常"

    insert_behavior_event(pond, etype)
    print("✅ 本次程式執行結束（只插入一筆）")
