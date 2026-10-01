import mysql.connector
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from sklearn.neural_network import MLPRegressor
from sklearn.utils import resample
import datetime
import time
import json

# ======================================
#             系統參數
# ======================================
FEATURES_TO_USE = ['orp', 'temperature', 'ph', 'do_level']
N_ESTIMATORS = 100
CONFIDENCE_LEVEL = 95
LOOK_BACK = 100

# MySQL 設定
db_config = {
    'host': 'localhost',
    'user': 'testuser',
    'password': 'test123',
    'database': 'ponddb_new'
}

# JSON 檔命名模板（會變成 prediction_results_Pond_A.json 這種）
JSON_NAME_TEMPLATE = "prediction_results_{pond}.json"


# ======================================
#       函數：抓資料庫最新資料（全部魚池）
# ======================================
def load_latest_data():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()

        feature_string = ", ".join(FEATURES_TO_USE)
        query = f"""
            SELECT timestamp, {feature_string}, HOUR(timestamp), pond_id
            FROM pond_data
            WHERE pond_id IN ('Pond A','Pond B','Pond C')
            ORDER BY timestamp DESC
            LIMIT {2000 + LOOK_BACK}
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        rows.reverse()  # 反轉成由舊到新
        return rows
    except Exception as e:
        print("資料庫讀取錯誤:", e)
        return None
    finally:
        try:
            cursor.close()
            conn.close()
        except:
            pass


# ======================================
#   函數：訓練「單一魚池」的兩個系統 (ORP / PERIODIC)
# ======================================
def train_single_pond(df_pond):
    """
    df_pond: 單一魚池的 DataFrame，欄位至少包含：
             ['timestamp','orp','temperature','ph','do_level','hour','pond_id']
    """

    # 清洗 NaN / 空字串
    df = df_pond.copy()
    df.replace('', np.nan, inplace=True)
    df.dropna(inplace=True)

    if len(df) < LOOK_BACK + 10:
        print(f"❌ {df['pond_id'].iloc[0]} 可用資料不足（{len(df)} 筆），無法訓練模型")
        return None

    # 確保時間由舊到新排序
    df.sort_values("timestamp", inplace=True)

    # 轉 numpy (乾淨資料)
    arr = df[["orp", "temperature", "ph", "do_level", "hour"]].astype(float).values

    # -------------------------
    # ORP 系統（無 hour）
    # -------------------------
    train_no_hour = arr[:, 0:4]  # orp, temp, ph, do
    scaler_no_hour = MinMaxScaler()

    # 防止某一欄全部相同導致除以 0 → 加一點點 noise
    for col in range(train_no_hour.shape[1]):
        if np.all(train_no_hour[:, col] == train_no_hour[0, col]):
            train_no_hour[:, col] += np.random.normal(0, 0.0001, size=len(train_no_hour))

    scaled_no_hour = scaler_no_hour.fit_transform(train_no_hour)

    # 建立序列
    X_orp, y_orp = [], []
    for i in range(len(scaled_no_hour) - LOOK_BACK):
        seq = scaled_no_hour[i:i + LOOK_BACK]
        target = scaled_no_hour[i + LOOK_BACK]
        if (np.isnan(seq).any()) or (np.isnan(target).any()):
            continue
        X_orp.append(seq.flatten())
        y_orp.append(target)

    X_orp, y_orp = np.array(X_orp), np.array(y_orp)
    if len(X_orp) == 0:
        print(f"❌ {df['pond_id'].iloc[0]} 的 ORP 訓練資料序列為 0 筆")
        return None

    # 訓練 ORP 模型 (Bagging)
    models_orp = []
    for i in range(N_ESTIMATORS):
        Xs, ys = resample(X_orp, y_orp, random_state=i)
        m = MLPRegressor(hidden_layer_sizes=(128, 64), max_iter=500)
        m.fit(Xs, ys)
        models_orp.append(m)

    # -------------------------
    # PERIODIC 系統（含 hour）
    # -------------------------
    train_with_hour = arr  # [orp, temp, ph, do, hour(0~23)]
    scaler_periodic = MinMaxScaler()

    for col in range(train_with_hour.shape[1]):
        if np.all(train_with_hour[:, col] == train_with_hour[0, col]):
            train_with_hour[:, col] += np.random.normal(0, 0.0001, size=len(train_with_hour))

    scaled_with_hour = scaler_periodic.fit_transform(train_with_hour)

    Xp, yp = [], []
    for i in range(len(scaled_with_hour) - LOOK_BACK):
        seq = scaled_with_hour[i:i + LOOK_BACK]
        tgt = scaled_with_hour[i + LOOK_BACK][:4]  # output 不含 hour
        if (np.isnan(seq).any()) or (np.isnan(tgt).any()):
            continue
        Xp.append(seq.flatten())
        yp.append(tgt)

    Xp, yp = np.array(Xp), np.array(yp)
    if len(Xp) == 0:
        print(f"❌ {df['pond_id'].iloc[0]} 的 PERIODIC 訓練資料序列為 0 筆")
        return None

    models_periodic = []
    for i in range(N_ESTIMATORS):
        Xs, ys = resample(Xp, yp, random_state=i)
        m = MLPRegressor(hidden_layer_sizes=(128, 64), max_iter=500)
        m.fit(Xs, ys)
        models_periodic.append(m)

    return {
        "orp": (models_orp, scaler_no_hour, scaled_no_hour),
        "periodic": (models_periodic, scaler_periodic, scaled_with_hour)
    }


# ======================================
#   函數：對「單一魚池」預測未來 1 小時
# ======================================
def predict_next_hour_for_pond(models, start_time=None):
    if start_time is None:
        start_time = datetime.datetime.now()

    models_orp, scaler_no_hour, scaled_orp = models["orp"]
    models_periodic, scaler_periodic, scaled_per = models["periodic"]

    future_steps = 360  # 360 * 10 秒 = 1 小時

    # ---- ORP ----
    last_orp = scaled_orp[-LOOK_BACK:].copy()
    pred_orp = []
    for _ in range(future_steps):
        x = last_orp.flatten().reshape(1, -1)
        preds = np.array([m.predict(x)[0] for m in models_orp])
        mean = preds.mean(axis=0)
        pred_orp.append(preds)
        last_orp = np.vstack([last_orp[1:], mean])

    # ---- PERIODIC ----
    last_per = scaled_per[-LOOK_BACK:].copy()
    pred_per = []

    for step in range(future_steps):
        x = last_per.flatten().reshape(1, -1)
        preds = np.array([m.predict(x)[0] for m in models_periodic])
        mean = preds.mean(axis=0)
        pred_per.append(preds)

        # hour 處理：用真實時間推進
        future_time = start_time + datetime.timedelta(seconds=10 * (step + 1))
        next_hour = future_time.hour

        dummy = np.zeros(5)
        dummy[-1] = next_hour
        scaled_hour = scaler_periodic.transform(dummy.reshape(1, -1))[0][-1]

        new_step = np.append(mean, scaled_hour).reshape(1, -1)
        last_per = np.vstack([last_per[1:], new_step])

    # 信賴區間 + 反標準化
    pred_orp = np.array(pred_orp)   # shape: [steps, N_EST, 4]
    pred_per = np.array(pred_per)   # shape: [steps, N_EST, 4]

    mean_orp = scaler_no_hour.inverse_transform(pred_orp.mean(axis=1))
    lower_orp = scaler_no_hour.inverse_transform(np.percentile(pred_orp, 2.5, axis=1))
    upper_orp = scaler_no_hour.inverse_transform(np.percentile(pred_orp, 97.5, axis=1))

    mean_per = scaler_periodic.inverse_transform(
        np.concatenate([pred_per.mean(axis=1), np.zeros((future_steps, 1))], axis=1)
    )[:, :4]

    lower_per = scaler_periodic.inverse_transform(
        np.concatenate([np.percentile(pred_per, 2.5, axis=1), np.zeros((future_steps, 1))], axis=1)
    )[:, :4]

    upper_per = scaler_periodic.inverse_transform(
        np.concatenate([np.percentile(pred_per, 97.5, axis=1), np.zeros((future_steps, 1))], axis=1)
    )[:, :4]

    # 合併：ORP 系統輸出 orp，PERIODIC 輸出 temp/ph/do
    final_pred = []
    for i in range(future_steps):
        out = mean_orp[i].copy()       # [orp, temp, ph, do] 但這裡的 temp/ph/do 會被覆蓋
        out[1:] = mean_per[i][1:]      # TEMP, PH, DO 使用 periodic 的
        final_pred.append(out)

    return np.array(final_pred), lower_orp, upper_orp, mean_per, lower_per, upper_per


# ======================================
#   函數：輸出 JSON (每 10 分鐘一筆) 並回傳 list
# ======================================
def export_json(pond_id, final_pred, lower_orp, upper_orp, mean_per, lower_per, upper_per):

    now = datetime.datetime.now()
    data_list = []

    # 每 10 分鐘輸出一筆
    for minutes in range(10, 61, 10):
        idx = minutes * 6 - 1
        if idx >= len(final_pred):
            break

        t = (now + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

        data_list.append({
            "timestamp": t,
            "temperature": round(float(final_pred[idx][1]), 2),
            "salinity": 0.0,  # 固定值
            "ph": round(float(final_pred[idx][2]), 2),
            "do_level": round(float(final_pred[idx][3]), 2),
            "orp": round(float(final_pred[idx][0]), 2),
            "pond_id": pond_id
        })

    # JSON 檔名：prediction_results_Pond_A.json
    safe_pond = pond_id.replace(" ", "_")
    filename = f"prediction_results_{safe_pond}.json"

    with open(filename, "w", encoding="utf8") as f:
        json.dump(data_list, f, ensure_ascii=False, indent=4)

    print(f"✅ {pond_id} 已輸出 {len(data_list)} 筆預測到 {filename}")

    return data_list



    # 針對這個 pond 輸出獨立 JSON 檔
    safe_pond = pond_id.replace(" ", "_")
    filename = JSON_NAME_TEMPLATE.format(pond=safe_pond)
    with open(filename, "w", encoding="utf8") as f:
        json.dump(data_list, f, ensure_ascii=False, indent=4)

    print(f"✅ {pond_id} 已輸出 {len(data_list)} 筆預測到 {filename}")

    return data_list


# ======================================
#   函數：將預測結果寫入 MySQL
# ======================================
def save_predictions_to_mysql(pond_id, data_list):
    if not data_list:
        print(f"⚠ {pond_id} 沒有可寫入的預測資料")
        return

    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()

        query = """
            INSERT INTO prediction_results (
                timestamp,
                temperature, salinity, ph, do_level, orp,
                pond_id
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """

        for row in data_list:
            cursor.execute(query, (
                row["timestamp"],
                row["temperature"],
                row["salinity"],
                row["ph"],
                row["do_level"],
                row["orp"],
                row["pond_id"]
            ))

        conn.commit()
        print(f"💾 {pond_id} 已成功寫入 {len(data_list)} 筆預測到 MySQL")

    except Exception as e:
        print("❌ MySQL 寫入失敗：", e)

    finally:
        try:
            cursor.close()
            conn.close()
        except:
            pass




# ======================================
#       主系統迴圈：持續預測（三個魚池）
# ======================================
print("\n🔥 系統啟動 — 進入持續預測模式，每 10 分鐘更新一次（分魚池）\n")

while True:
    print("\n============================================")
    print("⏳ 正在讀取資料庫最新資料...")
    rows = load_latest_data()

    if not rows:
        print("❌ 無法取得資料，10 分鐘後重試")
        time.sleep(600)
        continue

    # 先轉成 DataFrame，再依 pond_id 分組
    df_all = pd.DataFrame(
        rows,
        columns=["timestamp", "orp", "temperature", "ph", "do_level", "hour", "pond_id"]
    )

    pond_ids = sorted(df_all["pond_id"].unique())
    print(f"📌 本輪偵測到的魚池：{pond_ids}")

    # 記錄同一輪預測的起始時間，讓各池時間軸一致
    round_start_time = datetime.datetime.now()

    for pond_id in pond_ids:
        print(f"\n====== 🐟 處理 {pond_id} ======")
        df_pond = df_all[df_all["pond_id"] == pond_id].copy()

        if len(df_pond) < LOOK_BACK + 10:
            print(f"❌ {pond_id} 資料不足（{len(df_pond)} 筆），略過本輪")
            continue

        print(f"📘 正在訓練模型：{pond_id} ...")
        models = train_single_pond(df_pond)
        if not models:
            print(f"❌ {pond_id} 訓練失敗，略過本輪")
            continue

        print(f"🔮 {pond_id} 正在預測未來 1 小時數據...")
        final_pred, lower_orp, upper_orp, mean_per, lower_per, upper_per = \
            predict_next_hour_for_pond(models, start_time=round_start_time)

        print(f"📝 {pond_id} 正在輸出 JSON...")
        data_list = export_json(pond_id, final_pred, lower_orp, upper_orp, mean_per, lower_per, upper_per)

        print(f"💾 {pond_id} 正在寫入 MySQL...")
        save_predictions_to_mysql(pond_id, data_list)

    print("\n✔ 本輪全部魚池處理完成！10 分鐘後進行下一輪預測\n")
    time.sleep(600)   # 10 分鐘
