import os
import json
import threading
import requests as _req
from flask import Flask, jsonify, render_template, request

from core.group_api import FacebookGroupAPI, load_token, load_cookie, refresh_token
from core.ai_classifier import AIClassifier, DEFAULT_MODEL, DEFAULT_API_KEY, DEFAULT_CATEGORIES, PROVIDERS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

SEEN_FILE = os.path.join(DATA_DIR, 'seen_posts.json')
TG_CONFIG_FILE = os.path.join(DATA_DIR, 'telegram_config.json')
GROUPS_FILE = os.path.join(DATA_DIR, 'groups.json')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
AI_CONFIG_FILE = os.path.join(DATA_DIR, 'ai_config.json')
CLASSIFICATIONS_FILE = os.path.join(DATA_DIR, 'classifications.json')
LEADS_FILE = os.path.join(DATA_DIR, 'leads.json')

BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '8724375632:AAEgyz4yRPivDYWGXesTaJHhdqWYIraSoT8')
DEFAULT_GROUP = os.environ.get('DEFAULT_GROUP', '3809441172650624')
PORT = int(os.environ.get('PORT', 5000))

app = Flask(__name__, template_folder='views')

# ── State ──────────────────────────────────────────────
_api_cache: dict = {}
_seen_ids: set = set()
_tg_chat_ids: list = []
_pages_cache: dict = {}  # {page_id: {name, access_token}}
_groups: list = []       # [{id, name}]
_settings: dict = {}    # {auto_refresh, interval}
_ai_config: dict = {}   # {provider, model, keys, auto_classify, categories}
_classifications: dict = {}  # {post_id: category}
_leads: dict = {}       # {post_id: [lead]}


def _load_state():
    global _seen_ids, _tg_chat_ids, _groups, _settings, _ai_config, _classifications, _leads
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
    try:
        _groups = json.load(open(GROUPS_FILE))
    except Exception:
        _groups = [{'id': DEFAULT_GROUP, 'name': ''}]
    try:
        _settings = json.load(open(SETTINGS_FILE))
    except Exception:
        _settings = {'auto_refresh': True, 'interval': 5}
    try:
        _ai_config = json.load(open(AI_CONFIG_FILE))
    except Exception:
        _ai_config = {
            'provider': 'gemini',
            'model': DEFAULT_MODEL,
            'keys': {'gemini': '', 'openai': '', 'claude': ''},
            'auto_classify': False,
            'categories': DEFAULT_CATEGORIES,
        }
    try:
        _classifications = json.load(open(CLASSIFICATIONS_FILE))
    except Exception:
        _classifications = {}
    try:
        _leads = json.load(open(LEADS_FILE))
    except Exception:
        _leads = {}


def _save_seen():
    with open(SEEN_FILE, 'w') as f:
        json.dump(list(_seen_ids), f)


def _save_tg():
    with open(TG_CONFIG_FILE, 'w') as f:
        json.dump({'chat_ids': _tg_chat_ids}, f)


def _save_groups():
    with open(GROUPS_FILE, 'w') as f:
        json.dump(_groups, f, ensure_ascii=False)


def _save_settings():
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(_settings, f)


def _save_ai_config():
    with open(AI_CONFIG_FILE, 'w') as f:
        json.dump(_ai_config, f, ensure_ascii=False)


def _save_classifications():
    with open(CLASSIFICATIONS_FILE, 'w') as f:
        json.dump(_classifications, f, ensure_ascii=False)


def _save_leads():
    with open(LEADS_FILE, 'w') as f:
        json.dump(_leads, f, ensure_ascii=False)


def _get_classifier() -> AIClassifier:
    provider = _ai_config.get('provider', 'gemini')
    default_model = PROVIDERS.get(provider, {}).get('default_model', DEFAULT_MODEL)
    model = _ai_config.get('model', default_model) or default_model
    api_key = _ai_config.get('keys', {}).get(provider, '') or DEFAULT_API_KEY
    categories = _ai_config.get('categories', DEFAULT_CATEGORIES)
    return AIClassifier(provider, model, api_key, categories)


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


