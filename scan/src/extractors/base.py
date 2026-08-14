import re
from abc import ABC, abstractmethod
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from src.models import VideoMeta

_TRACKING_PARAMS = {
    "vd_source", "spm_id_from", "share_source", "share_medium",  # Bilibili
    "si", "feature", "pp",                                         # YouTube
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",  # 通用 UTM
}


def clean_url(url: str) -> str:
    """Strip tracking/analytics query params, return clean canonical URL."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned = {k: v for k, v in params.items() if k not in _TRACKING_PARAMS}
    new_query = urlencode(cleaned, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


class BaseExtractor(ABC):
    platform: str = ""

    @abstractmethod
    def match(self, url: str) -> bool: ...

    @abstractmethod
    def extract(self, url: str) -> VideoMeta: ...
