
import os
import json
import hashlib

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(".env에 DATABASE_URL이 없습니다.")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

app = FastAPI(title="RentWatch Seoul")


class ReviewRequest(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=100)


def get_hash(title, content):
    """검수 당시 제목과 본문을 구분해 식별한다."""
    value = json.dumps(
        [title, content],
        ensure_ascii=False,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get_article(conn, article_id):
    row = conn.execute(
        text("""
            SELECT *
            FROM articles
            WHERE id = :id
            FOR UPDATE
        """),
        {"id": article_id},
    ).mappings().first()

    if row is None:
        raise HTTPException(404, "기사를 찾을 수 없습니다.")

    return row


@app.get("/articles/{article_id}")
def read_article(article_id: int):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, title, content, status,
                       validation_passed, validation_result,
                       reviewed_by, reviewed_at
                FROM articles
                WHERE id = :id
            """),
            {"id": article_id},
        ).mappings().first()

    if row is None:
        raise HTTPException(404, "기사를 찾을 수 없습니다.")

    return dict(row)


@app.post("/articles/{article_id}/approve")
def approve_article(article_id: int, request: ReviewRequest):
    reviewer = request.reviewed_by.strip()

    if not reviewer:
        raise HTTPException(422, "검토자 이름이 필요합니다.")

    with engine.begin() as conn:
        article = get_article(conn, article_id)

        if article["status"] != "검토대기":
            raise HTTPException(
                409, "검토대기 기사만 승인할 수 있습니다."
            )

        if not article["validation_passed"]:
            raise HTTPException(
                409, "자동 검수를 통과하지 않았습니다."
            )

        result = article["validation_result"]

        if isinstance(result, str):
            result = json.loads(result)

        if not isinstance(result, dict):
            raise HTTPException(
                409, "검수 결과가 없습니다."
            )

        if result.get("passed") is not True:
            raise HTTPException(
                409, "검수 결과가 PASS가 아닙니다."
            )

        # 검수 이후 제목·본문이 바뀌었는지 확인
        current_hash = get_hash(
            article["title"],
            article["content"],
        )

        if result.get("content_hash") != current_hash:
            raise HTTPException(
                409,
                "현재 기사에 대한 검수 결과가 아닙니다. "
                "재검수가 필요합니다.",
            )

        # 기사에 연결된 Fact Sheet 확인
        sources = conn.execute(
            text("""
                SELECT fs.id, fs.verification_status
                FROM article_sources AS s
                JOIN fact_sheets AS fs
                  ON s.fact_sheet_id = fs.id
                WHERE s.article_id = :id
                  AND s.evidence_role = 'STATISTIC'
            """),
            {"id": article_id},
        ).mappings().all()

        if len(sources) != 1:
            raise HTTPException(
                409,
                "Fact Sheet가 정확히 하나 연결되어야 합니다.",
            )

        if sources[0]["verification_status"] != "PASSED":
            raise HTTPException(
                409, "Fact Sheet 검증이 필요합니다."
            )

        if result.get("fact_sheet_id") != sources[0]["id"]:
            raise HTTPException(
                409, "검수에 사용한 Fact Sheet가 다릅니다."
            )

        # 실제 사람이 승인 API를 요청했을 때만 실행
        conn.execute(
            text("""
                UPDATE articles
                SET status = '승인',
                    reviewed_by = :reviewer,
                    reviewed_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": article_id, "reviewer": reviewer},
        )

    return {
        "article_id": article_id,
        "status": "승인",
        "reviewed_by": reviewer,
    }


