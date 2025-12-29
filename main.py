import os
import base64
import json
import threading
import time
from collections import deque
import requests
import pyautogui
from io import BytesIO
from flask import Flask, jsonify, request, render_template
import webview
from PIL import Image, ImageChops, ImageStat, ImageDraw

# --- 端口配置 ---
PORT = 5008

app = Flask(__name__)

# --- 配置区 (请确保路径正确) ---
VCP_APPDATA = r"E:\DockerData\VCPChat\AppData"
VCP_URL = "http://localhost:6005/v1/chat/completions"
API_KEY = "Vcp_Secret_9x8d7f6a5s4d"

last_screen_img = None
DIFF_THRESHOLD = 3.0
CHANGE_WINDOW_SEC = 1.2
MIN_CHANGE_INTERVAL_SEC = 0.8
recent_diffs = deque()
last_change_time = 0.0


def load_agent_config(agent_id):
    path = os.path.join(VCP_APPDATA, "Agents", agent_id, "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# --- 截图 & 自身遮罩逻辑 ---
def capture_vision_image():
    try:
        # 1. 全屏截图
        img = pyautogui.screenshot()

        # 2. 自身遮罩 (防止无限套娃)
        try:
            if len(webview.windows) > 0:
                window = webview.windows[0]
                x, y = int(window.x), int(window.y)
                w, h = int(window.width), int(window.height)

                # 绘制纯黑矩形覆盖 HUD 所在位置
                draw = ImageDraw.Draw(img)
                draw.rectangle([x, y, x + w, y + h], fill="black")
        except Exception as e:
            print(f"遮罩绘制忽略: {e}")

        return img
    except Exception as e:
        print(f"截图错误: {e}")
        return None


def check_screen_change(current_img):
    global last_screen_img, last_change_time
    if last_screen_img is None:
        last_screen_img = current_img
        return True
    try:
        img1 = last_screen_img.resize((32, 32)).convert("L")
        img2 = current_img.resize((32, 32)).convert("L")
        diff = ImageChops.difference(img1, img2)
        stat = ImageStat.Stat(diff)
        diff_value = sum(stat.mean) / len(stat.mean)
        now = time.time()
        recent_diffs.append((now, diff_value))
        while recent_diffs and now - recent_diffs[0][0] > CHANGE_WINDOW_SEC:
            recent_diffs.popleft()
        window_avg = sum(v for _, v in recent_diffs) / max(len(recent_diffs), 1)

        if window_avg > DIFF_THRESHOLD and now - last_change_time >= MIN_CHANGE_INTERVAL_SEC:
            last_screen_img = current_img
            last_change_time = now
            return True
        return False
    except:
        return True


# --- 路由定义 ---

@app.route('/')
def index():
    return render_template('hud.html')


# 【本次修复】把这个漏掉的接口加回来了
@app.route('/api/get_agents')
def get_agents():
    agents_dir = os.path.join(VCP_APPDATA, "Agents")
    res_list = []
    if os.path.exists(agents_dir):
        try:
            for fid in os.listdir(agents_dir):
                conf = load_agent_config(fid)
                if conf:
                    res_list.append({"id": fid, "name": conf.get("name", fid)})
        except Exception as e:
            print(f"读取Agent失败: {e}")

    # 如果没找到Agent，返回一个默认的防止前端报错
    if not res_list:
        res_list.append({"id": "default", "name": "Default Agent"})

    return jsonify(res_list)


@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    agent_id = data.get("agent_id")
    user_message = data.get("message", "")
    mode = data.get("mode", "manual")

    config = load_agent_config(agent_id)
    # 如果找不到配置，给个默认空配置防止崩溃
    if not config:
        config = {"systemPrompt": "你是陪玩助手", "model": "gemini-3-flash-preview"}

    # 1. 截图
    current_img = capture_vision_image()
    if not current_img: return jsonify({"reply": "截图失败", "status": "error"})

    # 2. 查重
    if mode == "auto":
        if not check_screen_change(current_img):
            return jsonify({"reply": None, "status": "unchanged"})

    # 3. 转码
    current_img.thumbnail((800, 800))
    buf = BytesIO()
    current_img.save(buf, format="JPEG", quality=70)
    vision_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    messages = []
    system_text = config.get("systemPrompt", "你是一个陪玩助手。")
    if mode == "auto":
        system_text += "\n【模式：自动观察】画面有变化。若无关紧要请回 [SILENCE]。"

    messages.append({"role": "system", "content": system_text})

    user_content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vision_b64}"}}]

    if mode == "auto":
        user_content.append({"type": "text", "text": "画面变了，请判断是否需要评论。"})
    else:
        user_content.append({"type": "text", "text": f"用户: {user_message}"})

    messages.append({"role": "user", "content": user_content})

    payload = {
        "messages": messages,
        "model": config.get("model", "gemini-3-flash-preview"),
        "temperature": 0.7
    }

    try:
        r = requests.post(VCP_URL, headers={"Authorization": f"Bearer {API_KEY}"}, json=payload, timeout=30)
        res = r.json()

        # 增加健壮性检查
        if "choices" in res and len(res["choices"]) > 0:
            reply = res["choices"][0]["message"]["content"].strip()
            if mode == "auto" and "[SILENCE]" in reply:
                return jsonify({"reply": None, "status": "silent"})
            return jsonify({"reply": reply, "status": "ok"})
        else:
            return jsonify({"reply": "后端返回格式异常", "status": "error"})

    except Exception as e:
        print(f"API Error: {e}")
        return jsonify({"reply": str(e), "status": "error"})


@app.route('/api/close')
def close_app():
    os._exit(0)


def start_server():
    app.run(port=PORT, debug=False, threaded=True)


if __name__ == '__main__':
    t = threading.Thread(target=start_server, daemon=True)
    t.start()

    time.sleep(1.5)

    webview.create_window(
        'VCP HUD',
        f'http://127.0.0.1:{PORT}',
        width=750, height=480,
        min_size=(400, 300),
        frameless=True,
        easy_drag=True,
        resizable=True,
        on_top=True,
        transparent=True,
        background_color='#000000'
    )
    webview.start()
