import requests
import uuid
import re
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(BASE_DIR, 'data', 'token_success.txt')


class FacebookTokenGenerator:
    def __init__(self, client_id, cookie):
        self.client_id = client_id
        self.cookie_raw = re.sub(r'\s+', '', cookie, flags=re.UNICODE)
        self.cookies = self._parse_cookies()

    def _parse_cookies(self):
        result = {}
        for part in self.cookie_raw.strip().split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                result[k.strip()] = v.strip()
        return result

    def GetToken(self):
        try:
            c_user = self.cookies.get('c_user')
            if not c_user:
                raise ValueError('Không tìm thấy c_user trong cookie')

            get_data = requests.get(
                'https://www.facebook.com/v2.3/dialog/oauth',
                params={
                    'redirect_uri': 'fbconnect://success',
                    'scope': 'email,public_profile',
                    'response_type': 'token,code',
                    'client_id': self.client_id,
                },
                cookies=self.cookies,
                headers={
                    'authority': 'www.facebook.com',
                    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'accept-language': 'vi,en-US;q=0.9,en;q=0.8',
                    'cache-control': 'max-age=0',
                    'dpr': '1.25',
                    'sec-ch-ua': '"Chromium";v="117", "Not;A=Brand";v="8"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                    'sec-fetch-dest': 'document',
                    'sec-fetch-mode': 'navigate',
                    'sec-fetch-site': 'same-origin',
                    'upgrade-insecure-requests': '1',
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
                }
            ).text

            fb_dtsg_match = re.search(r'DTSGInitData",,\{"token":"(.+?)"', get_data.replace('[]', ''))
            if not fb_dtsg_match:
                raise ValueError('Không tìm thấy fb_dtsg trong response')
            fb_dtsg = fb_dtsg_match.group(1)

            variables = '{"input":{"client_mutation_id":"4","actor_id":"' + c_user + '","config_enum":"GDP_READ","device_id":null,"experience_id":"' + str(uuid.uuid4()) + '","extra_params_json":"{\\"app_id\\":\\"' + self.client_id + '\\",\\"display\\":\\"\\\\\\"popup\\\\\\"\\",\\"kid_directed_site\\":\\"false\\",\\"logger_id\\":\\"\\\\\\"' + str(uuid.uuid4()) + '\\\\\\"\\",\\"next\\":\\"\\\\\\"read\\\\\\"\\",\\"redirect_uri\\":\\"\\\\\\"https:\\\\\\\\\\\\/\\\\\\\\\\\\/www.facebook.com\\\\\\\\\\\\/connect\\\\\\\\\\\\/login_success.html\\\\\\"\\",\\"response_type\\":\\"\\\\\\"token\\\\\\"\\",\\"return_scopes\\":\\"false\\",\\"scope\\":\\"[\\\\\\"email\\\\\\",\\\\\\"public_profile\\\\\\"]\\",\\"sso_key\\":\\"\\\\\\"com\\\\\\"\\",\\"steps\\":\\"{\\\\\\"read\\\\\\":[\\\\\\"email\\\\\\",\\\\\\"public_profile\\\\\\"]}\\",\\"tp\\":\\"\\\\\\"unspecified\\\\\\"\\",\\"cui_gk\\":\\"\\\\\\"[PASS]:\\\\\\"\\",\\"is_limited_login_shim\\":\\"false\\"}","flow_name":"GDP","flow_step_type":"STANDALONE","outcome":"APPROVED","source":"gdp_delegated","surface":"FACEBOOK_COMET"}}'

            gql_resp = requests.post(
                'https://www.facebook.com/api/graphql/',
                cookies=self.cookies,
                headers={
                    'authority': 'www.facebook.com',
                    'accept': '*/*',
                    'accept-language': 'vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5',
                    'content-type': 'application/x-www-form-urlencoded',
                    'dnt': '1',
                    'origin': 'https://www.facebook.com',
                    'sec-ch-prefers-color-scheme': 'dark',
                    'sec-ch-ua': '"Chromium";v="117", "Not;A=Brand";v="8"',
                    'sec-ch-ua-full-version-list': '"Chromium";v="117.0.5938.157", "Not;A=Brand";v="8.0.0.0"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-model': '""',
                    'sec-ch-ua-platform': '"Windows"',
                    'sec-ch-ua-platform-version': '"15.0.0"',
                    'sec-fetch-dest': 'empty',
                    'sec-fetch-mode': 'cors',
                    'sec-fetch-site': 'same-origin',
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
                    'x-fb-friendly-name': 'useCometConsentPromptEndOfFlowBatchedMutation',
                },
                data={
                    'av': c_user,
                    '__user': c_user,
                    'fb_dtsg': fb_dtsg,
                    'fb_api_caller_class': 'RelayModern',
                    'fb_api_req_friendly_name': 'useCometConsentPromptEndOfFlowBatchedMutation',
                    'variables': variables,
                    'server_timestamps': 'true',
                    'doc_id': '6494107973937368',
                }
            )

            if gql_resp.status_code != 200:
                raise ValueError(f'GraphQL lỗi: HTTP {gql_resp.status_code}')

            status_resp = requests.get(
                'https://www.facebook.com/x/oauth/status',
                params={
                    'client_id': self.client_id,
                    'input_token': '',
                    'origin': '1',
                    'redirect_uri': 'https://www.facebook.com/connect/login_success.html',
                    'sdk': 'joey',
                    'wants_cookie_data': 'true',
                },
                headers={
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
                    'accept': '*/*',
                    'accept-language': 'vi-VN,vi;q=0.9,en;q=0.5',
                    'origin': 'https://www.facebook.com',
                    'referer': 'https://www.facebook.com/',
                    'Cookie': self.cookie_raw,
                }
            )

            fb_ar = status_resp.headers.get('fb-ar')
            if not fb_ar:
                raise ValueError('Không lấy được fb-ar header')

            token = json.loads(fb_ar).get('access_token')
            if not token:
                raise ValueError('Không tìm thấy access_token trong fb-ar')

            with open(TOKEN_FILE, 'a', encoding='utf-8') as f:
                f.write(f'{c_user}|{token}\n')
            return token

        except Exception as e:
            print(f'Lỗi lấy token: {e}')
            return None
