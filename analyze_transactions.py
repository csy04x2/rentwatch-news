
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# 1. .env에서 MySQL 접속 정보 불러오기
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(".env에 DATABASE_URL이 없습니다.")

# 2. MySQL 연결
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# 3. 실제 거래 데이터 조회
# 가상 테스트 데이터 5건은 제외한다.
query = text("""
    SELECT
        id,
        contract_date,
        area_sqm,
        deposit_manwon,
        rent_manwon
    FROM rental_transactions
    WHERE district_code = '11620'
      AND property_type = 'OFFICETEL'
      AND contract_date >= '2025-10-01'
      AND contract_date < '2026-10-01'
      AND source_row_key NOT LIKE 'SEED-SAMPLE-%'
    ORDER BY contract_date, id
""")

with engine.connect() as connection:
    df = pd.read_sql(query, connection)

if df.empty:
    raise RuntimeError("분석할 거래 데이터가 없습니다.")

# 4. 날짜 및 금액 자료형 변환
df["contract_date"] = pd.to_datetime(df["contract_date"])

df["deposit_manwon"] = pd.to_numeric(
    df["deposit_manwon"]
)

df["rent_manwon"] = pd.to_numeric(
    df["rent_manwon"]
)

# 5. 계약월과 거래 유형 구분
df["contract_month"] = (
    df["contract_date"].dt.strftime("%Y-%m")
)

df["rent_type"] = df["rent_manwon"].apply(
    lambda rent: "전세" if rent == 0 else "월세"
)

# 6. 월별 통계 계산
results = []

months = pd.period_range(
    "2025-10", "2026-09", freq="M"
)

for month in months:
    month_str = str(month)

    month_df = df[
        df["contract_month"] == month_str
    ]

    jeonse = month_df[
        month_df["rent_type"] == "전세"
    ]

    wolse = month_df[
        month_df["rent_type"] == "월세"
    ]

    results.append({
        "계약월": month_str,
        "전체 거래": len(month_df),
        "전세 건수": len(jeonse),
        "월세 건수": len(wolse),
        "전세 보증금 중위값(만원)": (
            jeonse["deposit_manwon"].median()
            if not jeonse.empty else None
        ),
        "월세 중위값(만원)": (
            wolse["rent_manwon"].median()
            if not wolse.empty else None
        ),
        "월세 보증금 중위값(만원)": (
            wolse["deposit_manwon"].median()
            if not wolse.empty else None
        ),
    })

summary = pd.DataFrame(results)

# 7. 전월 대비 전체 거래 건수 증감률
# 이전 달 건수가 0이면 계산하지 않는다.
previous = summary["전체 거래"].shift(1)

summary["전월 대비 증감률(%)"] = (
    (summary["전체 거래"] / previous - 1) * 100
).where(previous > 0).round(1)

# 8. 결과 출력
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)

print("\n===== RentWatch Seoul 월별 통계 =====")
print("지역: 서울 관악구")
print("주택 유형: 오피스텔")
print("분석 기간: 2025-10 ~ 2026-09")

print("\n전체 실제 거래:", len(df), "건")

print("\n월별 통계:")
print(summary.to_string(index=False, na_rep="-"))

print("\n===== 전체 기간 거래 유형 =====")
print(df["rent_type"].value_counts().to_string())

print("\n===== 데이터 확인 =====")
print("월별 거래 합계:", summary["전체 거래"].sum())
print("원자료 거래 수:", len(df))

if summary["전체 거래"].sum() == len(df):
    print("거래 건수 대조: PASS")
else:
    raise RuntimeError("월별 거래 합계가 원자료와 다릅니다.")