@app.post("/articles/{article_id}/hold")
def hold_article(article_id: int, request: ReviewRequest):
    reviewer = request.reviewed_by.strip()

    if not reviewer:
        raise HTTPException(422, "검토자 이름이 필요합니다.")

    with engine.begin() as conn:
        article = get_article(conn, article_id)

        if article["status"] != "검토대기":
            raise HTTPException(
                409, "검토대기 기사만 보류할 수 있습니다."
            )

        conn.execute(
            text("""
                UPDATE articles
                SET status = '보류',
                    reviewed_by = :reviewer,
                    reviewed_at = NOW(),
                    updated_at = NOW()
                WHERE id = :id
            """),
            {"id": article_id, "reviewer": reviewer},
        )

    return {
        "article_id": article_id,
        "status": "보류",
        "reviewed_by": reviewer,
    }

# ===== S01 데이터 분석 화면 =====

from pathlib import Path
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(BASE_DIR / "dashboard.html")


@app.get("/api/statistics")
def get_statistics():
    query = text("""
        SELECT
            DATE_FORMAT(contract_date, '%Y-%m') AS month,
            COUNT(*) AS total,
            SUM(CASE WHEN rent_manwon = 0 THEN 1 ELSE 0 END) AS jeonse,
            SUM(CASE WHEN rent_manwon > 0 THEN 1 ELSE 0 END) AS wolse
        FROM rental_transactions
        WHERE district_code = '11620'
          AND property_type = 'OFFICETEL'
          AND contract_date >= '2025-10-01'
          AND contract_date < '2026-10-01'
          AND source_row_key NOT LIKE 'SEED-SAMPLE-%'
        GROUP BY DATE_FORMAT(contract_date, '%Y-%m')
        ORDER BY month
    """)

    with engine.connect() as conn:
        rows = conn.execute(query).mappings().all()

    monthly = [
        {
            "month": row["month"],
            "total": int(row["total"]),
            "jeonse": int(row["jeonse"]),
            "wolse": int(row["wolse"]),
        }
        for row in rows
    ]

    total = sum(row["total"] for row in monthly)
    jeonse = sum(row["jeonse"] for row in monthly)
    wolse = sum(row["wolse"] for row in monthly)

    return {
        "region": "서울 관악구",
        "property_type": "오피스텔",
        "period": "2025-10 ~ 2026-09",
        "total": total,
        "jeonse": jeonse,
        "wolse": wolse,
        "wolse_share": round(wolse / total * 100, 2) if total else 0,
        "monthly": monthly,
    }
@app.get("/fact-sheet", include_in_schema=False)
def fact_sheet_page():
    return FileResponse(BASE_DIR / "fact_sheet.html")


@app.get("/api/fact-sheets/{fact_sheet_id}")
def read_fact_sheet(fact_sheet_id: int):
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    id, topic, region_code, property_type,
                    period_start, period_end,
                    facts_json, evidence_json,
                    verification_status, created_at
                FROM fact_sheets
                WHERE id = :id
            """),
            {"id": fact_sheet_id},
        ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Fact Sheet를 찾을 수 없습니다."
        )

    facts = row["facts_json"]
    evidence = row["evidence_json"]

    if isinstance(facts, str):
        facts = json.loads(facts)

    if isinstance(evidence, str):
        evidence = json.loads(evidence)

    transaction_ids = evidence.get("transaction_ids", [])

    return {
        "id": row["id"],
        "topic": row["topic"],
        "region_code": row["region_code"],
        "property_type": row["property_type"],
        "period_start": row["period_start"].isoformat(),
        "period_end": row["period_end"].isoformat(),
        "status": row["verification_status"],
        "created_at": row["created_at"].isoformat(),
        "facts": facts,
        "evidence": {
            "source_org": evidence.get("source_org"),
            "source_url": evidence.get("source_url"),
            "source_row_count": evidence.get("source_row_count"),
            "transaction_id_count": len(transaction_ids),
            "transaction_id_sample": transaction_ids[:20],
            "collected_at_min": evidence.get("collected_at_min"),
            "collected_at_max": evidence.get("collected_at_max"),
            "calculation": evidence.get("calculation", {}),
            "limitations": evidence.get("limitations", []),
            "verification_checks": evidence.get(
                "verification_checks", {}
            ),
        },
    }
