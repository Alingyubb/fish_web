from flask import Flask, request
from flask_cors import CORS
import paho.mqtt.client as mqtt

app = Flask(__name__)
CORS(app) # 允許網頁跨域傳輸

# === MQTT 設定 (這裡用你之前測試成功的設定) ===
BROKER = "broker.hivemq.com" # 或是你的電腦 IP (如果是 Local 端)
PORT = 1883
TOPIC = "myproject/screen/test1"

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect(BROKER, PORT, 60)
client.loop_start()

print(f"✅ Python 中繼站已啟動！MQTT 連線至: {BROKER}")

# === 設定一個網址讓網頁傳圖片進來 ===
@app.route('/upload', methods=['POST'])
def upload_image():
    try:
        # 1. 收到網頁傳來的原始圖片數據 (Binary)
        img_data = request.data
        
        # 2. 透過 MQTT 轉發出去
        if len(img_data) > 0:
            client.publish(TOPIC, img_data)
            print(f"🚀 已轉發圖片，大小: {len(img_data)} bytes")
            return "Sent", 200
        else:
            return "Empty Data", 400
            
    except Exception as e:
        print(f"❌ 錯誤: {e}")
        return str(e), 500

if __name__ == '__main__':
    # 啟動網頁伺服器在 Port 5000
    app.run(host='0.0.0.0', port=5000)