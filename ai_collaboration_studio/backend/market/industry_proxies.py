from __future__ import annotations

import copy
import csv
import io
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


FRED_HOST = "fred.stlouisfed.org"
FRED_CSV_ENDPOINT = f"https://{FRED_HOST}/graph/fredgraph.csv"
MAX_RESPONSE_BYTES = 512_000

INDUSTRY_PROXY_SERIES: dict[str, dict[str, str]] = {
    "U34BVS": {
        "label": "美国计算机存储设备制造商出货额",
        "units": "百万美元",
        "frequency": "monthly",
        "scope": "device",
        "source_agency": "U.S. Census Bureau M3",
    },
    "U34BTI": {
        "label": "美国计算机存储设备制造商总库存",
        "units": "百万美元",
        "frequency": "monthly",
        "scope": "device",
        "source_agency": "U.S. Census Bureau M3",
    },
    "PCU3341123341121": {
        "label": "美国计算机存储设备生产者价格指数",
        "units": "指数 Dec 2004=100",
        "frequency": "monthly",
        "scope": "device",
        "source_agency": "U.S. Bureau of Labor Statistics PPI",
    },
    "PCU334413334413": {
        "label": "美国半导体及相关器件生产者价格指数",
        "units": "指数 Dec 1998=100",
        "frequency": "monthly",
        "scope": "broad_semiconductor",
        "source_agency": "U.S. Bureau of Labor Statistics PPI",
    },
    "CAPUTLG3344S": {
        "label": "美国半导体及其他电子元件产能利用率",
        "units": "百分比",
        "frequency": "monthly",
        "scope": "broad_semiconductor",
        "source_agency": "Federal Reserve Board G.17",
    },
}


def _percent_change(current: float, previous: float | None) -> float | None:
    if previous is None or previous == 0:
        return None
    return round((current / previous - 1) * 100, 4)


