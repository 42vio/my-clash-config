from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from clash_sub.models import SubscriptionUserinfo


class TrafficError(RuntimeError):
    """Raised when subscription traffic metadata is missing or invalid."""


def parse_subscription_userinfo(value: str) -> SubscriptionUserinfo:
    fields = {}
    for item in value.split(";"):
        key, separator, raw_value = item.strip().partition("=")
        if separator != "=" or key in fields:
            raise TrafficError("invalid subscription metadata")
        if key not in {"upload", "download", "total", "expire"}:
            raise TrafficError("unknown subscription metadata field")
        if not raw_value.isdecimal():
            raise TrafficError("subscription metadata must be non-negative")
        fields[key] = int(raw_value, 10)
    if set(fields) != {"upload", "download", "total", "expire"}:
        raise TrafficError("incomplete subscription metadata")
    return SubscriptionUserinfo(**fields)


class TrafficClient:
    def __init__(self, opener=urlopen, timeout: int = 10) -> None:
        self.opener = opener
        self.timeout = timeout

    def fetch(self, source_url: str) -> Optional[SubscriptionUserinfo]:
        request = Request(source_url, headers={"Accept": "*/*"})
        try:
            with self.opener(request, timeout=self.timeout) as response:
                value = response.headers.get("Subscription-Userinfo")
        except (OSError, HTTPError, URLError) as exc:
            raise TrafficError("subscription metadata fetch failed") from exc
        if value is None:
            return None
        return parse_subscription_userinfo(value)