@app.route('/api/post', methods=['POST'])
def api_create_post():
    body = request.get_json() or {}
    group_id = body.get('group_id', '').strip()
    message = body.get('message', '').strip()
    page_id = body.get('page_id', '').strip()
    if not group_id or not message:
        return jsonify({'ok': False, 'error': 'Thiếu group_id hoặc message'}), 400
    try:
        page_token = _pages_cache.get(page_id, {}).get('access_token') if page_id else None
        result = get_api(group_id).create_post(message, page_token)
        if result and 'id' in result:
            return jsonify({'ok': True, 'post_id': result['id']})
        err = (result or {}).get('error', {}).get('message', 'Lỗi không xác định')
        return jsonify({'ok': False, 'error': err})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/pages')
def api_pages():
    global _pages_cache
    try:
        pages = get_api(DEFAULT_GROUP).get_pages() or []
        _pages_cache = {p['id']: {'name': p['name'], 'access_token': p['access_token']} for p in pages}
        return jsonify([{'id': p['id'], 'name': p['name']} for p in pages])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/comment', methods=['POST'])
def api_comment():
    body = request.get_json() or {}
    post_id = body.get('post_id', '').strip()
    message = body.get('message', '').strip()
    group_id = body.get('group_id', DEFAULT_GROUP)
    page_id = body.get('page_id', '').strip()
    if not post_id or not message:
        return jsonify({'ok': False, 'error': 'Thiếu post_id hoặc message'}), 400
    try:
        page_token = _pages_cache.get(page_id, {}).get('access_token') if page_id else None
        result = get_api(group_id).post_comment(post_id, message, page_token)
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
    try:
        api = get_api(DEFAULT_GROUP)
        data = api.resolve_slug(slug)
        if data and 'id' in data:
            is_member = api.check_membership(data['id'])
            return jsonify({'ok': True, 'id': data['id'], 'name': data.get('name', slug), 'is_member': is_member})
        err = (data or {}).get('error', {}).get('message', 'Không tìm thấy group')
        return jsonify({'ok': False, 'error': err})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/groups/<gid>/join', methods=['POST'])
def api_join_group(gid):
    try:
        result = get_api(DEFAULT_GROUP).join_group(gid)
        return jsonify(result)
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


@app.route('/api/groups', methods=['GET'])
def groups_get():
    return jsonify(_groups)


@app.route('/api/groups', methods=['POST'])
def groups_add():
    global _groups
    body = request.get_json() or {}
    gid = body.get('id', '').strip()
    name = body.get('name', '').strip()
    if not gid:
        return jsonify({'ok': False, 'error': 'Thiếu id'}), 400
    if not any(g['id'] == gid for g in _groups):
        _groups.append({'id': gid, 'name': name})
        _save_groups()
    else:
        for g in _groups:
            if g['id'] == gid and name:
                g['name'] = name
        _save_groups()
    return jsonify({'ok': True, 'groups': _groups})


@app.route('/api/groups/<gid>', methods=['DELETE'])
def groups_remove(gid):
    global _groups
    _groups = [g for g in _groups if g['id'] != gid]
    _save_groups()
    return jsonify({'ok': True, 'groups': _groups})


@app.route('/api/settings', methods=['GET'])
def settings_get():
    return jsonify(_settings)


@app.route('/api/settings', methods=['POST'])
def settings_save():
    global _settings
    body = request.get_json() or {}
    _settings.update({k: v for k, v in body.items() if k in ('auto_refresh', 'interval')})
    _save_settings()
    return jsonify({'ok': True, 'settings': _settings})


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


# ── AI Routes ──────────────────────────────────────────
@app.route('/api/ai/providers')
def ai_providers():
    return jsonify(PROVIDERS)


@app.route('/api/ai/config', methods=['GET'])
def ai_config_get():
    safe = dict(_ai_config)
    safe_keys = {}
    for k, v in safe.get('keys', {}).items():
        safe_keys[k] = ('***' + v[-4:]) if v and len(v) > 4 else ('***' if v else '')
    safe.pop('keys', None)
    safe['keys_masked'] = safe_keys
    return jsonify(safe)


