import json
import re
import requests
from typing import Optional, Dict, List

DEFAULT_MODEL = 'gemini-flash-latest'
DEFAULT_API_KEY = 'AIzaSyAQs_5bzQ_rVidJybw7YzSEYsXXgB8inZ8'

PROVIDERS = {
    'gemini': { 'name': 'Google Gemini', 'default_model': 'gemini-flash-latest' },
    'openai': { 'name': 'OpenAI',        'default_model': 'gpt-4o-mini' },
    'claude': { 'name': 'Claude',        'default_model': 'claude-3-haiku-20240307' },
}

DEFAULT_CATEGORIES = [
    'Mua bán', 'Hỏi đáp', 'Thông báo', 'Tán gẫu',
    'Spam/Quảng cáo', 'Tuyển dụng', 'Chia sẻ kiến thức',
]

CLASSIFY_PROMPT = """Bạn là AI phân loại bài viết Facebook. Phân loại các bài viết sau vào MỘT trong các danh mục: {categories}.

{posts}

Trả về kết quả dưới dạng JSON array, mỗi phần tử là object có "id" và "category".
Ví dụ: [{{"id":"123","category":"Mua bán"}}]
CHỈ trả về JSON, không giải thích."""


class AIClassifier:
    def __init__(self, provider: str, model: str, api_key: str, categories: List[str] = None):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.categories = categories or DEFAULT_CATEGORIES

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
            return self._parse_response(resp)
        except Exception as e:
            print(f'AI classify error: {e}')
            return {}

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
        text = text.strip()
        if text.startswith('```'):
            lines = text.split('\n')
            end = len(lines) - 1 if lines[-1].strip().startswith('```') else len(lines)
            text = '\n'.join(lines[1:end]).strip()
        try:
            results = json.loads(text)
            if isinstance(results, list):
                return {str(item['id']): item['category'] for item in results
                        if 'id' in item and 'category' in item}
        except json.JSONDecodeError:
            pass
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                results = json.loads(match.group())
                if isinstance(results, list):
                    return {str(item['id']): item['category'] for item in results
                            if 'id' in item and 'category' in item}
            except json.JSONDecodeError:
                pass
        return {}
