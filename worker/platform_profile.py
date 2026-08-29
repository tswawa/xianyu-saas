"""统一的闲鱼 Worker 请求指纹。

本模块只保存公开的协议常量，不保存账号或认证信息。
"""

CHROME_MAJOR = 133
PLATFORM = "Windows"
ORIGIN = "https://www.goofish.com"
REFERER = "https://www.goofish.com/"
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
SEC_CH_UA = (
    f'"Not(A:Brand";v="99", "Google Chrome";v="{CHROME_MAJOR}", '
    f'"Chromium";v="{CHROME_MAJOR}"'
)
DINGTALK_REGISTRATION_UA = (
    f"{USER_AGENT} DingTalk(2.1.5) OS(Windows/10) "
    f"Browser(Chrome/{CHROME_MAJOR}.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5"
)


def browser_headers(*, content_type=None):
    """返回 HTTP/MTOP 共用的浏览器身份头。"""
    headers = {
        "accept": "application/json",
        "accept-language": ACCEPT_LANGUAGE,
        "cache-control": "no-cache",
        "origin": ORIGIN,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": REFERER,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{PLATFORM}"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,
    }
    if content_type:
        headers["content-type"] = content_type
    return headers


def websocket_headers(cookie_header):
    """返回 WebSocket 握手头；Cookie 仅由调用方在内存中注入。"""
    return {
        "Cookie": cookie_header,
        "Host": "wss-goofish.dingtalk.com",
        "Connection": "Upgrade",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "User-Agent": USER_AGENT,
        "Origin": ORIGIN,
        "Referer": REFERER,
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": ACCEPT_LANGUAGE,
        "Sec-CH-UA": SEC_CH_UA,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": f'"{PLATFORM}"',
    }
