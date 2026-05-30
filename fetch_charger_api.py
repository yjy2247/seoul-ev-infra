"""
한국환경공단 전기자동차 충전소 정보 API
전국 데이터 수신 후 서울만 필터링
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests, json, time, os, re
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

API_KEY   = os.environ.get("EV_API_KEY", "")
BASE_URL  = "http://apis.data.go.kr/B552584/EvCharger/getChargerInfo"
CACHE_ALL = "data/seoul_chargers_api.json"

def fetch_page(page, num_rows=9999):
    params = {
        "serviceKey": API_KEY,
        "pageNo":     page,
        "numOfRows":  num_rows,
        "dataType":   "JSON",
    }
    r = requests.get(BASE_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def fetch_seoul():
    os.makedirs("data", exist_ok=True)
    if os.path.exists(CACHE_ALL):
        print("캐시 로드 중...")
        with open(CACHE_ALL, encoding='utf-8') as f:
            return json.load(f)

    print("API 전국 데이터 수신 중 (서울 필터링)...")
    seoul_items = []
    page = 1
    total = None

    while True:
        data = fetch_page(page)
        if total is None:
            total = int(data.get("totalCount", 0))
            pages = (total // 9999) + 1
            print(f"전국 총 {total:,}기 → 약 {pages}페이지")

        raw = data.get("items", {}).get("item", [])
        if isinstance(raw, dict):
            raw = [raw]
        if not raw:
            break

        seoul = [x for x in raw if x.get("addr", "").startswith("서울특별시")]
        seoul_items.extend(seoul)
        fetched = (page - 1) * 9999 + len(raw)
        print(f"  page {page:>3}: {len(raw):>5}건 수신 | 서울 {len(seoul):>4}건 | "
              f"서울 누적 {len(seoul_items):>6}건 ({fetched/total*100:.1f}%)")

        if fetched >= total:
            break
        page += 1
        time.sleep(0.2)

    with open(CACHE_ALL, 'w', encoding='utf-8') as f:
        json.dump(seoul_items, f, ensure_ascii=False, indent=2)
    print(f"\n저장 완료: {CACHE_ALL} ({len(seoul_items):,}건)")
    return seoul_items

# ─── 분석 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    items = fetch_seoul()

    total_count = len(items)
    print(f"\n{'='*50}")
    print(f"서울시 전체 충전기: {total_count:,}기")
    print(f"{'='*50}")

    # 충전기 타입 분류
    # chgerType: 01=DC차데모, 02=AC완속, 03=DC콤보, 04=DC차데모+AC3상
    #            05=DC차데모+DC콤보, 06=DC차데모+DC콤보+AC3상, 07=AC3상
    SLOW_TYPES = {"02", "07"}

    gu_total = defaultdict(int)
    gu_fast  = defaultdict(int)
    gu_slow  = defaultdict(int)
    gu_dong  = defaultdict(lambda: defaultdict(int))

    for item in items:
        addr = item.get("addr", "")
        gm = re.search(r'([가-힣]+구)', addr)
        dm = re.search(r'([가-힣0-9]+[동읍면리])', addr)
        gu   = gm.group(1) if gm else "기타"
        dong = dm.group(1) if dm else "기타"

        ctype = str(item.get("chgerType", "")).strip()
        gu_total[gu] += 1
        gu_dong[gu][dong] += 1
        if ctype in SLOW_TYPES:
            gu_slow[gu] += 1
        else:
            gu_fast[gu] += 1

    # 자치구별 출력
    print(f"\n{'자치구':<10} {'합계':>7} {'급속':>7} {'완속':>7}")
    print("-" * 36)
    for gu, cnt in sorted(gu_total.items(), key=lambda x: -x[1]):
        print(f"{gu:<10} {cnt:>7,} {gu_fast[gu]:>7,} {gu_slow[gu]:>7,}")
    print("-" * 36)
    print(f"{'합계':<10} {sum(gu_total.values()):>7,} "
          f"{sum(gu_fast.values()):>7,} {sum(gu_slow.values()):>7,}")

    # 은평구 상세
    print(f"\n=== 은평구 상세 ===")
    print(f"총 {gu_total.get('은평구',0):,}기  "
          f"(급속 {gu_fast.get('은평구',0):,} / 완속 {gu_slow.get('은평구',0):,})")
    print(f"\n동별 분포:")
    for dong, cnt in sorted(gu_dong['은평구'].items(), key=lambda x: -x[1])[:15]:
        print(f"  {dong}: {cnt}기")

    # CSV 저장
    import csv
    with open("data/seoul_chargers_by_gu_dong.csv", 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(["gu", "dong", "charger_count"])
        for gu in sorted(gu_dong):
            for dong, cnt in sorted(gu_dong[gu].items()):
                w.writerow([gu, dong, cnt])
    print("\nCSV 저장: data/seoul_chargers_by_gu_dong.csv")

    # 급속/완속 별도 CSV
    with open("data/seoul_chargers_by_gu.csv", 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(["gu", "total", "fast", "slow"])
        for gu in sorted(gu_total, key=lambda x: -gu_total[x]):
            w.writerow([gu, gu_total[gu], gu_fast[gu], gu_slow[gu]])
    print("CSV 저장: data/seoul_chargers_by_gu.csv")
