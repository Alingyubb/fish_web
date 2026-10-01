#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import mysql.connector
from datetime import datetime

# 連線設定（跟你 simulate_behavior_event.py 一樣）:contentReference[oaicite:1]{index=1}
DB_CONFIG = {
    "host": "localhost",
    "user": "testuser",
    "password": "test123",
    "database": "ponddb_new",
}

PONDS = ["Pond A", "Pond B", "Pond C"]

def insert_leftover_event(pond_id, event_type="殘料異常", video_path=None, event_time=None):
    """插入一筆飼料/殘料異常到 leftover_events"""
    if event_time is None:
        event_time = datetime.now()

    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()

    # ✅ 這裡假設你的表叫 leftover_events，欄位跟 behavior_events 類似
    # 若你實際欄位名稱不同（例如 time 欄叫 timestamp），我再幫你對齊
    sql = """
        INSERT INTO leftover_events (pond_id, event_type, event_time, video_path)
        VALUES (%s, %s, %s, %s)
    """
    cur.execute(sql, (pond_id, event_type, event_time, video_path))
    conn.commit()

    print(f"[OK] 插入飼料異常：{pond_id} / {event_type} / {event_time}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    pond = "Pond A"          # 想換池就改這裡
    etype = "殘料異常"       # 想換類型可改成 "飼料異常" 或你前端顯示的字
    insert_leftover_event(pond, etype)
    print("✅ 本次程式執行結束（只插入一筆）")