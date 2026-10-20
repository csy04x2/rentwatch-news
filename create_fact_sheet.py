
import os
import json
from collections import Counter
from datetime import date
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(".env에 DATABASE_URL이 없습니다.")

REGION = "11620"
START = date(2025, 10, 1)
END = date(2026, 9, 30)

TOPIC = (
    "2025년 10월부터 2026년 9월까지 "
    "서울 관악구 오피스텔의 전세·월세 거래량은 "
    "어떻게 변화했는가?"
)

SOURCE_URL = (
    "https://www.data.go.kr/data/15126475/openapi.do"
)

# 실제 거래만 조회
QUERY = text("""
    SELECT
        id,
        contract_date,
        rent_manwon,
        collected_at
    FROM rental_transactions
    WHERE district_code = '11620'
      AND property_type = 'OFFICETEL'
      AND contract_date >= :start
      AND contract_date < :end_exclusive
      AND source_row_key NOT LIKE 'SEED-SAMPLE-%'
    ORDER BY id
""")

# 월별 건수의 독립적인 DB 집계
COUNT_QUERY = text("""
    SELECT
        DATE_FORMAT(contract_date, '%Y-%m') AS month,
        COUNT(*) AS total,
        SUM(CASE WHEN rent_manwon = 0
                 THEN 1 ELSE 0 END) AS jeonse,
        SUM(CASE WHEN rent_manwon > 0
                 THEN 1 ELSE 0 END) AS wolse
    FROM rental_transactions
    WHERE district_code = '11620'
      AND property_type = 'OFFICETEL'
      AND contract_date >= :start
      AND contract_date < :end_exclusive
      AND source_row_key NOT LIKE 'SEED-SAMPLE-%'
    GROUP BY DATE_FORMAT(contract_date, '%Y-%m')
""")


def make_months():
    months = []

    for year in (2025, 2026):
        for month in range(1, 13):
            label = f"{year}-{month:02d}"

            if "2025-10" <= label <= "2026-09":
                months.append(label)

    return months


