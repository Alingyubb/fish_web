import mysql.connector
import random
from datetime import datetime, timedelta

# 資料庫連線設定（請改成你自己的）
conn = mysql.connector.connect(
    host="localhost",
    user="testuser",
    password="test123",
    database="pond_data"
)
cursor = conn.cursor()

# 假資料參數設定
ponds = ['A', 'B', 'C']
now = datetime.now()

def generate_data():
    for pond in ponds:
        for i in range(24):  # 每池 24 筆（一小時一筆）
            timestamp = now - timedelta(hours=i)
            temperature = round(random.uniform(20.0, 28.0), 2)  # 水溫 20~28°C
            salinity = round(random.uniform(5.0, 35.0), 2)     # 鹽度
            ph = round(random.uniform(6.5, 9.0), 2)            # pH值
            do_level = round(random.uniform(4.0, 10.0), 2)     # 溶氧量

            cursor.execute("""
                INSERT INTO pond_data (pond_name, timestamp, temperature, salinity, ph, do_level)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (pond, timestamp, temperature, salinity, ph, do_level))

# 清空原本資料（如果有需要）
cursor.execute("DELETE FROM pond_data")

# 寫入假資料
generate_data()

# 提交 & 關閉連線
conn.commit()
cursor.close()
conn.close()

print("data input pond_data")
