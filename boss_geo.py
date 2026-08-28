"""BOSS 直聘地区/城市/区数据本地代理。

数据来源：BOSS 公开 API（参考 https://www.zhipin.com/wapi/zpCommon/data/city.json）。
结果在内存中按需缓存，进程重启不持久化；如需持久化可在 SQLite 加 geo_cache 表。
调用失败时返回空 dict / 空 list，调用方需做兜底。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

CITY_URL = "https://www.zhipin.com/wapi/zpCommon/data/city.json"
DISTRICT_URL = "https://www.zhipin.com/wapi/zpgeek/businessDistrict.json"

_cache: Dict[str, Any] = {
    "cities_ts": 0.0,  # 上次拉城市列表时间
    "cities": [],  # List[{name, code}]
    "city_by_name": {},  # name -> code
    "city_by_code": {},  # code -> name
    "districts_ts": {},  # city_code -> 上次拉取时间
    "districts": {},  # city_code -> List[{name, code}]
    "district_by_name": {},  # city_code -> {name: code}
    "district_by_code": {},  # city_code -> {code: name}
    "ttl_sec": 6 * 3600,  # 缓存 6 小时
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.zhipin.com/web/geek/job",
}

# 静态城市码兜底（BOSS API 不可用时回退）
_CITY_MAP_FALLBACK: Dict[str, str] = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "成都": "101270100",
    "杭州": "101210100",
    "武汉": "101200100",
    "南京": "101190100",
    "重庆": "101040100",
    "西安": "101110100",
    "长沙": "101250100",
    "天津": "101030100",
    "苏州": "101190400",
    "郑州": "101180100",
    "东莞": "101281600",
    "沈阳": "101070100",
    "宁波": "101210400",
    "昆明": "101290100",
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "南昌": "101240100",
    "贵阳": "101260100",
    "南宁": "101300100",
    "太原": "101100100",
    "石家庄": "101090100",
    "哈尔滨": "101050100",
    "长春": "101060100",
    "兰州": "101160100",
    "乌鲁木齐": "101130100",
    "呼和浩特": "101080100",
    "拉萨": "101140100",
    "西宁": "101150100",
    "银川": "101170100",
    "海口": "101310100",
    "三亚": "101310200",
    "济南": "101120100",
    "青岛": "101120200",
    "淄博": "101120300",
    "德州": "101120400",
    "烟台": "101120500",
    "潍坊": "101120600",
    "济宁": "101120700",
    "泰安": "101120800",
    "临沂": "101120900",
    "菏泽": "101121000",
    "滨州": "101121100",
    "东营": "101121200",
    "威海": "101121300",
    "枣庄": "101121400",
    "日照": "101121500",
    "聊城": "101121700",
    "全国": "100010000",
}


def _http_json(
    url: str,
    params: Optional[dict] = None,
    timeout: float = 12.0,
    cookie_str: str = "",
    extra_headers: Optional[dict] = None,
) -> Optional[dict]:
    try:
        h = dict(_HEADERS)
        if cookie_str:
            h["Cookie"] = cookie_str
        if extra_headers:
            h.update(extra_headers)
        with httpx.Client(timeout=timeout, follow_redirects=True) as cli:
            r = cli.get(url, params=params or {}, headers=h)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _parse_cities(data: dict) -> Tuple[List[dict], Dict[str, str], Dict[str, str]]:
    cities: List[dict] = []
    by_name: Dict[str, str] = {}
    by_code: Dict[str, str] = {}
    seen_codes: set = set()

    def add_city(node: Any) -> None:
        if not isinstance(node, dict):
            return
        code = str(node.get("code") or "")
        name = node.get("name")
        if not (code and name) or code in seen_codes:
            return
        seen_codes.add(code)
        cities.append({"name": name, "code": code})
        by_name.setdefault(name, code)
        by_code.setdefault(code, name)

    root = data.get("zpData") or data.get("data") or {}
    for top in root.get("hotCityList") or []:
        add_city(top)
    for province in root.get("cityList") or []:
        if not isinstance(province, dict):
            continue
        sub = province.get("subLevelModelList")
        if isinstance(sub, list) and sub:
            for city in sub:
                add_city(city)
        else:
            add_city(province)
    return cities, by_name, by_code


def get_cities(force: bool = False, cookie_str: str = "") -> List[dict]:
    now = time.time()
    if not force and _cache["cities"] and (now - _cache["cities_ts"]) < _cache["ttl_sec"]:
        return _cache["cities"]

    raw = _http_json(CITY_URL, cookie_str=cookie_str)
    cities: List[dict] = []
    by_name: Dict[str, str] = {}
    by_code: Dict[str, str] = {}
    if raw and raw.get("code") == 0:
        cities, by_name, by_code = _parse_cities(raw)

    if not cities:
        if _cache["cities"]:
            return _cache["cities"]
        cities = [{"name": n, "code": c} for n, c in _CITY_MAP_FALLBACK.items()]
        by_name = dict(_CITY_MAP_FALLBACK)
        by_code = {c: n for n, c in _CITY_MAP_FALLBACK.items()}

    _cache["cities"] = cities
    _cache["city_by_name"] = by_name
    _cache["city_by_code"] = by_code
    _cache["cities_ts"] = now
    return cities


def resolve_city_code(name_or_code: str, cookie_str: str = "") -> Optional[str]:
    """把城市名（如"广州"）或 city code（"101280100"）解析为 code。BOSS API 失败时回退静态表。"""
    if not name_or_code:
        return None
    s = str(name_or_code).strip()
    if not s:
        return None
    if s.isdigit() and s in _cache["city_by_code"]:
        return s
    if not _cache["city_by_name"]:
        get_cities(cookie_str=cookie_str)
    if s in _cache["city_by_name"]:
        return _cache["city_by_name"][s]
    # 宽松匹配：去掉"市"后查
    if s.endswith("市") and s[:-1] in _cache["city_by_name"]:
        return _cache["city_by_name"][s[:-1]]
    # 兼容旧 city_code 直传
    if s in _cache["city_by_code"]:
        return s
    # 静态兜底
    if s in _CITY_MAP_FALLBACK:
        return _CITY_MAP_FALLBACK[s]
    if s.endswith("市") and s[:-1] in _CITY_MAP_FALLBACK:
        return _CITY_MAP_FALLBACK[s[:-1]]
    return None


def _parse_districts(data: dict) -> Tuple[List[dict], Dict[str, str], Dict[str, str]]:
    """解析 BOSS 区域接口返回。

    返回结构包含「区/县级」和「三级商圈」两级：
      - 区/县级：subLevelModelList 的第一层（如"增城区" → 440118）
      - 商圈级：各区下的 subLevelModelList（如"荔城" → 2410）
    multiBusinessDistrict 参数可以接收任意层级的 code。
    """
    districts: List[dict] = []
    by_name: Dict[str, str] = {}
    by_code: Dict[str, str] = {}

    bd = (data.get("zpData") or {}).get("businessDistrict") or {}
    for sub in bd.get("subLevelModelList") or []:
        if not isinstance(sub, dict):
            continue
        code = str(sub.get("code") or "")
        name = sub.get("name")
        parent = sub.get("parent") or ""
        if code and name:
            entry = {"name": name, "code": code}
            if parent:
                entry["parent"] = str(parent)
            districts.append(entry)
            by_name.setdefault(name, code)
            by_code.setdefault(code, name)

            # 展开三级商圈（区下面的商圈/街道）
            for child in sub.get("subLevelModelList") or []:
                if not isinstance(child, dict):
                    continue
                child_code = str(child.get("code") or "")
                child_name = child.get("name")
                if child_code and child_name:
                    child_entry = {"name": child_name, "code": child_code, "parent": code}
                    districts.append(child_entry)
                    by_name.setdefault(child_name, child_code)
                    by_code.setdefault(child_code, child_name)

    return districts, by_name, by_code


# ── 「工作区域」缓存（按 city_code 收集，从 search 结果中汇总）──
# BOSS 实际把「工作区域」和「商圈」合并到同一个 multiBusinessDistrict 参数里。
# 6 位 code（如 370305=临淄区）是行政区域，4-8 位 code（如 2410=彩虹）是商圈。
# get_areas() 返回该城市下所有 6 位的行政区域。
_area_cache: Dict[str, Dict[str, Any]] = {}
_AREA_TTL = 6 * 3600


def get_areas(city_code: str, cookie_str: str = "", extra_headers: Optional[dict] = None) -> List[dict]:
    """取某城市下的「工作区域」列表（BOSS 行政区域，6 位 code）。
    优先用缓存;没有则主动拉 districts 缓存并筛 6 位 code 部分。
    """
    if not city_code:
        return []
    entry = _area_cache.get(city_code)
    if entry and (time.time() - entry.get("ts", 0)) < _AREA_TTL and entry.get("items"):
        return entry["items"]
    # 主动拉一次（会写入 districts 缓存）
    districts = get_districts(city_code, cookie_str=cookie_str, extra_headers=extra_headers)
    areas = [d for d in districts if d["code"].isdigit() and len(d["code"]) == 6]
    if areas:
        _area_cache[city_code] = {"items": areas, "ts": time.time()}
    return areas


def collect_areas_from_jobs(city_code: str, jobs: list) -> None:
    """从搜索结果中收集「工作区域」,存入缓存供后续 UI 使用。
    job 中需有 areaDistrict 字段（BOSS 接口原始字段,值是区名）。
    """
    if not city_code or not jobs:
        return
    items: Dict[str, dict] = {}
    for j in jobs:
        name = (j.get("areaDistrict") or "").strip()
        if not name:
            continue
        if name not in items:
            items[name] = {"name": name, "code": name}
    if not items:
        return
    existing = _area_cache.get(city_code, {}).get("items", [])
    name_set = {x["name"] for x in existing}
    for it in items.values():
        if it["name"] not in name_set:
            existing.append(it)
            name_set.add(it["name"])
    _area_cache[city_code] = {"items": existing, "ts": time.time()}


def resolve_area_code(city_code: str, name_or_code: str) -> Optional[str]:
    """把工作区域名或 code 解析为可用的过滤值。
    BOSS 的 multiBusinessDistrict 用 6 位行政区域 code（如 370305）。
    """
    if not name_or_code:
        return None
    s = str(name_or_code).strip()
    # 已经是 6 位数字 code
    if s.isdigit() and len(s) == 6:
        return s
    # 已经是任意长度数字 code
    if s.isdigit() and len(s) >= 3:
        return s
    # 按名字查
    areas = get_areas(city_code)
    for a in areas:
        if a["name"] == s:
            return a["code"]
    return s  # 兜底：原样返回


def get_districts(
    city_code_or_name: str, force: bool = False, cookie_str: str = "", extra_headers: Optional[dict] = None
) -> List[dict]:
    """取某城市下的区/商圈列表。传城市名或 city code 都行。"""
    city_code = resolve_city_code(city_code_or_name)
    if not city_code:
        return []

    now = time.time()
    cached = _cache["districts"].get(city_code)
    ts = _cache["districts_ts"].get(city_code, 0)
    if not force and cached and (now - ts) < _cache["ttl_sec"]:
        return cached

    raw = _http_json(DISTRICT_URL, params={"cityCode": city_code}, cookie_str=cookie_str, extra_headers=extra_headers)
    if not raw or raw.get("code") != 0:
        return cached or []

    districts, by_name, by_code = _parse_districts(raw)
    _cache["districts"][city_code] = districts
    _cache["districts_ts"][city_code] = now
    _cache["district_by_name"][city_code] = by_name
    _cache["district_by_code"][city_code] = by_code
    return districts


def resolve_district_code(city_code_or_name: str, district_name_or_code: str) -> Optional[str]:
    """把区/商圈名（如"张店区"）或区 code（"440118"）解析为 code。"""
    if not district_name_or_code:
        return None
    s = str(district_name_or_code).strip()
    if not s:
        return None
    # 已是 code（区级 6 位，商圈级 3-8 位）
    if s.isdigit() and len(s) >= 3:
        return s
    city_code = resolve_city_code(city_code_or_name)
    if not city_code:
        return None
    if not _cache["district_by_name"].get(city_code):
        get_districts(city_code)
    by_name = _cache["district_by_name"].get(city_code, {})
    if s in by_name:
        return by_name[s]
    # 去掉"区"字后查
    for suffix in ("区", "县", "市"):
        if s.endswith(suffix) and s[: -len(suffix)] in by_name:
            return by_name[s[: -len(suffix)]]
    # 模糊匹配
    for name, code in by_name.items():
        if name in s or s in name:
            return code
    return None
