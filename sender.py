import time
import io
import pyautogui
import paho.mqtt.client as mqtt

# === 設定區 ===
BROKER = "broker.hivemq.com"
PORT = 1883
TOPIC = "myproject/screen/test1" # 記得確認跟 Unity 一樣
# =============

# 【修正這裡】v2 版本必須多加一個 properties 參數
def on_connect(client, userdata, flags, rc, properties):
    if rc == 0:
        print("已連線到 MQTT Broker! 成功！")
    else:
        print(f"連線失敗，代碼: {rc}")

# 指定使用新版 API
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.connect(BROKER, PORT, 60)

client.loop_start()

print("開始傳送截圖... (按 Ctrl+C 停止)")

try:
    while True:
        screenshot = pyautogui.screenshot()
        
        # 縮放圖片 (寬度 800)
        width, height = screenshot.size
        new_width = 800 
        new_height = int(height * (new_width / width))
        screenshot = screenshot.resize((new_width, new_height))

        img_byte_arr = io.BytesIO()
        # JPG 品質 30
        screenshot.save(img_byte_arr, format='JPEG', quality=30) 
        img_bytes = img_byte_arr.getvalue()

        if len(img_bytes) < 128000:
            client.publish(TOPIC, img_bytes)
            print(f"已發送: {len(img_bytes)} bytes")
        else:
            print("圖片太大，跳過")

        # 稍微加速一點，每 0.2 秒傳一張 (FPS 約 5)
        time.sleep(0.2)

except KeyboardInterrupt:
    print("程式停止")
    client.loop_stop()
    client.disconnect()