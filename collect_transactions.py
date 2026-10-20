
import os
import json
import hashlib
from collections import Counter
from datetime import date
from decimal import Decimal

import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

API_KEY = os.getenv("MOLIT_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

REGION = "11620"
YEAR_MONTH = "202609"

API_URL = (
    "https://apis.data.go.kr/1613000/"
    "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent"
)

if not API_KEY or not DATABASE_URL:
    raise RuntimeError("MOLIT_API_KEY 또는 DATABASE_URL이 없습니다.")


def fetch_transactions():
    """해당 계약월의 모든 API 페이지를 조회한다."""
    all_items = []
    page = 1
    page_size = 100
    expected_total = None

    while True:
        params = {
            "serviceKey": API_KEY,
            "LAWD_CD": REGION,
            "DEAL_YMD": YEAR_MONTH,
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
            raise RuntimeError(f"API 오류: {code} / {message}")

        total = int(root.findtext(".//totalCount", "0"))
        items = root.findall(".//item")

        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("수집 도중 API 전체 건수가 변경됐습니다.")

        print(f"{page}페이지: {len(items)}건")

        for item in items:
            fields = {
                child.tag: (child.text or "").strip()
                for child in item
            }
            all_items.append(fields)

        if len(all_items) >= expected_total:
            break

        if not items:
            raise RuntimeError("전체 수집 전 빈 페이지가 반환됐습니다.")

        page += 1

    if len(all_items) != expected_total:
        raise RuntimeError("API 건수와 수집 건수가 일치하지 않습니다.")

    return all_items


def convert_transaction(fields, occurrence):
    """API 문자열을 MySQL 컬럼 형식으로 바꾼다."""

    contract_date = date(
        int(fields["dealYear"]),
        int(fields["dealMonth"]),
        int(fields["dealDay"]),
    )

    area = Decimal(fields["excluUseAr"].replace(",", ""))
    deposit = int(fields["deposit"].replace(",", ""))
    rent = int(fields["monthlyRent"].replace(",", ""))

    floor_text = fields.get("floor", "")
    floor = int(floor_text) if floor_text else None

    # 응답 내용을 기반으로 재현 가능한 키를 생성한다.
    # 실제 계약 고유번호가 아니므로 다른 계약월 수집이나
    # 신고 정정 시 영구 식별자로 사용해서는 안 된다.
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
    )

    digest = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:32]

    source_key = (
        f"OFFI-{REGION}-{YEAR_MONTH}-{digest}-{occurrence}"
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
        "raw_data": json.dumps(fields, ensure_ascii=False),
        "source_row_key": source_key,
    }


def main():
    print("===== 국토교통부 거래 수집 =====")

    raw_items = fetch_transactions()

    print(f"\nAPI 전체 거래: {len(raw_items)}건")

    # 내용이 같은 행도 각각 저장하기 위해 순번을 부여한다.
    occurrence_counts = Counter()
    transactions = []

    for fields in raw_items:
        canonical = json.dumps(
            fields, ensure_ascii=False, sort_keys=True
        )

        occurrence_counts[canonical] += 1

        transactions.append(
            convert_transaction(
                fields,
                occurrence_counts[canonical],
            )
        )

    # Python에서 파싱 및 중복 키 충돌 확인
    keys = [row["source_row_key"] for row in transactions]

    if len(keys) != len(set(keys)):
        raise RuntimeError("수집 거래 식별키가 충돌했습니다.")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    month_start = date(2026, 9, 1)
    next_month = date(2026, 10, 1)

    with engine.begin() as connection:
        # 이미 실제 거래를 수집한 월은 자동으로 덮어쓰지 않는다.
        existing = connection.execute(
            text("""
                SELECT COUNT(*)
                FROM rental_transactions
                WHERE district_code = :region
                  AND property_type = 'OFFICETEL'
                  AND contract_date >= :start
                  AND contract_date < :end
                  AND source_row_key NOT LIKE 'SEED-SAMPLE-%'
            """),
            {
                "region": REGION,
                "start": month_start,
                "end": next_month,
            },
        ).scalar_one()

        if existing > 0:
            print(
                f"\n이미 저장된 실제 거래 {existing}건이 있습니다."
            )
            print("기존 데이터 보호를 위해 저장을 건너뜁니다.")
            return

        # 전체 거래를 하나의 트랜잭션으로 저장한다.
        # 오류가 나면 이번 INSERT 전체가 취소된다.
        connection.execute(
            text("""
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
            """),
            transactions,
        )

    print("\n===== 저장 완료 =====")
    print("API에서 가져온 거래:", len(raw_items))
    print("MySQL에 신규 저장한 거래:", len(transactions))
    print("가상 테스트 데이터: 변경 없음")


if __name__ == "__main__":
    main()
