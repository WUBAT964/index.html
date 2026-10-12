# -*- coding: utf-8 -*-
"""
月度好菜投票系统 - 后端服务
- 按月份设置投票截止时间
- 完整投票日志（用菜 ID + 时间戳）
- 统计接口 /api/stats（含设备/浏览器/登录/投票记录）
"""
import os
import sys
import json
import base64
import time
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

GH_TOKEN = os.environ.get("GH_VOTING_TOKEN")
REPO = "WUBAT964/dish-votes"
FILE = "votes.json"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(BASE_DIR, "images")
DISHES_FILE = os.path.join(BASE_DIR, "dishes.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
LOCAL_VOTES_FILE = os.path.join(BASE_DIR, "votes.json")
CURRENT_MONTH_FILE = os.path.join(BASE_DIR, "current_month.txt")

SYNC_INTERVAL = 30
MAX_VOTES_PER_USER = 3
MAX_VOTE_LOG = 50  # 每人最多保留 50 条投票记录

CST = timezone(timedelta(hours=8))

VOTE_DEADLINES = {
    "2026-09": datetime(2026, 10, 12, 0, 0, 0, tzinfo=CST),
    # "2026-10": datetime(2026, 11, 12, 0, 0, 0, tzinfo=CST),
}

_dish_month_cache = {}
_dish_name_cache = {}
_dish_cache_lock = threading.Lock()


def load_dish_month_cache():
    global _dish_month_cache, _dish_name_cache
    try:
        with open(DISHES_FILE, 'r', encoding='utf-8') as f:
            dishes = json.load(f)
        month_cache = {}
        name_cache = {}
        for d in dishes:
            did = str(d.get('id', ''))
            date = d.get('date', '')
            name = d.get('name', '')
            if did:
                if len(date) >= 7:
                    month_cache[did] = date[:7]
                if name:
                    name_cache[did] = name
        with _dish_cache_lock:
            _dish_month_cache = month_cache
            _dish_name_cache = name_cache
        print(f"✅ 已缓存 {len(month_cache)} 道菜的月份信息")
    except Exception as e:
        print(f"⚠️ 加载 dishes.json 失败: {e}")


def get_dish_month(dish_id):
    with _dish_cache_lock:
        return _dish_month_cache.get(str(dish_id))


def get_dish_name(dish_id):
    with _dish_cache_lock:
        return _dish_name_cache.get(str(dish_id), f"#{dish_id}")


def get_deadline_for_month(month):
    return VOTE_DEADLINES.get(month)


def is_month_vote_open(month):
    if not month:
        return True
    dl = get_deadline_for_month(month)
    if dl is None:
        return True
    return datetime.now(CST) < dl


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ==================== 🖥️ 设备 & 浏览器识别 ====================
def parse_user_agent(ua):
    if not ua:
        return '未知'
    ua_l = ua.lower()

    if 'windows nt 10' in ua_l:
        os_name = 'Windows 10/11'
    elif 'windows nt 6.3' in ua_l:
        os_name = 'Windows 8.1'
    elif 'windows nt 6.1' in ua_l:
        os_name = 'Windows 7'
    elif 'windows' in ua_l:
        os_name = 'Windows'
    elif 'iphone' in ua_l:
        os_name = 'iPhone'
    elif 'ipad' in ua_l:
        os_name = 'iPad'
    elif 'mac os x' in ua_l or 'macintosh' in ua_l:
        os_name = 'macOS'
    elif 'android' in ua_l:
        os_name = 'Android'
    elif 'linux' in ua_l:
        os_name = 'Linux'
    else:
        os_name = '未知'

    if 'micromessenger' in ua_l:
        browser = '微信'
    elif 'qqbrowser' in ua_l:
        browser = 'QQ浏览器'
    elif 'ucbrowser' in ua_l or ' ucweb' in ua_l:
        browser = 'UC浏览器'
    elif 'baidubrowser' in ua_l:
        browser = '百度浏览器'
    elif 'edg/' in ua_l or 'edga/' in ua_l or 'edgios/' in ua_l:
        browser = 'Edge'
    elif 'opr/' in ua_l or 'opera' in ua_l:
        browser = 'Opera'
    elif 'firefox' in ua_l:
        browser = 'Firefox'
    elif 'chrome' in ua_l or 'crios' in ua_l:
        browser = 'Chrome'
    elif 'safari' in ua_l:
        browser = 'Safari'
    else:
        browser = '未知'

    return f"{os_name} · {browser}"


def get_client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    xri = request.headers.get('X-Real-IP', '')
    if xri:
        return xri
    return request.remote_addr or '未知'


def record_device(phone, device_id, ua):
    user = users_data.get(phone)
    if not user:
        return
    devices = user.setdefault('devices', [])
    ua_parsed = parse_user_agent(ua)
    ip = get_client_ip()
    now = datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')

    for d in devices:
        if d.get('id') == device_id:
            d['last_login'] = now
            d['last_ip'] = ip
            d['ua'] = ua
            d['ua_parsed'] = ua_parsed
            d['login_count'] = d.get('login_count', 1) + 1
            return
    devices.append({
        "id": device_id or '未知',
        "ua": ua,
        "ua_parsed": ua_parsed,
        "first_login": now,
        "last_login": now,
        "last_ip": ip,
        "login_count": 1
    })


# ===========================================================

vote_cache = {}
users_data = {}
cache_lock = threading.Lock()
github_write_lock = threading.Lock()

print("=" * 60)
print(f"🚀 投票后端启动中... {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
if VOTE_DEADLINES:
    for m, dl in VOTE_DEADLINES.items():
        status = "已结束" if datetime.now(CST) >= dl else "进行中"
        print(f"⏰ {m} 投票截止: {dl.strftime('%Y-%m-%d %H:%M:%S')} ({status})")
print("=" * 60)


def load_users():
    global users_data
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                users_data = json.load(f)
            print(f"✅ 已加载 {len(users_data)} 位注册用户")
        except Exception as e:
            print(f"⚠️ 加载 users.json 失败: {e}")
            users_data = {}
    else:
        print("ℹ️ 未发现 users.json，将从零开始")
        users_data = {}


def save_users():
    try:
        tmp_file = USERS_FILE + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, USERS_FILE)
    except Exception as e:
        print(f"⚠️ 保存 users.json 失败: {e}")


def save_local_votes():
    try:
        tmp_file = LOCAL_VOTES_FILE + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(vote_cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, LOCAL_VOTES_FILE)
    except Exception as e:
        print(f"⚠️ 保存 votes.json 失败: {e}")


def fetch_votes_from_github():
    if not GH_TOKEN:
        return {}
    url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()
        return json.loads(base64.b64decode(data['content']).decode('utf-8'))
    except Exception as e:
        print(f"⚠️ 从 GitHub 拉取票数失败: {e}")
        return {}


def push_votes_to_github():
    if not GH_TOKEN:
        return
    with github_write_lock:
        try:
            url = f"https://api.github.com/repos/{REPO}/contents/{FILE}"
            headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
            resp = requests.get(url, headers=headers, timeout=15)
            sha = resp.json().get('sha') if resp.status_code == 200 else None
            with cache_lock:
                snapshot = vote_cache.copy()
            content = base64.b64encode(json.dumps(snapshot, indent=2, ensure_ascii=False).encode('utf-8')).decode('utf-8')
            payload = {"message": f"auto vote sync at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "content": content}
            if sha:
                payload["sha"] = sha
            put_resp = requests.put(url, headers=headers, json=payload, timeout=15)
            if put_resp.status_code in (200, 201):
                print(f"[同步] ✅ {datetime.now().strftime('%H:%M:%S')} 票数已同步至 GitHub")
        except Exception as e:
            print(f"[同步] ⚠️ 推送异常: {e}")


def background_sync_loop():
    while True:
        time.sleep(SYNC_INTERVAL)
        with cache_lock:
            has_votes = len(vote_cache) > 0
        if has_votes:
            push_votes_to_github()


def init_data():
    global vote_cache
    load_users()
    load_dish_month_cache()
    if os.path.exists(LOCAL_VOTES_FILE):
        try:
            with open(LOCAL_VOTES_FILE, 'r', encoding='utf-8') as f:
                vote_cache = json.load(f)
            print(f"✅ 已从本地加载票数: {vote_cache}")
        except Exception as e:
            print(f"⚠️ 本地票数读取失败: {e}")
            vote_cache = fetch_votes_from_github()
    else:
        vote_cache = fetch_votes_from_github()
        print(f"✅ 已从 GitHub 同步初始票数: {vote_cache}")


# ==================== API 接口 ====================
@app.route('/api/votes', methods=['GET'])
def api_get_votes():
    with cache_lock:
        return jsonify(vote_cache)


@app.route('/api/dishes', methods=['GET'])
def api_get_dishes():
    try:
        with open(DISHES_FILE, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    except Exception as e:
        print(f"⚠️ 读取 dishes.json 失败: {e}")
        return jsonify([])


@app.route('/api/current_month', methods=['GET'])
def api_current_month():
    try:
        if os.path.exists(CURRENT_MONTH_FILE):
            with open(CURRENT_MONTH_FILE, 'r', encoding='utf-8') as f:
                month = f.read().strip()
            print(f"[月份配置] 当前显示: {month if month else '全部（自动）'}")
            return jsonify({"month": month, "configured": True})
    except Exception as e:
        print(f"⚠️ 读取 current_month.txt 失败: {e}")
    return jsonify({"month": "", "configured": False})


@app.route('/api/vote_status', methods=['GET'])
def api_vote_status():
    month = request.args.get('month', '').strip()
    now = datetime.now(CST)

    if month:
        dl = get_deadline_for_month(month)
        return jsonify({
            "month": month,
            "open": is_month_vote_open(month),
            "deadline": dl.isoformat() if dl else None,
            "now": now.isoformat(),
            "server_time": now.strftime('%Y-%m-%d %H:%M:%S')
        })
    else:
        all_status = {}
        for m, dl in VOTE_DEADLINES.items():
            all_status[m] = {
                "open": datetime.now(CST) < dl,
                "deadline": dl.isoformat()
            }
        return jsonify({
            "deadlines": all_status,
            "now": now.isoformat(),
            "server_time": now.strftime('%Y-%m-%d %H:%M:%S')
        })


@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    phone = (data.get('phone') or '').strip()
    device_id = (data.get('device_id') or '').strip()
    ua = request.headers.get('User-Agent', '')

    if not phone:
        return jsonify({"error": "手机号不能为空"}), 400
    if not name:
        return jsonify({"error": "姓名不能为空"}), 400

    with cache_lock:
        if phone in users_data:
            existing = users_data[phone]
            if existing['name'] != name:
                return jsonify({"error": "该手机号已被其他姓名使用！"}), 400
            record_device(phone, device_id, ua)
            save_users()
            log(f"[注册] 🔑 老用户登录 {existing['name']}({phone}) · {parse_user_agent(ua)} · {get_client_ip()}")
            return jsonify({
                "success": True,
                "name": existing['name'],
                "phone": phone,
                "votedDishes": existing.get('votedDishes', {})
            })

        for p, u in users_data.items():
            if u['name'] == name:
                return jsonify({"error": "该姓名已被其他手机号注册！"}), 400

        users_data[phone] = {
            "name": name,
            "votedDishes": {},
            "devices": [],
            "vote_log": []
        }
        record_device(phone, device_id, ua)
        save_users()
        log(f"[注册] 👤 新用户注册 {name}({phone}) · {parse_user_agent(ua)} · {get_client_ip()}，共 {len(users_data)} 位用户")
        return jsonify({
            "success": True,
            "name": name,
            "phone": phone,
            "votedDishes": {}
        })


@app.route('/api/vote', methods=['POST'])
def api_vote():
    data = request.get_json(silent=True) or {}
    dish_id = str(data.get('dish_id') or '').strip()
    action = data.get('action', 'add')
    phone = (data.get('phone') or '').strip()

    if not dish_id or not phone:
        log(f"[投票] ❌ 参数缺失 dish_id={dish_id!r} phone={phone!r}")
        return jsonify({"error": "参数缺失"}), 400

    month = get_dish_month(dish_id)
    if month and not is_month_vote_open(month):
        user_name = users_data.get(phone, {}).get('name', '未注册')
        log(f"[投票] 🚫 拒绝 {user_name}({phone}) 给 {month} #{dish_id} 投票 —— 该月已结束")
        return jsonify({"error": f"{month} 投票已结束！"}), 403

    with cache_lock:
        if phone not in users_data:
            log(f"[投票] ❌ 未注册用户 {phone} 尝试投票 #{dish_id}")
            return jsonify({"error": "用户未注册"}), 403

        user = users_data[phone]
        user_name = user.get('name', phone)
        voted_dishes = user.setdefault('votedDishes', {})
        vote_log = user.setdefault('vote_log', [])
        total_voted = sum(voted_dishes.values())

        now_str = datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')

        if action == 'add':
            if total_voted >= MAX_VOTES_PER_USER:
                log(f"[投票] ⚠️ {user_name}({phone}) 已达 {MAX_VOTES_PER_USER} 票上限，拒绝加票 #{dish_id}")
                return jsonify({"error": f"每人最多只能投 {MAX_VOTES_PER_USER} 票"}), 400
            voted_dishes[dish_id] = voted_dishes.get(dish_id, 0) + 1
            vote_cache[dish_id] = vote_cache.get(dish_id, 0) + 1

            # ✅ 记录投票时间
            vote_log.append({
                "dish": dish_id,
                "action": "add",
                "time": now_str,
                "month": month or ''
            })
            if len(vote_log) > MAX_VOTE_LOG:
                vote_log[:] = vote_log[-MAX_VOTE_LOG:]

            log(f"[投票] ➕ {user_name}({phone}) 投#{dish_id} 1票 "
                f"| 该菜共 {vote_cache.get(dish_id, 0)} 票 "
                f"| 该用户 {sum(voted_dishes.values())}/{MAX_VOTES_PER_USER}")

        elif action == 'remove':
            if voted_dishes.get(dish_id, 0) <= 0:
                log(f"[投票] ⚠️ {user_name}({phone}) 尝试取消未投过的 #{dish_id}")
                return jsonify({"error": "您还没有给这道菜投过票"}), 400
            voted_dishes[dish_id] -= 1
            if voted_dishes[dish_id] <= 0:
                del voted_dishes[dish_id]
            vote_cache[dish_id] = max(0, vote_cache.get(dish_id, 0) - 1)

            # ✅ 记录取消时间
            vote_log.append({
                "dish": dish_id,
                "action": "remove",
                "time": now_str,
                "month": month or ''
            })
            if len(vote_log) > MAX_VOTE_LOG:
                vote_log[:] = vote_log[-MAX_VOTE_LOG:]

            log(f"[投票] ➖ {user_name}({phone}) 取消#{dish_id} 1票 "
                f"| 该菜共 {vote_cache.get(dish_id, 0)} 票 "
                f"| 该用户 {sum(voted_dishes.values())}/{MAX_VOTES_PER_USER}")

        else:
            log(f"[投票] ❌ 未知操作 action={action!r}")
            return jsonify({"error": "未知的操作类型"}), 400

        save_users()
        save_local_votes()

        return jsonify({
            "success": True,
            "count": vote_cache.get(dish_id, 0),
            "myVotes": voted_dishes,
            "totalVoted": sum(voted_dishes.values())
        })


@app.route('/api/user_votes', methods=['GET'])
def api_user_votes():
    phone = request.args.get('phone', '').strip()
    with cache_lock:
        if phone in users_data:
            return jsonify({
                "success": True,
                "name": users_data[phone]['name'],
                "votedDishes": users_data[phone].get('votedDishes', {})
            })
        return jsonify({"success": False, "error": "用户不存在"})


@app.route('/api/stats', methods=['GET'])
def api_stats():
    """统计接口：所有用户的投票情况（含设备/登录/投票记录）"""
    with cache_lock:
        users_list = []
        total_votes = 0
        voted_count = 0
        for phone, u in users_data.items():
            votes = u.get('votedDishes', {})
            t = sum(votes.values())
            total_votes += t
            if t > 0:
                voted_count += 1
            users_list.append({
                "name": u.get('name', ''),
                "phone": phone,
                "votes": votes,
                "total": t,
                "devices": u.get('devices', []),
                "vote_log": u.get('vote_log', [])
            })

        voted = [u for u in users_list if u['total'] > 0]
        not_voted = [u for u in users_list if u['total'] == 0]
        voted.sort(key=lambda x: -x['total'])
        users_list = voted + not_voted

        return jsonify({
            "users": users_list,
            "totalUsers": len(users_list),
            "votedUsers": voted_count,
            "totalVotes": total_votes
        })


@app.route('/api/stats/dishes', methods=['GET'])
def api_stats_dishes():
    try:
        with open(DISHES_FILE, 'r', encoding='utf-8') as f:
            dishes = json.load(f)
    except Exception:
        dishes = []
    with cache_lock:
        result = []
        for d in dishes:
            did = str(d.get('id', ''))
            result.append({
                "dish_id": did,
                "dish_name": d.get('name', ''),
                "date": d.get('date', ''),
                "month": (d.get('date') or '')[:7],
                "votes": vote_cache.get(did, 0)
            })
    result.sort(key=lambda x: -x['votes'])
    return jsonify(result)


@app.route('/api/health', methods=['GET'])
def api_health():
    with cache_lock:
        return jsonify({
            "status": "ok",
            "users": len(users_data),
            "votes": vote_cache,
            "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "deadlines": {m: dl.isoformat() for m, dl in VOTE_DEADLINES.items()}
        })


@app.route('/images/<path:filename>')
def serve_image(filename):
    response = send_from_directory(IMAGE_DIR, filename)
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


init_data()
threading.Thread(target=background_sync_loop, daemon=True).start()
print("🔄 后台同步线程已启动（每 30 秒）")

if __name__ == '__main__':
    print("=" * 60)
    print("🎉 服务已就绪，监听 http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host='127.0.0.1', port=5000, threaded=True, debug=False)