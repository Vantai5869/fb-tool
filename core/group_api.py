import requests
import os
from typing import Optional, List, Dict
from core.token_gen import FacebookTokenGenerator

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(BASE_DIR, 'data', 'token_success.txt')
COOKIE_FILE = os.path.join(BASE_DIR, 'data', 'cookie.txt')
FB_CLIENT_ID = '350685531728'
GRAPH_URL = 'https://graph.facebook.com/v21.0'


def load_token() -> Optional[str]:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if l.strip()]
    if not lines:
        return None
    return lines[-1].split('|')[-1]


def load_cookie() -> Optional[str]:
    if not os.path.exists(COOKIE_FILE):
        return None
    with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
        return f.read().strip() or None


def refresh_token() -> Optional[str]:
    cookie = load_cookie()
    if not cookie:
        print('Không tìm thấy cookie.txt — cần cập nhật cookie thủ công')
        return None
    print('🔄 Token hết hạn, đang lấy token mới từ cookie...')
    return FacebookTokenGenerator(FB_CLIENT_ID, cookie).GetToken()


class FacebookGroupAPI:
    def __init__(self, group_id: str):
        self.group_id = group_id
        self.access_token = load_token() or refresh_token()

    def _is_expired(self, data: dict) -> bool:
        return data.get('error', {}).get('code') == 190

    def _call(self, method: str, url: str, **kwargs) -> Optional[dict]:
        for attempt in range(2):
            kwargs.setdefault('params', {})['access_token'] = self.access_token
            resp = getattr(requests, method)(url, **kwargs)
            data = resp.json()
            if self._is_expired(data):
                if attempt == 0:
                    new_token = refresh_token()
                    if new_token:
                        self.access_token = new_token
                        continue
                print('Không thể refresh token — kiểm tra lại cookie.txt')
                return None
            return data
        return None

    def get_posts(self, limit: int = 10) -> Optional[List[Dict]]:
        data = self._call('get', f'{GRAPH_URL}/{self.group_id}/feed', params={
            'fields': 'id,message,from,created_time,updated_time,is_hidden,permalink_url,attachments,comments.limit(5){message,from},reactions.limit(0).summary(true),shares',
            'limit': limit,
        })
        return data.get('data') if data else None

    def create_post(self, message: str, page_token: str = None) -> Optional[dict]:
        token = page_token or self.access_token
        resp = requests.post(
            f'{GRAPH_URL}/{self.group_id}/feed',
            params={'access_token': token, 'message': message}
        )
        return resp.json()

    def get_pages(self) -> Optional[list]:
        data = self._call('get', f'{GRAPH_URL}/me/accounts', params={'fields': 'id,name,access_token'})
        return data.get('data') if data else None

    def post_comment(self, post_id: str, message: str, page_token: str = None) -> Optional[dict]:
        token = page_token or self.access_token
        resp = requests.post(
            f'{GRAPH_URL}/{post_id}/comments',
            params={'access_token': token, 'message': message}
        )
        return resp.json()

    def resolve_slug(self, slug: str) -> Optional[dict]:
        # Thử Graph API trước
        data = self._call('get', f'{GRAPH_URL}/{slug}', params={'fields': 'id,name'})
        if data and 'id' in data:
            return data
        # Fallback: scrape trang group lấy numeric ID qua cookie
        return _scrape_group_id(slug)


def _scrape_group_id(slug: str) -> Optional[dict]:
    cookie = load_cookie()
    if not cookie:
        return None
    import re as _re
    from collections import Counter
    try:
        resp = requests.get(
            f'https://mbasic.facebook.com/groups/{slug}?v=info',
            headers={
                'user-agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
                'accept': 'text/html',
                'accept-language': 'vi-VN,vi;q=0.9,en;q=0.5',
                'Cookie': cookie,
            },
            timeout=15,
            allow_redirects=True,
        )
        html = resp.text
        # Lấy số xuất hiện nhiều nhất trong khoảng 10-16 chữ số (độ dài ID group FB)
        candidates = _re.findall(r'\b(\d{10,16})\b', html)
        if not candidates:
            return None
        freq = Counter(candidates)
        gid = freq.most_common(1)[0][0]
        # Lấy tên group từ title
        name_m = _re.search(r'<title>([^<]+)</title>', html)
        name = name_m.group(1).replace('| Facebook', '').strip() if name_m else slug
        return {'id': gid, 'name': name}
    except Exception:
        pass
    return None
