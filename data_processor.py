import os
import re
import json
import math
import time
import requests
import pandas as pd
from shapely.geometry import Point, shape
from dotenv import load_dotenv

load_dotenv()

EV_FILE       = os.path.join(os.path.dirname(__file__), "서울시 자치구 읍면동별 연료별 자동차 등록현황(행정동)(26년4월).xlsx")
CHARGER_EXCEL = os.path.join(os.path.dirname(__file__), "전기차 충전소 설치현황_20260512.xlsx")
CHARGER_CACHE = os.path.join(os.path.dirname(__file__), "data", "seoul_chargers_api.json")
CHARGER_CSV   = os.path.join(os.path.dirname(__file__), "data", "seoul_chargers_by_gu.csv")
GEOJSON_URL   = (
    "https://raw.githubusercontent.com/southkorea/seoul-maps/master/"
    "kostat/2013/json/seoul_municipalities_geo_simple.json"
)
GEOJSON_CACHE = os.path.join(os.path.dirname(__file__), "data", "seoul_gu.geojson")

# 행정동 경계 (GPS→동 매핑용)
DONG_GEOJSON_URL   = (
    "https://raw.githubusercontent.com/vuski/admdongkor/master/"
    "ver20230701/HangJeongDong_ver20230701.geojson"
)
DONG_GEOJSON_CACHE = os.path.join(os.path.dirname(__file__), "data", "seoul_dong.geojson")
DONG_LOOKUP_CACHE  = os.path.join(os.path.dirname(__file__), "data", "charger_dong_lookup.json")

API_KEY  = os.environ.get("EV_API_KEY", "")
BASE_URL = "http://apis.data.go.kr/B552584/EvCharger/getChargerInfo"

# 완속 타입 코드
SLOW_TYPES = {"02", "07"}

# 정규식 사전 컴파일 (load_charger_data 74,804회 반복 호출 최적화)
_RE_GU      = re.compile(r"([가-힣]+구)")
_RE_DONG    = re.compile(r"([가-힣0-9]+[동읍면리])")
_RE_GU_DONG = re.compile(r"([가-힣]+구)\s+([가-힣0-9]+[동읍면리])")
# 점(.) 포함 복합 행정동 지원: "금호2.3가동", "종로1.2.3.4가동" 등
_RE_DONG_EXT = re.compile(r"([가-힣][가-힣0-9.·]+[동읍면리])")

# 서울시 25개 자치구 (비서울 오탐 방지)
SEOUL_GU = {
    "강남구","강동구","강북구","강서구","관악구","광진구","구로구","금천구",
    "노원구","도봉구","동대문구","동작구","마포구","서대문구","서초구",
    "성동구","성북구","송파구","양천구","영등포구","용산구","은평구",
    "종로구","중구","중랑구",
}


