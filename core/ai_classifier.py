import json
import re
import requests
from typing import Optional, Dict, List

DEFAULT_MODEL = 'gemini-flash-latest'
DEFAULT_API_KEY = ''

PROVIDERS = {
    'gemini': { 'name': 'Google Gemini', 'default_model': 'gemini-flash-latest' },
    'openai': { 'name': 'OpenAI',        'default_model': 'gpt-4o-mini' },
    'claude': { 'name': 'Claude',        'default_model': 'claude-3-haiku-20240307' },
}

DEFAULT_CATEGORIES = [
    'Mua bán', 'Hỏi đáp', 'Thông báo', 'Tán gẫu',
    'Spam/Quảng cáo', 'Tuyển dụng', 'Chia sẻ kiến thức',
]

PHONE_RE = re.compile(r'(?<!\d)(?:\+?84|0)(?:[\s.\-()]?\d){8,10}(?!\d)')

LEAD_EXTRACTION_PROMPT = """Bạn là AI trích xuất lead/nhu cầu từ bài viết Facebook tiếng Việt.

Quy tắc:
- Trích từng người có nhu cầu thật sự từ bài viết hoặc bình luận.
- Comment không có số điện thoại vẫn được trích nếu có nhu cầu như hỏi giá, cần mua, cần thuê, xin tư vấn, còn hàng, muốn inbox, hỏi địa điểm, hỏi ngân sách.
- Dùng ngữ cảnh bài gốc để hiểu comment ngắn như "xin giá", "ib mình", "còn không", "mình lấy 2".
- Không trích các comment chỉ tag bạn bè, chấm, hóng, emoji, spam không liên quan.
- Không tự bịa tên, số điện thoại, địa điểm, ngân sách. Chỉ dùng dữ liệu có trong nguồn.
- source_id phải giữ nguyên đúng SOURCE_ID được cung cấp.
- confidence là số từ 0 đến 1.

Dữ liệu:
{posts}

Trả về JSON array. Mỗi phần tử có đúng các trường:
[
  {{
    "post_id": "id bài viết",
    "source": "post hoặc comment",
    "source_id": "SOURCE_ID",
    "name": "tên tác giả nguồn",
    "phone": "số đầu tiên nếu có, không có thì chuỗi rỗng",
    "phones": ["các số nếu có"],
    "need": "mô tả ngắn nhu cầu",
    "intent": "buyer|seller|renter|service_request|job|question|other",
    "product_or_service": "sản phẩm/dịch vụ nếu xác định được",
    "location": "địa điểm nếu có",
    "budget": "ngân sách/giá nếu có",
    "urgency": "low|medium|high",
    "contact_status": "has_phone|no_phone",
    "confidence": 0.0,
    "evidence": "câu ngắn chứng minh"
  }}
]

CHỈ trả về JSON, không giải thích."""

CLASSIFY_PROMPT = """Bạn là AI phân loại bài viết Facebook. Phân loại các bài viết sau vào MỘT trong các danh mục: {categories}.

{posts}

Trả về kết quả dưới dạng JSON array, mỗi phần tử là object có "id" và "category".
Ví dụ: [{{"id":"123","category":"Mua bán"}}]
CHỈ trả về JSON, không giải thích."""


def normalize_phone(raw: str) -> str:
    digits = re.sub(r'\D', '', raw or '')
    if digits.startswith('0084'):
        digits = '0' + digits[4:]
    elif digits.startswith('84') and len(digits) in (11, 12):
        digits = '0' + digits[2:]
    if len(digits) in (10, 11) and digits.startswith('0'):
        return digits
    return ''


def extract_phones(text: str) -> List[str]:
    seen = set()
    phones = []
    for match in PHONE_RE.finditer(text or ''):
        phone = normalize_phone(match.group())
        if phone and phone not in seen:
            seen.add(phone)
            phones.append(phone)
    return phones


def _compact_text(text: str, limit: int = 900) -> str:
    text = re.sub(r'\s+', ' ', text or '').strip()
    if len(text) > limit:
        return text[:limit].rstrip() + '...'
    return text