class FredIndustryProxyAdapter:
    def __init__(
        self,
        *,
        fetch_bytes: Callable[[str], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        cache_ttl_seconds: int = 21_600,
    ) -> None:
        self._fetch_bytes = fetch_bytes or self._default_fetch_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.cache_ttl_seconds = max(300, int(cache_ttl_seconds))
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.RLock()

    def status(self) -> dict[str, Any]:
        return {
            "source": "fred_official_public_series",
            "state": "available",
            "configured": True,
            "series_count": len(INDUSTRY_PROXY_SERIES),
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        with self._lock:
            if self._cache and self._cache[0] > now_monotonic and not force:
                payload = copy.deepcopy(self._cache[1])
                payload["cache"] = {"hit": True, "ttl_seconds": self.cache_ttl_seconds}
                return payload

            payload = self._build_snapshot()
            self._cache = (now_monotonic + self.cache_ttl_seconds, copy.deepcopy(payload))
            payload["cache"] = {"hit": False, "ttl_seconds": self.cache_ttl_seconds}
            return payload

    def _build_snapshot(self) -> dict[str, Any]:
        now = self._clock().astimezone(timezone.utc)
        rows: list[dict[str, Any]] = []
        source_errors: list[dict[str, str]] = []
        for series_id, definition in INDUSTRY_PROXY_SERIES.items():
            try:
                observations = self._read_series(series_id, now)
            except Exception as exc:
                source_errors.append({
                    "source": "fred",
                    "series_id": series_id,
                    "code": "FRED_SERIES_ERROR",
                    "message": str(exc)[:300],
                })
                continue
            if not observations:
                source_errors.append({
                    "source": "fred",
                    "series_id": series_id,
                    "code": "FRED_SERIES_EMPTY",
                    "message": "官方序列当前没有有效的非未来观测值",
                })
                continue
            latest = observations[-1]
            current_value = latest["value"]
            rows.append({
                "series_id": series_id,
                **definition,
                "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
                "as_of": latest["date"],
                "latest": current_value,
                "change_1_observation_pct": _percent_change(current_value, observations[-2]["value"] if len(observations) >= 2 else None),
                "change_3_observations_pct": _percent_change(current_value, observations[-4]["value"] if len(observations) >= 4 else None),
                "change_12_observations_pct": _percent_change(current_value, observations[-13]["value"] if len(observations) >= 13 else None),
                "observations": observations[-24:],
            })

        derived = self._derive_inventory_ratio(rows)
        return {
            "ok": bool(rows),
            "state": "ready" if len(rows) == len(INDUSTRY_PROXY_SERIES) else "degraded" if rows else "offline",
            "source": "fred_official_public_series",
            "captured_at": now.isoformat().replace("+00:00", "Z"),
            "rows": rows,
            "derived": derived,
            "source_errors": source_errors,
            "interpretation": (
                "这些是美国官方的月度行业代理，不是 DRAM/NAND/HDD 即时报价。"
                "NAICS 334112 偏向存储设备，NAICS 3344 覆盖更广泛半导体与电子元件；"
                "不得把代理变化直接解释为任一公司的供需、盈利或交易方向。"
            ),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def _read_series(self, series_id: str, now: datetime) -> list[dict[str, Any]]:
        if series_id not in INDUSTRY_PROXY_SERIES:
            raise ValueError("FRED 序列不在固定白名单")
        url = f"{FRED_CSV_ENDPOINT}?{urlencode({'id': series_id})}"
        raw = self._fetch_bytes(url)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("FRED CSV 超过大小上限")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("FRED CSV 不是有效 UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames != ["observation_date", series_id]:
            raise ValueError("FRED CSV 表头与请求序列不匹配")
        observations = []
        for raw_row in reader:
            try:
                observed_at = datetime.strptime(raw_row.get("observation_date") or "", "%Y-%m-%d").replace(tzinfo=timezone.utc)
                value = float(raw_row.get(series_id) or "")
            except (TypeError, ValueError):
                continue
            if observed_at > now or not math.isfinite(value):
                continue
            observations.append({"date": observed_at.date().isoformat(), "value": value})
        observations.sort(key=lambda item: item["date"])
        return observations

    @staticmethod
    def _derive_inventory_ratio(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {row["series_id"]: row for row in rows}
        shipments = by_id.get("U34BVS")
        inventories = by_id.get("U34BTI")
        if not shipments or not inventories:
            return []
        shipments_by_date = {item["date"]: item["value"] for item in shipments["observations"]}
        common = [
            {"date": item["date"], "value": round(item["value"] / shipments_by_date[item["date"]], 4)}
            for item in inventories["observations"]
            if shipments_by_date.get(item["date"], 0) > 0
        ]
        if not common:
            return []
        latest = common[-1]
        return [{
            "metric_id": "storage_device_inventory_to_shipments",
            "label": "美国存储设备库存/当月出货额",
            "units": "倍",
            "scope": "device",
            "as_of": latest["date"],
            "latest": latest["value"],
            "change_12_observations_pct": _percent_change(latest["value"], common[-13]["value"] if len(common) >= 13 else None),
            "observations": common,
            "interpretation": "比值上升可能来自库存增加或当月出货下降；未季调月度数据存在季节性，不能单独判定去库或补库。",
        }]

    @staticmethod
    def _default_fetch_bytes(url: str) -> bytes:
        parsed = urlparse(url)
        query = parse_qs(parsed.query, strict_parsing=True)
        series_ids = query.get("id") or []
        if (
            parsed.scheme != "https"
            or parsed.hostname != FRED_HOST
            or parsed.path != "/graph/fredgraph.csv"
            or set(query) != {"id"}
            or len(series_ids) != 1
            or series_ids[0] not in INDUSTRY_PROXY_SERIES
        ):
            raise ValueError("只允许固定 FRED 官方 CSV 序列")
        request = Request(url, headers={"User-Agent": "AI-Collaboration-Studio/0.1 read-only-research"})
        with urlopen(request, timeout=15) as response:
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != FRED_HOST or final.path != "/graph/fredgraph.csv":
                raise ValueError("FRED 请求重定向到非官方端点")
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "csv" not in content_type and "text/plain" not in content_type:
                raise ValueError("FRED 返回了非 CSV 内容")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("FRED CSV 超过大小上限")
        return raw
