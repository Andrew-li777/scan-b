from diskcache import Cache

from src.config import PROJECT_ROOT

_cache_dir = PROJECT_ROOT / ".cache"
_cache_dir.mkdir(exist_ok=True)

cache = Cache(str(_cache_dir))
