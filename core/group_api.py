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
            'fields': 'id,message,from,created_time,updated_time,is_hidden,permalink_url,attachments,comments.limit(50).summary(true){id,message,from,created_time},reactions.limit(0).summary(true),shares',
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
        data = self._call('get', f'{GRAPH_URL}/{slug}', params={'fields': 'id,name'})
        if data and 'id' in data:
            return data
        return _scrape_group_id(slug)

    def check_membership(self, group_id: str) -> bool:
        """Check if current user is a member of the group."""
        # Try to get /member endpoint which requires membership
        data = self._call('get', f'{GRAPH_URL}/{group_id}', params={'fields': 'id,name'})
        if data is None or 'error' in data:
            return False
        # Also try feed access — if feed returns error, not a member
        feed = self._call('get', f'{GRAPH_URL}/{group_id}/feed', params={'fields': 'id', 'limit': 1})
        if feed is None or 'error' in feed:
            return False
        # For public groups, feed is accessible even without membership
        # Use cookie-based check as fallback
        return self._cookie_check_membership(group_id)

    def _cookie_check_membership(self, group_id: str) -> bool:
        """Check membership via mbasic.facebook.com (cookie-based)."""
        import re
        cookie = load_cookie()
        if not cookie:
            return True  # Can't check, assume member
        try:
            resp = requests.get(
                f'https://mbasic.facebook.com/groups/{group_id}',
                headers={
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
                    'Accept': 'text/html',
                    'Cookie': cookie,
                },
                timeout=15,
                allow_redirects=True,
            )
            html = resp.text
            # If redirected to login
            if '/login' in resp.url or 'Đăng nhập' in html[:500]:
                return True  # Can't determine, assume member
            # Check for leave/member indicators
            if re.search(r'leave_group|rời nhóm|Rời Nhóm|Đã tham gia', html, re.I):
                return True
            # Check for join button — means NOT a member
            if re.search(r'join.*group|tham gia nhóm|Tham Gia Nhóm|/join/', html, re.I):
                return False
            return True  # Default assume member
        except Exception:
            return True

    def join_group(self, group_id: str) -> dict:
        import re
        cookie = load_cookie()
        if not cookie:
            return {'ok': False, 'error': 'Không có cookie'}
        try:
            sess = requests.Session()
            r = sess.get(
                f'https://mbasic.facebook.com/groups/{group_id}',
                headers={
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
                    'Accept': 'text/html',
                    'Cookie': cookie,
                },
                timeout=15,
            )
            html = r.text
            if re.search(r'leave_group|rời nhóm|Rời Nhóm', html, re.I):
                return {'ok': True, 'already_member': True, 'msg': 'Đã là thành viên'}
            m = re.search(r'action="(/groups/[^"]*join[^"]*)"', html, re.I) or \
                re.search(r'action="(/a/group/join[^"]*)"', html, re.I)
            if not m:
                return {'ok': False, 'error': 'Nhóm riêng tư hoặc không tìm được nút tham gia'}
            form_url = 'https://mbasic.facebook.com' + m.group(1).replace('&amp;', '&')
            inputs = {k: v for k, v in re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html)}
            r2 = sess.post(
                form_url, data=inputs,
                headers={
                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
                    'Cookie': cookie,
                    'Referer': f'https://mbasic.facebook.com/groups/{group_id}',
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                timeout=15,
            )
            if r2.status_code < 400:
                return {'ok': True, 'msg': 'Đã gửi yêu cầu tham gia nhóm'}
            return {'ok': False, 'error': f'Lỗi HTTP {r2.status_code}'}
        except Exception as e:
            return {'ok': False, 'error': str(e)}


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
        # Detect login redirect
        if '/login' in resp.url or 'Đăng nhập' in html[:500] or '<title>Log in' in html[:500]:
            return None
        # Detect error pages
        title_m = _re.search(r'<title>([^<]+)</title>', html)
        if title_m:
            title = title_m.group(1).strip()
            # Skip if title is a login/error page
            if any(k in title.lower() for k in ['đăng nhập', 'log in', 'login', 'error', 'not found']):
                return None
        # Lấy số xuất hiện nhiều nhất trong khoảng 10-16 chữ số (độ dài ID group FB)
        candidates = _re.findall(r'\b(\d{10,16})\b', html)
        if not candidates:
            return None
        freq = Counter(candidates)
        gid = freq.most_common(1)[0][0]
        # Lấy tên group từ title
        name = title_m.group(1).replace('| Facebook', '').strip() if title_m else slug
        return {'id': gid, 'name': name}
    except Exception:
        pass
    return None