# ─── Haversine ───────────────────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ─── 행정동 경계 폴리곤 (GPS → 동 매핑) ──────────────────────────────────────
def get_seoul_dong_polygons():
    """서울시 행정동 경계 GeoJSON 로드/캐시. [(gu, dong, shapely_polygon), ...]"""
    os.makedirs(os.path.dirname(DONG_GEOJSON_CACHE), exist_ok=True)

    if not os.path.exists(DONG_GEOJSON_CACHE):
        try:
            resp = requests.get(DONG_GEOJSON_URL, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            # 서울 행정동만 필터
            seoul_feats = [
                f for f in data.get("features", [])
                if f.get("properties", {}).get("adm_nm", "").startswith("서울")
            ]
            seoul_data = {"type": "FeatureCollection", "features": seoul_feats}
            with open(DONG_GEOJSON_CACHE, "w", encoding="utf-8") as f:
                json.dump(seoul_data, f, ensure_ascii=False)
        except Exception:
            return []

    with open(DONG_GEOJSON_CACHE, encoding="utf-8") as f:
        data = json.load(f)

    polygons = []
    for feat in data.get("features", []):
        props  = feat.get("properties", {})
        adm_nm = props.get("adm_nm", "")          # 예: "서울특별시 강남구 대치1동"
        parts  = adm_nm.split()
        if len(parts) < 3:
            continue
        gu   = parts[1]   # "강남구"
        dong = parts[2]   # "대치1동"
        if gu not in SEOUL_GU:
            continue
        try:
            polygons.append((gu, dong, shape(feat["geometry"])))
        except Exception:
            continue
    return polygons


def build_dong_lookup(charger_df: pd.DataFrame) -> dict:
    """stat_id → (gu, dong) 매핑 테이블 생성 (GPS 기반). 캐시 사용."""
    os.makedirs(os.path.dirname(DONG_LOOKUP_CACHE), exist_ok=True)

    # 캐시 유효성: charger 캐시보다 최신이면 재사용
    if os.path.exists(DONG_LOOKUP_CACHE) and os.path.exists(CHARGER_CACHE):
        if os.path.getmtime(DONG_LOOKUP_CACHE) >= os.path.getmtime(CHARGER_CACHE):
            with open(DONG_LOOKUP_CACHE, encoding="utf-8") as f:
                return json.load(f)

    print("행정동 폴리곤 기반 dong 매핑 생성 중 (최초 1회, 수분 소요)...")
    polygons = get_seoul_dong_polygons()
    if not polygons:
        return {}

    # 유효 행정동 이름 집합 (법정동·도로명 오탐 걸러내기 위해)
    valid_dongs = set(dong for _, dong, _ in polygons)

    # dong=None 이거나 유효 행정동이 아닌 경우(법정동 오추출 등) 모두 GPS 매핑 대상
    targets = charger_df[
        (charger_df["dong"].isna() | ~charger_df["dong"].isin(valid_dongs)) &
        (charger_df["lat"] != 0) &
        (charger_df["lon"] != 0)
    ][["stat_id", "lat", "lon"]].drop_duplicates("stat_id")

    lookup = {}
    for _, row in targets.iterrows():
        pt = Point(row["lon"], row["lat"])
        matched = None
        # 1차: 정확히 포함되는 폴리곤
        for gu, dong, poly in polygons:
            if poly.contains(pt):
                matched = {"gu": gu, "dong": dong}
                break
        # 2차 fallback: 경계선에 걸리거나 폴리곤 틈새인 경우 가장 가까운 폴리곤
        if matched is None:
            min_dist = float("inf")
            for gu, dong, poly in polygons:
                d = poly.distance(pt)
                if d < min_dist:
                    min_dist = d
                    matched = {"gu": gu, "dong": dong}
        if matched:
            lookup[row["stat_id"]] = matched

    with open(DONG_LOOKUP_CACHE, "w", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False)
    print(f"  → {len(lookup):,}개 충전소 동 매핑 완료")
    return lookup


# ─── GeoJSON ─────────────────────────────────────────────────────────────────
def get_seoul_gu_geojson():
    os.makedirs(os.path.dirname(GEOJSON_CACHE), exist_ok=True)
    if os.path.exists(GEOJSON_CACHE):
        with open(GEOJSON_CACHE, encoding="utf-8") as f:
            return json.load(f)
    try:
        resp = requests.get(GEOJSON_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        with open(GEOJSON_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data
    except Exception:
        return None


def geojson_centroids(geojson):
    centroids = {}
    if not geojson:
        return centroids
    for feat in geojson.get("features", []):
        props = feat.get("properties", {})
        name  = props.get("name") or props.get("NAME") or props.get("SIG_KOR_NM") or ""
        geom  = feat.get("geometry", {})
        coords = []
        if geom.get("type") == "Polygon":
            coords = geom["coordinates"][0]
        elif geom.get("type") == "MultiPolygon":
            for poly in geom["coordinates"]:
                coords.extend(poly[0])
        if coords:
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            centroids[name] = (sum(lats)/len(lats), sum(lons)/len(lons))
    return centroids


# ─── API 수집 ─────────────────────────────────────────────────────────────────
def _fetch_page(page, num_rows=9999):
    params = {"serviceKey": API_KEY, "pageNo": page,
              "numOfRows": num_rows, "dataType": "JSON"}
    r = requests.get(BASE_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_seoul_chargers_api():
    """API에서 서울시 전체 충전기 데이터 수집 후 캐시."""
    os.makedirs(os.path.dirname(CHARGER_CACHE), exist_ok=True)
    if os.path.exists(CHARGER_CACHE):
        with open(CHARGER_CACHE, encoding="utf-8") as f:
            return json.load(f)

    all_items, page = [], 1
    total = None
    while True:
        data   = _fetch_page(page)
        total  = total or int(data.get("totalCount", 0))
        raw    = data.get("items", {}).get("item", [])
        if isinstance(raw, dict):
            raw = [raw]
        if not raw:
            break
        seoul  = [x for x in raw if x.get("addr", "").startswith("서울특별시")]
        all_items.extend(seoul)
        fetched = (page - 1) * 9999 + len(raw)
        if fetched >= total:
            break
        page += 1
        time.sleep(0.2)

    with open(CHARGER_CACHE, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False)
    return all_items


# ─── EV 등록 데이터 ───────────────────────────────────────────────────────────
def _parse_gu_dong(gu_full, dong_raw):
    gu = None
    if gu_full:
        m = _RE_GU.search(gu_full)
        if m:
            gu = m.group(1)
    dong = None
    if dong_raw:
        # 구 이름 이후 부분에서만 동 추출
        # → "성동구 금호2.3가동"에서 "성동" 오탐 방지
        gm = _RE_GU.search(dong_raw)
        rest = dong_raw[gm.end():] if gm else dong_raw
        if not gu and gm:
            gu = gm.group(1)
        # 점(.) 포함 복합 행정동 우선 시도 ("금호2.3가동", "종로1.2.3.4가동")
        m2 = _RE_DONG_EXT.search(rest)
        if m2:
            dong = m2.group(1).replace(".", "·")  # 마침표 → 가운뎃점 정규화
        else:
            m3 = _RE_DONG.search(rest)
            if m3:
                dong = m3.group(1)
    return gu, dong


def load_ev_data() -> pd.DataFrame:
    # pandas로 일괄 읽기 → openpyxl 순차 읽기 대비 빠름
    raw = pd.read_excel(EV_FILE, engine="openpyxl", skiprows=8, header=None)
    # 병합 셀 처리: 구(0열)·동(2열) 앞 값 전파
    raw.iloc[:, 0] = raw.iloc[:, 0].ffill()
    raw.iloc[:, 2] = raw.iloc[:, 2].ffill()
    # 전기 연료만 필터 (3열) + 등록 대수 존재(4열)
    mask = raw.iloc[:, 3].astype(str).str.strip() == "전기"
    ev = raw[mask & raw.iloc[:, 4].notna()].copy()

    rows = []
    for _, r in ev.iterrows():
        gu, dong = _parse_gu_dong(str(r.iloc[0]), str(r.iloc[2]))
        if dong and dong != "기타":
            rows.append({"gu": gu, "dong": dong, "ev_count": int(r.iloc[4])})

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.groupby(["gu", "dong"], as_index=False)["ev_count"].sum()


# ─── 충전소 데이터 (Excel) ────────────────────────────────────────────────────
def _load_charger_excel() -> pd.DataFrame:
    """전기차 충전소 설치현황 엑셀에서 스테이션 단위 데이터 로드."""
    if not os.path.exists(CHARGER_EXCEL):
        return pd.DataFrame()
    try:
        raw = pd.read_excel(CHARGER_EXCEL, engine="openpyxl", skiprows=3, header=None)
    except Exception:
        return pd.DataFrame()

    rows = []
    for idx, r in raw.iterrows():
        addr = str(r.iloc[2]).strip() if pd.notna(r.iloc[2]) else ""
        if not addr.startswith("서울특별시"):
            continue
        gm   = _RE_GU.search(addr)
        gu   = gm.group(1) if gm else None
        if not gu or gu not in SEOUL_GU:
            continue
        # 구 이름 이후에서만 동 추출 (예: "성동구"→"성동" 오탐 방지)
        rest = addr[gm.end():]
        dm   = _RE_DONG.search(rest)
        dong = dm.group(1) if dm else None
        try:
            fast = int(float(r.iloc[3])) if pd.notna(r.iloc[3]) else 0
            slow = int(float(r.iloc[4])) if pd.notna(r.iloc[4]) else 0
        except (ValueError, TypeError):
            fast, slow = 0, 0
        if fast + slow == 0:
            continue
        location = str(r.iloc[1]).strip() if pd.notna(r.iloc[1]) else ""
        rows.append({
            "stat_id":      f"xl_{idx}",
            "charger_id":   f"xl_{idx}_0",
            "location":     location,
            "address":      addr,
            "charger_type": "xl",
            "fast_count":   fast,
            "slow_count":   slow,
            "total_count":  fast + slow,
            "gu":           gu,
            "dong":         dong,
            "lat":          0.0,
            "lon":          0.0,
        })
    return pd.DataFrame(rows)


# ─── 충전소 데이터 (API + Excel 통합) ────────────────────────────────────────
def load_charger_data() -> pd.DataFrame:
    """API 캐시 + 설치현황 엑셀을 합쳐 반환. (GPS 있는 API 우선, 엑셀은 보완)"""
    items = fetch_seoul_chargers_api()

    rows = []
    for item in items:
        addr = item.get("addr", "")
        # 주소가 서울특별시로 시작하지 않으면 제외 (캐시된 오탐 방어)
        if not addr.startswith("서울특별시"):
            continue

        gm   = _RE_GU.search(addr)
        gu   = gm.group(1) if gm else None

        # 서울 25개 자치구가 아닌 경우 제외
        if gu and gu not in SEOUL_GU:
            continue

        # 구 이름 이후에서만 동 추출 (예: "성동구"→"성동", "강동구"→"강동" 오탐 방지)
        rest = addr[gm.end():] if gm else addr
        dm   = _RE_DONG.search(rest)
        dong = dm.group(1) if dm else None

        try:
            lat = float(item.get("lat") or 0)
            lon = float(item.get("lng") or 0)
        except (ValueError, TypeError):
            lat, lon = 0.0, 0.0

        ctype = str(item.get("chgerType", "")).strip()
        is_fast = ctype not in SLOW_TYPES

        rows.append({
            "stat_id":    item.get("statId", ""),
            "charger_id": item.get("chgerId", ""),
            "location":   item.get("statNm", ""),
            "address":    addr,
            "charger_type": ctype,
            "fast_count": 1 if is_fast else 0,
            "slow_count": 0 if is_fast else 1,
            "total_count": 1,
            "gu":  gu,
            "dong": dong,
            "lat": lat,
            "lon": lon,
        })

    api_df = pd.DataFrame(rows)

    # ── GPS 기반 dong 보완 (dong=None 또는 법정동 오추출 포함) ──────────────────
    if not api_df.empty:
        lookup = build_dong_lookup(api_df)
        if lookup:
            # lookup에 있는 stat_id는 법정동 오추출 포함 → 무조건 GPS 결과로 덮어씀
            gps_mask = api_df["stat_id"].isin(lookup)
            def _apply_lookup(row):
                if row["stat_id"] in lookup:
                    info = lookup[row["stat_id"]]
                    row["gu"]   = info["gu"]
                    row["dong"] = info["dong"]
                return row
            api_df.loc[gps_mask] = api_df[gps_mask].apply(_apply_lookup, axis=1)

    # 엑셀 데이터 보완 — API에 없는 충전소 추가
    xl_df = _load_charger_excel()
    if xl_df.empty:
        return api_df

    # 중복 제거: API에 이미 같은 (gu, dong, location) 있으면 엑셀 행 제외
    api_keys = set(
        zip(api_df["gu"].fillna(""), api_df["dong"].fillna(""), api_df["location"])
    )
    xl_new = xl_df[
        ~xl_df.apply(
            lambda r: (r["gu"] or "", r["dong"] or "", r["location"]) in api_keys,
            axis=1,
        )
    ]

    if xl_new.empty:
        return api_df

    return pd.concat([api_df, xl_new], ignore_index=True)


# ─── 병합 & 지수 계산 ─────────────────────────────────────────────────────────
def merge_data(ev_df: pd.DataFrame, charger_df: pd.DataFrame) -> pd.DataFrame:
    charger_by_dong = (
        charger_df[charger_df["dong"].notna()]
        .groupby(["gu", "dong"], as_index=False)
        .agg(fast_total=("fast_count","sum"), slow_total=("slow_count","sum"),
             charger_total=("total_count","sum"), station_count=("stat_id","nunique"))
    )

    # LEFT JOIN: EV 등록 동 기준 유지
    # outer 조인 시 GPS 없는 엑셀 충전기의 법정동명(마곡동, 상계동 등)이
    # phantom ev=0 행을 만들어 "해당없음" 오표시 발생 → left로 제거
    merged = pd.merge(ev_df, charger_by_dong, on=["gu","dong"], how="left")
    for col in ["ev_count","charger_total","fast_total","slow_total","station_count"]:
        merged[col] = merged[col].fillna(0).astype(int)

    merged["convenience_idx"] = merged.apply(
        lambda r: round((r["fast_total"]*3 + r["slow_total"]) / max(r["ev_count"],1) * 100, 1),
        axis=1)
    merged["shortage_idx"] = merged.apply(
        lambda r: round(r["ev_count"] / max(r["charger_total"],1), 1),
        axis=1)

    def status(row):
        if row["ev_count"] == 0:
            return "해당없음"
        if row["charger_total"] == 0:
            return "충전소 없음"
        if row["shortage_idx"] > 20:
            return "매우 부족"
        if row["shortage_idx"] > 10:
            return "부족"
        if row["shortage_idx"] > 5:
            return "보통"
        return "충분"

    merged["status"] = merged.apply(status, axis=1)
    return merged


def aggregate_by_gu(charger_df: pd.DataFrame, ev_df: pd.DataFrame) -> pd.DataFrame:
    """자치구별 집계 — 충전기는 API 전수, EV는 등록 데이터."""
    gu_ev = ev_df.groupby("gu", as_index=False)["ev_count"].sum()

    gu_ch = (
        charger_df[charger_df["gu"].notna()]
        .groupby("gu", as_index=False)
        .agg(charger_total=("total_count","sum"),
             fast_total=("fast_count","sum"),
             slow_total=("slow_count","sum"),
             station_count=("stat_id","nunique"))
    )

    gu = pd.merge(gu_ev, gu_ch, on="gu", how="outer")
    for col in ["ev_count","charger_total","fast_total","slow_total","station_count"]:
        gu[col] = gu[col].fillna(0).astype(int)

    gu["convenience_idx"] = gu.apply(
        lambda r: round((r["fast_total"]*3 + r["slow_total"]) / max(r["ev_count"],1) * 100, 1),
        axis=1)
    gu["shortage_idx"] = gu.apply(
        lambda r: round(r["ev_count"] / max(r["charger_total"],1), 1),
        axis=1)
    return gu


# ─── 사용자 위치 → 가까운 충전소 ─────────────────────────────────────────────
def geocode_address(address: str):
    try:
        from geopy.geocoders import Nominatim
        geolocator = Nominatim(user_agent="ev_infra_seoul", timeout=5)
        query = address if "서울" in address else f"서울특별시 {address}"
        loc = geolocator.geocode(query, language="ko")
        if loc:
            return loc.latitude, loc.longitude
    except Exception:
        pass
    return None, None


def find_nearest_chargers(lat: float, lon: float,
                           charger_df: pd.DataFrame, top_n: int = 5):
    """API 데이터에 실제 좌표가 있으므로 정확한 거리 계산."""
    valid = charger_df[(charger_df["lat"] != 0) & (charger_df["lon"] != 0)].copy()

    # 충전소 단위로 집계 (같은 stat_id = 같은 충전소의 여러 충전기)
    stations = (
        valid.groupby(["stat_id","location","address","gu","dong","lat","lon"], as_index=False)
        .agg(fast_count=("fast_count","sum"), slow_count=("slow_count","sum"),
             total_count=("total_count","sum"))
    )

    stations["distance_km"] = stations.apply(
        lambda r: round(haversine(lat, lon, r["lat"], r["lon"]), 2), axis=1)

    return stations.nsmallest(top_n, "distance_km").to_dict("records")