def main():
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
    )

    params = {
        "start": START,
        "end_exclusive": date(2026, 10, 1),
    }

    months = make_months()

    # 같은 DB 트랜잭션 안에서 조회·검증·저장
    with engine.begin() as connection:
        rows = connection.execute(
            QUERY, params
        ).mappings().all()

        if not rows:
            raise RuntimeError("분석할 거래가 없습니다.")

        counts = {
            month: {
                "total": 0,
                "jeonse": 0,
                "wolse": 0,
            }
            for month in months
        }

        evidence_ids = []
        collection_times = []

        for row in rows:
            month = row["contract_date"].strftime(
                "%Y-%m"
            )

            counts[month]["total"] += 1

            if row["rent_manwon"] == 0:
                counts[month]["jeonse"] += 1
            else:
                counts[month]["wolse"] += 1

            evidence_ids.append(row["id"])
            collection_times.append(row["collected_at"])

        total = len(rows)
        jeonse_total = sum(
            value["jeonse"]
            for value in counts.values()
        )
        wolse_total = sum(
            value["wolse"]
            for value in counts.values()
        )

        wolse_ratio = float(
            (
                Decimal(wolse_total)
                / Decimal(total)
                * 100
            ).quantize(Decimal("0.01"))
        )

        monthly_facts = []

        for month in months:
            value = counts[month]

            monthly_facts.append({
                "month": month,
                "total_count": value["total"],
                "jeonse_count": value["jeonse"],
                "wolse_count": value["wolse"],
            })

        facts = {
            "total_count": total,
            "jeonse_count": jeonse_total,
            "wolse_count": wolse_total,
            "wolse_ratio_percent": wolse_ratio,
            "monthly": monthly_facts,
            "unit": "건",
        }

        evidence = {
            "source_org": "국토교통부",
            "source_url": SOURCE_URL,
            "transaction_ids": evidence_ids,
            "source_row_count": total,
            "collected_at_min": min(
                collection_times
            ).isoformat(),
            "collected_at_max": max(
                collection_times
            ).isoformat(),
            "calculation": {
                "jeonse": "rent_manwon == 0",
                "wolse": "rent_manwon > 0",
                "ratio": (
                    "wolse_count / total_count * 100"
                ),
            },
            "limitations": [
                "실거래 신고 지연 및 정정 가능성",
                "수집 시점의 신고 자료 기준",
                "거래 건수는 전체 임대주택 재고가 아님",
                "동일 조건 거래도 별개 행으로 보존",
            ],
        }

        # Fact Sheet를 우선 PENDING으로 생성
        result = connection.execute(
            text("""
                INSERT INTO fact_sheets (
                    topic,
                    region_code,
                    property_type,
                    period_start,
                    period_end,
                    query_filters,
                    facts_json,
                    evidence_json,
                    verification_status,
                    created_at
                )
                VALUES (
                    :topic,
                    :region,
                    'OFFICETEL',
                    :start,
                    :end,
                    :filters,
                    :facts,
                    :evidence,
                    'PENDING',
                    NOW()
                )
            """),
            {
                "topic": TOPIC,
                "region": REGION,
                "start": START,
                "end": END,
                "filters": json.dumps(
                    {
                        "district_code": REGION,
                        "property_type": "OFFICETEL",
                        "exclude_seed": True,
                        "rent_types": [
                            "jeonse", "wolse"
                        ],
                    },
                    ensure_ascii=False,
                ),
                "facts": json.dumps(
                    facts, ensure_ascii=False
                ),
                "evidence": json.dumps(
                    evidence, ensure_ascii=False
                ),
            },
        )

        fact_sheet_id = result.lastrowid

        # Python 결과와 DB 집계 결과 대조
        db_rows = connection.execute(
            COUNT_QUERY, params
        ).mappings().all()

        db_counts = {
            row["month"]: {
                "total": int(row["total"]),
                "jeonse": int(row["jeonse"]),
                "wolse": int(row["wolse"]),
            }
            for row in db_rows
        }

        checks = {
            "total_matches": (
                sum(
                    item["total"]
                    for item in counts.values()
                ) == total
            ),
            "type_sum_matches": (
                jeonse_total + wolse_total == total
            ),
            "ids_unique": (
                len(set(evidence_ids)) == total
            ),
            "months_complete": (
                all(
                    counts[month]["total"] > 0
                    for month in months
                )
            ),
            "db_counts_match": (
                db_counts == {
                    month: value
                    for month, value in counts.items()
                    if value["total"] > 0
                }
            ),
        }

        passed = all(checks.values())

        status = "PASSED" if passed else "FAILED"

        # 검증 결과를 근거 JSON에 포함
        evidence["verification_checks"] = checks

        connection.execute(
            text("""
                UPDATE fact_sheets
                SET verification_status = :status,
                    evidence_json = :evidence
                WHERE id = :id
            """),
            {
                "status": status,
                "evidence": json.dumps(
                    evidence, ensure_ascii=False
                ),
                "id": fact_sheet_id,
            },
        )

    print("\n===== RentWatch Seoul Fact Sheet =====")
    print("Fact Sheet ID:", fact_sheet_id)
    print("취재 질문:", TOPIC)
    print("분석 기간:", START, "~", END)
    print("전체 거래:", total, "건")
    print("전세:", jeonse_total, "건")
    print("월세:", wolse_total, "건")
    print("월세 비중:", wolse_ratio, "%")

    print("\n월별 거래 건수")
    for item in monthly_facts:
        print(
            item["month"],
            "전체:", item["total_count"],
            "전세:", item["jeonse_count"],
            "월세:", item["wolse_count"],
        )

    print("\n검증 결과")
    for name, ok in checks.items():
        print(name, "PASS" if ok else "FAIL")

    print("\n최종 상태:", status)
    print("MySQL Fact Sheet ID:", fact_sheet_id)


if __name__ == "__main__":
    main()
