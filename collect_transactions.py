
import os
import json
import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

API_KEY = os.getenv("MOLIT_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

REGION = "11620"

# 관악구 오피스텔 최초 수집 기간
MONTHS = [
    f"{year}{month:02d}"
    for year in (2025, 2026)
    for month in range(1, 13)
    if "202510" <= f"{year}{month:02d}" <= "202609"
]

API_URL = (
    "https://apis.data.go.kr/1613000/"
    "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent"
)

if not API_KEY or not DATABASE_URL:
    raise RuntimeError(
        "MOLIT_API_KEY 또는 DATABASE_URL이 없습니다."
    )


def month_bounds(year_month):
    """계약월의 첫날과 다음 달 첫날을 반환한다."""
    year = int(year_month[:4])
    month = int(year_month[4:])

    start = date(year, month, 1)

    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    return start, end


def fetch_transactions(year_month):
    """지정한 계약월의 모든 API 페이지를 조회한다."""
    all_items = []
    page = 1
    page_size = 100
    expected_total = None

    while True:
        params = {
            "serviceKey": API_KEY,
            "LAWD_CD": REGION,
            "DEAL_YMD": year_month,
            "numOfRows": page_size,
            "pageNo": page,
        }

        response = requests.get(
            API_URL,
            params=params,
            timeout=30,
        )
        response.raise_for_status()

        root = ET.fromstring(response.content)

        code = root.findtext(".//resultCode")
        message = root.findtext(".//resultMsg")

        if code != "000":
            raise RuntimeError(
                f"API 오류: {code} / {message}"
            )

        total = int(root.findtext(".//totalCount", "0"))
        items = root.findall(".//item")

        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError(
                "수집 도중 API 전체 건수가 변경됐습니다."
            )

        print(f"  {page}페이지: {len(items)}건")

        for item in items:
            fields = {
                child.tag: (child.text or "").strip()
                for child in item
            }
            all_items.append(fields)

        if len(all_items) >= expected_total:
            break

        if not items:
            raise RuntimeError(
                "전체 수집 전 빈 페이지가 반환됐습니다."
            )

        page += 1

    if len(all_items) != expected_total:
        raise RuntimeError(
            "API 건수와 수집 건수가 일치하지 않습니다."
        )

    return all_items


def convert_transaction(fields, occurrence, year_month):
    """API 데이터를 MySQL 저장 형식으로 변환한다."""

    contract_date = date(
        int(fields["dealYear"]),
        int(fields["dealMonth"]),
        int(fields["dealDay"]),
    )

    if contract_date.strftime("%Y%m") != year_month:
        raise RuntimeError(
            f"계약월 불일치: {contract_date}"
        )

    if fields["sggCd"] != REGION:
        raise RuntimeError(
            f"지역코드 불일치: {fields['sggCd']}"
        )

    area = Decimal(
        fields["excluUseAr"].replace(",", "")
    )
    deposit = int(
        fields["deposit"].replace(",", "")
    )
    rent = int(
        fields["monthlyRent"].replace(",", "")
    )

    floor_text = fields.get("floor", "")
    floor = int(floor_text) if floor_text else None

    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
    )

    digest = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:32]

    source_key = (
        f"OFFI-{REGION}-{year_month}-{digest}-{occurrence}"
    )

    return {
        "property_type": "OFFICETEL",
        "district_code": fields["sggCd"],
        "district_name": fields["sggNm"],
        "building_name": fields.get("offiNm") or None,
        "contract_date": contract_date,
        "area_sqm": area,
        "deposit_manwon": deposit,
        "rent_manwon": rent,
        "floor_no": floor,
        "raw_data": json.dumps(
            fields, ensure_ascii=False
        ),
        "source_row_key": source_key,
    }


INSERT_SQL = text("""
    INSERT INTO rental_transactions (
        property_type,
        district_code,
        district_name,
        building_name,
        contract_date,
        area_sqm,
        deposit_manwon,
        rent_manwon,
        floor_no,
        raw_data,
        source_row_key,
        collected_at
    )
    VALUES (
        :property_type,
        :district_code,
        :district_name,
        :building_name,
        :contract_date,
        :area_sqm,
        :deposit_manwon,
        :rent_manwon,
        :floor_no,
        :raw_data,
        :source_row_key,
        NOW()
    )
""")


COUNT_SQL = text("""
    SELECT COUNT(*)
    FROM rental_transactions
    WHERE district_code = :region
      AND property_type = 'OFFICETEL'
      AND contract_date >= :start
      AND contract_date < :end
      AND source_row_key NOT LIKE 'SEED-SAMPLE-%'
""")


def collect_month(engine, year_month):
    """한 달을 확인하고 미수집 상태면 저장한다."""

    start, end = month_bounds(year_month)

    # 이미 수집한 달은 API 호출 없이 건너뛴다.
    with engine.connect() as connection:
        existing = connection.execute(
            COUNT_SQL,
            {
                "region": REGION,
                "start": start,
                "end": end,
            },
        ).scalar_one()

    if existing > 0:
        print(f"[{year_month}] 기존 {existing}건 → 건너뜀")
        return 0

    print(f"\n[{year_month}] 수집 시작")

    raw_items = fetch_transactions(year_month)

    occurrence_counts = Counter()
    transactions = []

    for fields in raw_items:
        canonical = json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
        )

        occurrence_counts[canonical] += 1

        transactions.append(
            convert_transaction(
                fields,
                occurrence_counts[canonical],
                year_month,
            )
        )

    keys = [
        row["source_row_key"]
        for row in transactions
    ]

    if len(keys) != len(set(keys)):
        raise RuntimeError(
            f"{year_month}: 거래 식별키 충돌"
        )

    # 저장 직전에 다시 확인하고 한 달 단위로 커밋한다.
    with engine.begin() as connection:
        existing = connection.execute(
            COUNT_SQL,
            {
                "region": REGION,
                "start": start,
                "end": end,
            },
        ).scalar_one()

        if existing > 0:
            print(
                f"[{year_month}] 저장 전 기존 데이터 확인 "
                f"({existing}건) → 건너뜀"
            )
            return 0

        if transactions:
            connection.execute(
                INSERT_SQL,
                transactions,
            )

    print(
        f"[{year_month}] 저장 완료: "
        f"{len(transactions)}건"
    )

    return len(transactions)


def main():
    print("===== 관악구 오피스텔 12개월 수집 =====")
    print("수집 기간:", MONTHS[0], "~", MONTHS[-1])

    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
    )

    total_saved = 0

    for year_month in MONTHS:
        saved = collect_month(engine, year_month)
        total_saved += saved

    print("\n===== 전체 작업 완료 =====")
    print("이번 실행에서 신규 저장:", total_saved, "건")
    print("기존 수집 데이터: 유지")
    print("가상 테스트 데이터: 유지")


if __name__ == "__main__":
    main()