def _strip_json_fence(text: str) -> str:
    text = (text or '').strip()
    if text.startswith('```'):
        lines = text.split('\n')
        end = len(lines) - 1 if lines and lines[-1].strip().startswith('```') else len(lines)
        text = '\n'.join(lines[1:end]).strip()
    return text


def _load_json_payload(text: str):
    text = _strip_json_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'(\[.*\]|\{.*\})', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _as_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


class AIClassifier:
    def __init__(self, provider: str, model: str, api_key: str, categories: List[str] = None):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.categories = categories or DEFAULT_CATEGORIES
        self.last_error = ''

    def classify_posts(self, posts: List[Dict]) -> Dict[str, str]:
        """Classify multiple posts. Returns {post_id: category}."""
        if not posts or not self.api_key:
            return {}
        posts_text = ""
        for i, post in enumerate(posts, 1):
            text = post.get('message', '') or '[Không có nội dung]'
            pid = post.get('id', f'post_{i}')
            author = (post.get('from') or {}).get('name', 'Ẩn danh')
            posts_text += f'Bài {i} (ID: {pid}):\nTác giả: {author}\nNội dung: {text[:500]}\n\n'

        prompt = CLASSIFY_PROMPT.format(
            categories=', '.join(self.categories),
            posts=posts_text
        )
        try:
            resp = self._call_api(prompt)
            self.last_error = ''
            return self._parse_response(resp)
        except Exception as e:
            self.last_error = str(e)
            print(f'AI classify error: {e}')
            return {}

    def extract_leads(self, posts: List[Dict], batch_size: int = 4) -> Dict[str, List[Dict]]:
        """Extract lead/need records from posts and loaded comments."""
        if not posts or not self.api_key:
            return {}
        results: Dict[str, List[Dict]] = {}
        errors = []
        for start in range(0, len(posts), batch_size):
            batch = posts[start:start + batch_size]
            posts_text, source_meta = self._format_lead_posts(batch)
            if not posts_text.strip():
                continue
            prompt = LEAD_EXTRACTION_PROMPT.format(posts=posts_text)
            try:
                resp = self._call_api(prompt)
                for lead in self._parse_leads_response(resp, source_meta):
                    results.setdefault(lead['post_id'], []).append(lead)
            except Exception as e:
                msg = str(e)
                errors.append(msg)
                print(f'AI lead extract error: {e}')
        self.last_error = '; '.join(dict.fromkeys(errors))[:500] if errors else ''
        return {pid: self._dedupe_leads(items) for pid, items in results.items()}

    def _format_lead_posts(self, posts: List[Dict]) -> tuple[str, Dict[str, Dict]]:
        blocks = []
        source_meta: Dict[str, Dict] = {}
        for post_index, post in enumerate(posts, 1):
            pid = str(post.get('id') or f'post_{post_index}')
            author = (post.get('from') or {}).get('name', 'Ẩn danh')
            text = post.get('message', '') or ''
            post_phones = extract_phones(text)
            source_meta[pid] = {
                'post_id': pid,
                'source': 'post',
                'name': author,
                'phones': post_phones,
            }
            lines = [
                f'POST {post_index}',
                f'POST_ID: {pid}',
                f'SOURCE_ID: {pid}',
                f'AUTHOR: {author}',
                f'PHONES_IN_TEXT: {", ".join(post_phones) if post_phones else ""}',
                f'TEXT: {_compact_text(text, 1200) or "[Không có nội dung]"}',
                'COMMENTS:',
            ]

            comments = ((post.get('comments') or {}).get('data') or [])[:50]
            if not comments:
                lines.append('- [Không có bình luận được tải]')
            for idx, comment in enumerate(comments, 1):
                cid = str(comment.get('id') or f'{pid}:comment:{idx}')
                cname = (comment.get('from') or {}).get('name', 'Ẩn danh')
                ctext = comment.get('message', '') or ''
                cphones = extract_phones(ctext)
                source_meta[cid] = {
                    'post_id': pid,
                    'source': 'comment',
                    'name': cname,
                    'phones': cphones,
                }
                lines.extend([
                    f'- COMMENT {idx}',
                    f'  SOURCE_ID: {cid}',
                    f'  AUTHOR: {cname}',
                    f'  PHONES_IN_TEXT: {", ".join(cphones) if cphones else ""}',
                    f'  TEXT: {_compact_text(ctext, 500) or "[Không có nội dung]"}',
                ])
            blocks.append('\n'.join(lines))
        return '\n\n---\n\n'.join(blocks), source_meta

    def _parse_leads_response(self, text: str, source_meta: Dict[str, Dict]) -> List[Dict]:
        payload = _load_json_payload(text)
        if isinstance(payload, dict):
            payload = payload.get('leads') or payload.get('data') or []
        if not isinstance(payload, list):
            return []

        leads = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get('source_id') or '').strip()
            meta = source_meta.get(source_id)
            if not meta:
                continue

            need = _compact_text(str(item.get('need') or item.get('need_summary') or ''), 220)
            if not need:
                continue

            phones = meta.get('phones') or []
            lead = {
                'post_id': meta['post_id'],
                'source': meta['source'],
                'source_id': source_id,
                'name': meta.get('name') or str(item.get('name') or 'Ẩn danh'),
                'phone': phones[0] if phones else '',
                'phones': phones,
                'need': need,
                'intent': str(item.get('intent') or 'other')[:40],
                'product_or_service': _compact_text(str(item.get('product_or_service') or ''), 120),
                'location': _compact_text(str(item.get('location') or ''), 80),
                'budget': _compact_text(str(item.get('budget') or ''), 80),
                'urgency': str(item.get('urgency') or 'low')[:20],
                'contact_status': 'has_phone' if phones else 'no_phone',
                'confidence': _as_float(item.get('confidence'), 0.5),
                'evidence': _compact_text(str(item.get('evidence') or ''), 180),
            }
            leads.append(lead)
        return leads

    def _dedupe_leads(self, leads: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        for lead in sorted(leads, key=lambda item: item.get('confidence', 0), reverse=True):
            key = (lead.get('source_id'), lead.get('need', '').lower(), lead.get('phone', ''))
            if key in seen:
                continue
            seen.add(key)
            unique.append(lead)
        return unique

    def test_connection(self) -> Dict:
        try:
            resp = self._call_api('Trả lời "OK" nếu bạn nhận được tin nhắn này.')
            return {'ok': True, 'response': resp[:100]}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def _call_api(self, prompt: str) -> str:
        if self.provider == 'gemini':
            return self._call_gemini(prompt)
        elif self.provider == 'openai':
            return self._call_openai(prompt)
        elif self.provider == 'claude':
            return self._call_claude(prompt)
        raise ValueError(f'Unknown provider: {self.provider}')

    def _call_gemini(self, prompt: str) -> str:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent'
        resp = requests.post(url,
            headers={
                'Content-Type': 'application/json',
                'X-goog-api-key': self.api_key,
            },
            json={
                'contents': [{'parts': [{'text': prompt}]}],
            }, timeout=60)
        data = resp.json()
        if 'error' in data:
            raise Exception(data['error'].get('message', 'Gemini API error'))
        return data['candidates'][0]['content']['parts'][0]['text']

    def _call_openai(self, prompt: str) -> str:
        resp = requests.post('https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {self.api_key}'},
            json={
                'model': self.model,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.3,
            }, timeout=60)
        data = resp.json()
        if 'error' in data:
            raise Exception(data['error'].get('message', 'OpenAI API error'))
        return data['choices'][0]['message']['content']

    def _call_claude(self, prompt: str) -> str:
        resp = requests.post('https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': self.api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': self.model,
                'max_tokens': 4096,
                'messages': [{'role': 'user', 'content': prompt}],
            }, timeout=60)
        data = resp.json()
        if data.get('type') == 'error' or 'error' in data:
            err = data.get('error', {})
            raise Exception(err.get('message', 'Claude API error'))
        return data['content'][0]['text']

    def _parse_response(self, text: str) -> Dict[str, str]:
        results = _load_json_payload(text)
        if isinstance(results, dict):
            results = results.get('data') or results.get('classifications') or []
        if isinstance(results, list):
            return {str(item['id']): item['category'] for item in results
                    if isinstance(item, dict) and 'id' in item and 'category' in item}
        return {}
