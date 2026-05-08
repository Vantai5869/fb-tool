import os
import json
import threading
import requests as _req
from flask import Flask, jsonify, render_template, request

from core.group_api import FacebookGroupAPI, load_token, load_cookie, refresh_token

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

SEEN_FILE = os.path.join(DATA_DIR, 'seen_posts.json')
TG_CONFIG_FILE = os.path.join(DATA_DIR, 'telegram_config.json')

BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '8724375632:AAEgyz4yRPivDYWGXesTaJHhdqWYIraSoT8')
DEFAULT_GROUP = os.environ.get('DEFAULT_GROUP', '3809441172650624')
PORT = int(os.environ.get('PORT', 5000))

app = Flask(__name__, template_folder='views')

# ── State ──────────────────────────────────────────────
_api_cache: dict = {}
_seen_ids: set = set()
_tg_chat_ids: list = []


def _load_state():
    global _seen_ids, _tg_chat_ids
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        _seen_ids = set(json.load(open(SEEN_FILE)))
    except Exception:
        _seen_ids = set()
    try:
        cfg = json.load(open(TG_CONFIG_FILE))
        _tg_chat_ids = cfg.get('chat_ids') or ([cfg['chat_id']] if cfg.get('chat_id') else ['7129448686'])
    except Exception:
        _tg_chat_ids = ['7129448686']


def _save_seen():
    with open(SEEN_FILE, 'w') as f:
        json.dump(list(_seen_ids), f)


def _save_tg():
    with open(TG_CONFIG_FILE, 'w') as f:
        json.dump({'chat_ids': _tg_chat_ids}, f)


def get_api(group_id: str) -> FacebookGroupAPI:
    if group_id not in _api_cache:
        _api_cache[group_id] = FacebookGroupAPI(group_id)
    return _api_cache[group_id]


# ── Telegram ───────────────────────────────────────────
def _tg_send(chat_id: str, text: str):
    try:
        _req.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown',
                  'disable_web_page_preview': False},
            timeout=10,
        )
    except Exception:
        pass


def _notify_new_post(post: dict):
    if not _tg_chat_ids:
        return
    author = (post.get('from') or {}).get('name', 'Ẩn danh')
    text = post.get('message', '') or ''
    preview = text[:300] + ('...' if len(text) > 300 else '')
    msg = (
        f"🔔 *Bài mới trong nhóm* `{post.get('_group_id', '')}`\n\n"
        f"👤 *{author}*\n{preview}\n\n"
        f"[🔗 Xem bài viết]({post.get('permalink_url', '')})"
    )
    for cid in _tg_chat_ids:
        _tg_send(cid, msg)


def _poll_telegram():
    offset = 0
    while True:
        try:
            r = _req.get(
                f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates',
                params={'offset': offset, 'timeout': 30},
                timeout=35,
            )
            for upd in r.json().get('result', []):
                offset = upd['update_id'] + 1
                msg = upd.get('message', {})
                if msg.get('text', '').startswith('/start'):
                    cid = str(msg['chat']['id'])
                    name = msg['from'].get('first_name', '')
                    _tg_send(cid,
                        f"👋 Xin chào {name}\\!\n\n"
                        f"Chat ID của bạn là:\n`{cid}`\n\n"
                        f"Copy ID này rồi vào web thêm vào mục *Telegram* để nhận thông báo\\."
                    )
        except Exception:
            pass


# ── Routes ─────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/posts')
def api_posts():
    global _seen_ids
    limit = request.args.get('limit', 10, type=int)
    group_ids = [g.strip() for g in request.args.get('groups', DEFAULT_GROUP).split(',') if g.strip()]
    is_first = len(_seen_ids) == 0

    try:
        all_posts = []
        for gid in group_ids:
            posts = get_api(gid).get_posts(limit)
            if posts is None:
                return jsonify({'error': 'Cookie hết hạn — cập nhật data/cookie.txt rồi restart'}), 401
            for p in posts:
                p['_group_id'] = gid
            all_posts.extend(posts)

        all_posts.sort(key=lambda x: x.get('created_time', ''), reverse=True)

        new_ids = set()
        for post in all_posts:
            pid = post.get('id')
            if pid and pid not in _seen_ids:
                new_ids.add(pid)
                if not is_first:
                    threading.Thread(target=_notify_new_post, args=(post,), daemon=True).start()

        if new_ids:
            _seen_ids.update(new_ids)
            _save_seen()

        return jsonify(all_posts)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/comment', methods=['POST'])
def api_comment():
    body = request.get_json() or {}
    post_id = body.get('post_id', '').strip()
    message = body.get('message', '').strip()
    group_id = body.get('group_id', DEFAULT_GROUP)
    if not post_id or not message:
        return jsonify({'ok': False, 'error': 'Thiếu post_id hoặc message'}), 400
    try:
        result = get_api(group_id).post_comment(post_id, message)
        if result and 'id' in result:
            return jsonify({'ok': True, 'comment_id': result['id']})
        err = (result or {}).get('error', {}).get('message', 'Lỗi không xác định')
        return jsonify({'ok': False, 'error': err})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/groups/resolve')
def api_resolve_group():
    slug = request.args.get('slug', '').strip()
    if not slug:
        return jsonify({'ok': False, 'error': 'Thiếu slug'}), 400
    if slug.isdigit():
        return jsonify({'ok': True, 'id': slug})
    try:
        data = get_api(DEFAULT_GROUP).resolve_slug(slug)
        if data and 'id' in data:
            return jsonify({'ok': True, 'id': data['id'], 'name': data.get('name', '')})
        err = (data or {}).get('error', {}).get('message', 'Không tìm thấy group')
        return jsonify({'ok': False, 'error': err})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/telegram/chatids', methods=['GET'])
def tg_get():
    return jsonify(_tg_chat_ids)


@app.route('/api/telegram/chatids', methods=['POST'])
def tg_add():
    cid = (request.get_json() or {}).get('chat_id', '').strip()
    if not cid:
        return jsonify({'ok': False, 'error': 'Thiếu chat_id'}), 400
    if cid not in _tg_chat_ids:
        _tg_chat_ids.append(cid)
        _save_tg()
    return jsonify({'ok': True, 'chat_ids': _tg_chat_ids})


@app.route('/api/telegram/chatids/<chat_id>', methods=['DELETE'])
def tg_remove(chat_id):
    if chat_id in _tg_chat_ids:
        _tg_chat_ids.remove(chat_id)
        _save_tg()
    return jsonify({'ok': True, 'chat_ids': _tg_chat_ids})


@app.route('/api/telegram/test/<chat_id>', methods=['POST'])
def tg_test(chat_id):
    try:
        r = _req.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': chat_id, 'text': '✅ Kết nối Telegram thành công!'},
            timeout=10,
        )
        return jsonify({'ok': r.ok})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── Start ──────────────────────────────────────────────
_load_state()
threading.Thread(target=_poll_telegram, daemon=True).start()

if __name__ == '__main__':
    print(f'🚀 Server đang chạy tại http://localhost:{PORT}')
    app.run(debug=False, port=PORT)