@app.route('/api/ai/config', methods=['POST'])
def ai_config_save():
    global _ai_config
    body = request.get_json() or {}
    if 'provider' in body:
        _ai_config['provider'] = body['provider']
    if 'model' in body:
        _ai_config['model'] = body['model']
    if 'auto_classify' in body:
        _ai_config['auto_classify'] = bool(body['auto_classify'])
    if 'categories' in body and isinstance(body['categories'], list):
        _ai_config['categories'] = body['categories']
    if 'key' in body:
        provider = body.get('provider', _ai_config.get('provider', 'gemini'))
        if 'keys' not in _ai_config:
            _ai_config['keys'] = {}
        _ai_config['keys'][provider] = body['key']
    _save_ai_config()
    return jsonify({'ok': True})


@app.route('/api/ai/test', methods=['POST'])
def ai_test():
    classifier = _get_classifier()
    if not classifier.api_key:
        return jsonify({'ok': False, 'error': 'Chưa nhập API key'})
    result = classifier.test_connection()
    return jsonify(result)


@app.route('/api/ai/key/<provider>', methods=['DELETE'])
def ai_key_delete(provider):
    global _ai_config
    if 'keys' in _ai_config and provider in _ai_config['keys']:
        _ai_config['keys'][provider] = ''
        _save_ai_config()
    return jsonify({'ok': True})


@app.route('/api/ai/classify', methods=['POST'])
def ai_classify():
    global _classifications
    body = request.get_json() or {}
    posts = body.get('posts', [])
    force = body.get('force', False)
    if not posts:
        return jsonify({'ok': False, 'error': 'Không có bài viết'})
    classifier = _get_classifier()
    if not classifier.api_key:
        return jsonify({'ok': False, 'error': 'Chưa cấu hình API key'})
    to_classify = [p for p in posts if force or p.get('id') not in _classifications]
    if not to_classify:
        return jsonify({'ok': True, 'classifications': {pid: _classifications[pid] for pid in [p['id'] for p in posts] if pid in _classifications}})
    results = classifier.classify_posts(to_classify)
    if classifier.last_error and not results:
        return jsonify({'ok': False, 'error': classifier.last_error}), 502
    _classifications.update(results)
    _save_classifications()
    all_results = {p['id']: _classifications.get(p['id'], '') for p in posts}
    return jsonify({'ok': True, 'classifications': all_results})


@app.route('/api/ai/classifications', methods=['GET'])
def ai_classifications_get():
    return jsonify(_classifications)


@app.route('/api/ai/leads', methods=['GET'])
def ai_leads_get():
    return jsonify(_leads)


@app.route('/api/ai/extract-leads', methods=['POST'])
def ai_extract_leads():
    global _leads
    body = request.get_json() or {}
    posts = body.get('posts', [])
    force = body.get('force', False)
    if not posts:
        return jsonify({'ok': False, 'error': 'Không có bài viết'})
    classifier = _get_classifier()
    if not classifier.api_key:
        return jsonify({'ok': False, 'error': 'Chưa cấu hình API key'})

    to_extract = [p for p in posts if force or p.get('id') not in _leads]
    if to_extract:
        results = classifier.extract_leads(to_extract)
        if classifier.last_error and not results:
            return jsonify({'ok': False, 'error': classifier.last_error}), 502
        for post in to_extract:
            pid = post.get('id')
            if pid:
                _leads[pid] = results.get(pid, [])
        _save_leads()

    all_results = {p['id']: _leads.get(p['id'], []) for p in posts if p.get('id')}
    payload = {'ok': True, 'leads': all_results}
    if classifier.last_error:
        payload['warning'] = classifier.last_error
    return jsonify(payload)


# ── Start ──────────────────────────────────────────────
_load_state()
threading.Thread(target=_poll_telegram, daemon=True).start()

if __name__ == '__main__':
    print(f'🚀 Server đang chạy tại http://localhost:{PORT}')
    app.run(debug=False, port=PORT)
