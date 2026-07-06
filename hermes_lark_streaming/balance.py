"""DeepSeek 余额查询（缓存5分钟）。"""
import time
from urllib.request import Request, urlopen
import json as _json
import os

_cache = {"balance": None, "time": 0}


def _get_deepseek_balance() -> float | None:
    now = time.time()
    if now - _cache["time"] < 300:
        return _cache["balance"]
    try:
        api_key = os.environ.get("DEEPSEEK_API_KEY") or ""
        if not api_key:
            env_path = os.path.expanduser("~/.hermes/.env")
            if os.path.exists(env_path):
                for line in open(env_path):
                    import re as _re
                    m = _re.search(r'^DEEPSEEK_API_KEY=(.+)', line)
                    if m:
                        parts = line.strip().split("=", 1)
                        if len(parts) >= 2:
                            api_key = parts[1].strip().strip("\"'")
                        break
        if not api_key:
            return None
        req = Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp = urlopen(req, timeout=10)
        data = _json.loads(resp.read().decode())
        bal = data.get("balance")
        if isinstance(bal, (int, float)):
            _cache["balance"] = round(float(bal), 2)
            _cache["time"] = now
            return _cache["balance"]
        infos = data.get("balance_infos", [])
        if infos:
            total = float(infos[0].get("total_balance", 0))
            _cache["balance"] = round(total, 2)
            _cache["time"] = now
            return _cache["balance"]
    except Exception:
        pass
    return None
