"""Stable, non-secret browser fingerprint for Xianyu platform requests."""

from __future__ import annotations


CHROME_MAJOR = "133"
CHROME_VERSION = f"{CHROME_MAJOR}.0.0.0"
PLATFORM = "Windows"
ORIGIN = "https://www.goofish.com"
REFERER = f"{ORIGIN}/"
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VERSION} Safari/537.36"
)
SEC_CH_UA = (
    f'"Not(A:Brand";v="99", "Google Chrome";v="{CHROME_MAJOR}", '
    f'"Chromium";v="{CHROME_MAJOR}"'
)


def browser_headers() -> dict[str, str]:
    """Return one consistent browser identity without account-specific data."""
    return {
        "User-Agent": USER_AGENT,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{PLATFORM}"',
        "Accept-Language": ACCEPT_LANGUAGE,
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
