import time
import hashlib
import json
import os
import threading

import requests
from loguru import logger
from platform_profile import browser_headers
from utils.xianyu_utils import generate_sign


class XianyuApiError(RuntimeError):
    """A classified platform request failure without response or secret data."""

    ALLOWED_CODES = frozenset({
        "risk_control",
        "session_expired",
        "platform_busy",
        "network_error",
        "response_invalid",
        "token_unavailable",
        "account_restricted",
    })

    def __init__(self, code="token_unavailable"):
        if code not in self.ALLOWED_CODES:
            raise ValueError("invalid platform error code")
        self.code = code
        super().__init__(code)


class XianyuAuthenticationError(XianyuApiError):
    """A confirmed authentication failure requiring a bounded recovery decision."""

    ALLOWED_CODES = frozenset({"session_expired", "risk_control"})

    def __init__(self, code):
        if code not in self.ALLOWED_CODES:
            raise ValueError("invalid authentication error code")
        self.code = code
        RuntimeError.__init__(self, code)


class XianyuApis:
    DEFAULT_REQUEST_TIMEOUT = (5.0, 25.0)
    DEFAULT_TOKEN_BACKOFF = (3.0, 10.0, 30.0)
    DEFAULT_REQUEST_BACKOFF = (0.5, 1.0)

    def __init__(
        self,
        request_timeout=DEFAULT_REQUEST_TIMEOUT,
        sleep_func=None,
        token_max_attempts=3,
        login_max_attempts=2,
        item_max_attempts=3,
        trade_max_attempts=3,
        token_backoff=DEFAULT_TOKEN_BACKOFF,
        request_backoff=DEFAULT_REQUEST_BACKOFF,
    ):
        if min(
            token_max_attempts,
            login_max_attempts,
            item_max_attempts,
            trade_max_attempts,
        ) < 1:
            raise ValueError("重试次数必须大于 0")

        self.url = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/'
        self.session = requests.Session()
        self._session_lock = threading.RLock()
        self.request_timeout = request_timeout
        self._sleep = sleep_func or time.sleep
        self.token_max_attempts = token_max_attempts
        self.login_max_attempts = login_max_attempts
        self.item_max_attempts = item_max_attempts
        self.trade_max_attempts = trade_max_attempts
        self.token_backoff = tuple(token_backoff)
        self.request_backoff = tuple(request_backoff)
        self.session.headers.update(browser_headers())

    def _wait_before_retry(self, attempt, total_attempts, schedule):
        """仅在还有预算时退避；sleep 可由测试注入。"""
        if attempt + 1 >= total_attempts or not schedule:
            return
        delay = schedule[min(attempt, len(schedule) - 1)]
        if delay > 0:
            self._sleep(delay)

    @staticmethod
    def _validate_device_id(device_id):
        """Validate the locally-derived device identifier before request construction."""
        if (
            not isinstance(device_id, str)
            or not device_id
            or len(device_id) > 256
            or any(
                not character.isascii()
                or not (character.isalnum() or character in "-_.:")
                for character in device_id
            )
        ):
            raise ValueError("device_id is invalid")
        return device_id

    @staticmethod
    def _validate_item_id(item_id):
        """Only numeric Goofish item IDs are valid for the detail endpoint."""
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 64
            or not item_id.isascii()
            or not item_id.isdigit()
        ):
            raise ValueError("item_id is invalid")
        return item_id

    @staticmethod
    def _validate_numeric_id(value, name, max_length=64):
        value = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
        if (
            not isinstance(value, str)
            or not value
            or len(value) > max_length
            or not value.isascii()
            or not value.isdigit()
        ):
            raise ValueError(f"{name} is invalid")
        return value

    @classmethod
    def _validate_session_id(cls, session_id):
        return cls._validate_numeric_id(session_id, "session_id", 64)

    @classmethod
    def _validate_order_id(cls, order_id):
        return cls._validate_numeric_id(order_id, "order_id", 64)

    @staticmethod
    def _is_success_response(payload):
        if not isinstance(payload, dict):
            return False
        ret_value = payload.get("ret", [])
        if isinstance(ret_value, str):
            ret_value = [ret_value]
        return any("SUCCESS::调用成功" in str(value) for value in ret_value)

    @staticmethod
    def _response_error_code(payload, status_code=None):
        """Map public platform signals to one stable, non-secret error code."""
        if not isinstance(payload, dict):
            return "response_invalid"
        ret_value = payload.get("ret", [])
        if isinstance(ret_value, str):
            ret_value = [ret_value]
        try:
            payload_text = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )[:8192]
        except (TypeError, ValueError):
            payload_text = ""
        text = (" ".join(str(value) for value in ret_value) + " " + payload_text).upper()
        if any(signal in text for signal in (
            "RGV587",
            "USER_VALIDATE",
            "SECURITY_CHECK",
            "CAPTCHA",
            "被挤爆啦",
        )):
            return "risk_control"
        if any(signal in text for signal in (
            "FAIL_SYS_SESSION_EXPIRED",
            "NOT_LOGIN",
            "AUTH_EXPIRED",
        )):
            return "session_expired"
        if any(signal in text for signal in (
            "ACCOUNT_BANNED",
            "PUBLISH_FORBIDDEN",
        )):
            return "account_restricted"
        if any(signal in text for signal in (
            "FAIL_SYS_BUSY",
            "SYSTEM_BUSY",
            "TOO_MANY_REQUEST",
            "TRAFFIC_LIMIT",
        )) or status_code in {409, 412, 429, 503}:
            return "platform_busy"
        return "token_unavailable"

    def _post_json(self, url, **kwargs):
        try:
            response = self.session.post(url, timeout=self.request_timeout, **kwargs)
        except requests.RequestException as exc:
            raise XianyuApiError("network_error") from exc
        except Exception as exc:
            raise XianyuApiError("network_error") from exc
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise XianyuApiError("response_invalid") from exc
        if not isinstance(payload, dict):
            raise XianyuApiError("response_invalid")
        return response, payload

    def _signed_mtop_request(
        self,
        api_name,
        endpoint,
        payload,
        *,
        attempts,
        value_type=None,
    ):
        """Issue one signed request with a finite retry budget on the shared session."""
        data_val = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        data = {"data": data_val}
        for attempt in range(attempts):
            params = {
                "jsv": "2.7.2",
                "appKey": "34839810",
                "t": str(int(time.time() * 1000)),
                "sign": "",
                "v": "1.0",
                "type": "originaljson",
                "accountSite": "xianyu",
                "dataType": "json",
                "timeout": "20000",
                "api": api_name,
                "sessionOption": "AutoLoginOnly",
                "spm_cnt": "a21ybx.im.0.0",
            }
            if value_type:
                params["valueType"] = value_type
            try:
                with self._session_lock:
                    token = self.session.cookies.get("_m_h5_tk", "").split("_")[0]
                    params["sign"] = generate_sign(params["t"], token, data_val)
                    response = self.session.post(
                        endpoint,
                        params=params,
                        data=data,
                        timeout=self.request_timeout,
                    )
                    result = response.json()
            except Exception as exc:
                logger.warning("订单核验接口请求异常 error={}", type(exc).__name__)
                self._wait_before_retry(
                    attempt, attempts, self.request_backoff
                )
                continue

            if self._is_success_response(result):
                return result

            ret_value = result.get("ret", []) if isinstance(result, dict) else []
            if isinstance(ret_value, str):
                ret_value = [ret_value]
            ret_text = " ".join(str(value) for value in ret_value)
            if "RGV587_ERROR" in ret_text or "被挤爆啦" in ret_text:
                logger.error("订单核验接口触发平台风控")
                raise XianyuAuthenticationError("risk_control")
            if "FAIL_SYS_SESSION_EXPIRED" in ret_text:
                logger.error("订单核验会话已过期")
                raise XianyuAuthenticationError("session_expired")
            if "Set-Cookie" in getattr(response, "headers", {}):
                self.clear_duplicate_cookies()
            logger.warning("订单核验接口调用失败")
            self._wait_before_retry(attempt, attempts, self.request_backoff)

        raise XianyuApiError("token_unavailable")

    def get_message_head_info(self, session_id, item_id, session_type=1):
        """Return the platform-owned order header for a chat session and item."""
        session_id = self._validate_session_id(session_id)
        item_id = self._validate_item_id(str(item_id))
        if type(session_type) is not int or session_type not in {1, 2}:
            raise ValueError("session_type is invalid")
        return self._signed_mtop_request(
            "mtop.idle.trade.pc.message.headinfo",
            "https://h5api.m.goofish.com/h5/mtop.idle.trade.pc.message.headinfo/1.0/",
            {
                "itemId": item_id,
                "sessionId": int(session_id),
                "sessionType": session_type,
            },
            attempts=self.trade_max_attempts,
            value_type="string",
        )

    def get_order_detail(self, order_id):
        """Return seller-visible order detail for a platform order ID."""
        order_id = self._validate_order_id(order_id)
        return self._signed_mtop_request(
            "mtop.idle.web.trade.order.detail",
            "https://h5api.m.goofish.com/h5/mtop.idle.web.trade.order.detail/1.0/",
            {"tid": order_id},
            attempts=self.trade_max_attempts,
            value_type="string",
        )

    def consign_dummy(self, order_id):
        """无需邮寄发货:把待发货订单在平台上标记为已发货(虚拟物流)。"""
        order_id = self._validate_order_id(order_id)
        return self._signed_mtop_request(
            "mtop.taobao.idle.logistic.consign.dummy",
            "https://h5api.m.goofish.com/h5/mtop.taobao.idle.logistic.consign.dummy/1.0/",
            {"orderId": order_id},
            attempts=self.trade_max_attempts,
            value_type="string",
        )

    def upload_media(self, media_path):
        """Upload one seller image and return the bounded platform response."""
        if not isinstance(media_path, str) or not media_path or len(media_path) > 4096:
            raise ValueError("media_path is invalid")
        if not os.path.isfile(media_path):
            raise FileNotFoundError(media_path)
        headers = browser_headers()
        headers["accept"] = "*/*"
        extension = os.path.splitext(media_path)[1].lower()
        media_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(extension)
        if not media_type:
            raise ValueError("media_path extension is invalid")
        params = {"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}
        with self._session_lock:
            with open(media_path, "rb") as handle:
                response = self.session.post(
                    "https://stream-upload.goofish.com/api/upload.api",
                    headers=headers,
                    params=params,
                    files={"file": (os.path.basename(media_path), handle, media_type)},
                    timeout=self.request_timeout,
                )
                response.raise_for_status()
                result = response.json()
        if not isinstance(result, dict):
            raise XianyuApiError("response_invalid")
        return result

    def update_cookies(self, cookies):
        with self._session_lock:
            self.session.cookies.update(cookies)

    def cookie_header_snapshot(self):
        """Return a thread-safe snapshot of all current in-memory cookies."""
        with self._session_lock:
            latest = {}
            for cookie in self.session.cookies:
                name = str(cookie.name)
                value = str(cookie.value)
                if not name or any(character in name for character in "\r\n;="):
                    continue
                if any(character in value for character in "\r\n;"):
                    continue
                latest[name] = value
            return "; ".join(
                f"{name}={value}" for name, value in latest.items()
            )

    def clear_duplicate_cookies(self):
        """清理重复的cookies"""
        with self._session_lock:
            new_jar = requests.cookies.RequestsCookieJar()
            added_cookies = set()
            cookie_list = list(self.session.cookies)
            cookie_list.reverse()
            for cookie in cookie_list:
                if cookie.name not in added_cookies:
                    new_jar.set_cookie(cookie)
                    added_cookies.add(cookie.name)
            self.session.cookies = new_jar
        # 注意：不写回 .env！运行中轮换的 cookie（_m_h5_tk 等）只用于当前会话，
        # 写回会覆盖人工维护的完整 cookie（含 HttpOnly unb/sgcookie），导致重启后失效。

    def hasLogin(self, device_id, retry_count=0):
        """使用同一稳定设备 ID 执行一次登录态恢复探测。"""
        if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
            raise ValueError("retry_count is invalid")
        if retry_count:
            return False
        self._validate_device_id(device_id)

        url = 'https://passport.goofish.com/newlogin/hasLogin.do'
        params = {'appName': 'xianyu', 'fromSite': '77'}
        with self._session_lock:
            data = {
                'hid': self.session.cookies.get('unb', ''),
                'ltl': 'true',
                'appName': 'xianyu',
                'appEntrance': 'web',
                '_csrf_token': self.session.cookies.get('XSRF-TOKEN', ''),
                'umidToken': '',
                'hsiz': self.session.cookies.get('cookie2', ''),
                'bizParams': 'taobaoBizLoginFrom=web',
                'mainPage': 'false',
                'isMobile': 'false',
                'lang': 'zh_CN',
                'returnUrl': '',
                'fromSite': '77',
                'isIframe': 'true',
                'documentReferer': browser_headers()['referer'],
                'defaultView': 'hasLogin',
                'umidTag': 'SERVER',
                # 与 WebSocket/Token 使用同一稳定账号设备身份；不再混用 cna。
                'deviceId': device_id,
            }
            response, res_json = self._post_json(
                url,
                headers=browser_headers(content_type="application/x-www-form-urlencoded"),
                params=params,
                data=data,
            )
        content = res_json.get('content')
        if isinstance(content, dict) and content.get('success') is True:
            self.clear_duplicate_cookies()
            logger.debug("Login恢复成功")
            return True
        error_code = self._response_error_code(
            res_json, getattr(response, "status_code", None)
        )
        if error_code == "risk_control":
            raise XianyuAuthenticationError("risk_control")
        if error_code in {"platform_busy", "account_restricted"}:
            raise XianyuApiError(error_code)
        logger.warning("Login恢复未确认有效")
        return False

    def _single_token_request(self, device_id, data_val):
        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time() * 1000)),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idlemessage.pc.login.token',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
            'spm_pre': 'a21ybx.item.want.1.14ad3da6ALVq3n',
            'log_id': '14ad3da6ALVq3n',
        }
        with self._session_lock:
            token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
            params['sign'] = generate_sign(params['t'], token, data_val)
            response, payload = self._post_json(
                self.url,
                headers=browser_headers(content_type="application/x-www-form-urlencoded"),
                params=params,
                data={'data': data_val},
            )
        if self._is_success_response(payload):
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("accessToken"), str) or not data.get("accessToken"):
                raise XianyuApiError("token_unavailable")
            self.clear_duplicate_cookies()
            return payload
        error_code = self._response_error_code(
            payload, getattr(response, "status_code", None)
        )
        if error_code in XianyuAuthenticationError.ALLOWED_CODES:
            raise XianyuAuthenticationError(error_code)
        raise XianyuApiError(error_code)

    def get_token(self, device_id, retry_count=0):
        """每个调度轮次一次 Token 请求；仅 Session 恢复后允许再请求一次。"""
        self._validate_device_id(device_id)
        if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
            raise ValueError("retry_count is invalid")
        if retry_count:
            raise XianyuApiError("token_unavailable")
        data_val = json.dumps(
            {
                "appKey": "444e9908a51d1cb236a27862abc769c9",
                "deviceId": device_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            result = self._single_token_request(device_id, data_val)
        except XianyuAuthenticationError as exc:
            if exc.code != "session_expired":
                logger.error("Token请求需要人工处理 code={}", exc.code)
                raise
            logger.warning("Token会话过期，执行本轮唯一一次Session恢复")
            if not self.hasLogin(device_id=device_id):
                raise XianyuAuthenticationError("session_expired") from None
            try:
                result = self._single_token_request(device_id, data_val)
            except XianyuAuthenticationError as retry_exc:
                if retry_exc.code == "session_expired":
                    logger.error("Session恢复后Token仍过期")
                raise
        logger.info("Token获取成功")
        return result

    def get_item_info(self, item_id, retry_count=0):
        """获取商品信息，自动处理token失效的情况"""
        self._validate_item_id(item_id)
        if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
            raise ValueError("retry_count is invalid")
        start_attempt = retry_count
        if start_attempt >= self.item_max_attempts:
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}

        data_val = json.dumps(
            {"itemId": item_id}, ensure_ascii=True, separators=(",", ":")
        )
        data = {'data': data_val}

        for attempt in range(start_attempt, self.item_max_attempts):
            params = {
                'jsv': '2.7.2',
                'appKey': '34839810',
                't': str(int(time.time()) * 1000),
                'sign': '',
                'v': '1.0',
                'type': 'originaljson',
                'accountSite': 'xianyu',
                'dataType': 'json',
                'timeout': '20000',
                'api': 'mtop.taobao.idle.pc.detail',
                'sessionOption': 'AutoLoginOnly',
                'spm_cnt': 'a21ybx.im.0.0',
            }
            try:
                with self._session_lock:
                    token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
                    params['sign'] = generate_sign(params['t'], token, data_val)
                    response = self.session.post(
                        'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/',
                        params=params,
                        data=data,
                        timeout=self.request_timeout,
                    )
                    res_json = response.json()
            except Exception as exc:
                logger.error(f"商品信息API请求异常: {type(exc).__name__}")
                self._wait_before_retry(attempt, self.item_max_attempts, self.request_backoff)
                continue

            if not isinstance(res_json, dict):
                logger.error("商品信息API返回格式异常")
                self._wait_before_retry(attempt, self.item_max_attempts, self.request_backoff)
                continue

            ret_value = res_json.get('ret', [])
            if isinstance(ret_value, str):
                ret_value = [ret_value]
            ret_text = [str(ret) for ret in ret_value]
            if any('SUCCESS::调用成功' in ret for ret in ret_text):
                item_ref = hashlib.sha256(str(item_id).encode("utf-8")).hexdigest()[:10]
                logger.debug("商品信息获取成功 item={}", item_ref)
                return res_json
            if any('RGV587_ERROR' in ret or '被挤爆啦' in ret for ret in ret_text):
                logger.error("商品信息API触发平台风控")
                raise XianyuAuthenticationError("risk_control")
            if any('FAIL_SYS_SESSION_EXPIRED' in ret for ret in ret_text):
                logger.error("商品信息API会话已过期")
                raise XianyuAuthenticationError("session_expired")

            logger.warning("商品信息API调用失败")
            if 'Set-Cookie' in response.headers:
                logger.debug("检测到Set-Cookie，更新当前会话cookie")
                self.clear_duplicate_cookies()
            self._wait_before_retry(attempt, self.item_max_attempts, self.request_backoff)

        logger.error("获取商品信息失败，已用完尝试预算")
        return {"error": "获取商品信息失败，重试次数过多"}
