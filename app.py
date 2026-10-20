
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
                       reviewed_by, reviewed_at, updated_at
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

        # 사람 체크리스트 4항목의 최신 PASS와 본문 일치를 검증
        require_approved_checklist(conn, article, reviewer)

        if not article["validation_passed"]:
            raise HTTPException(
                400, "자동 검수를 통과하지 않았습니다."
            )

        result = article["validation_result"]

        if isinstance(result, str):
            result = json.loads(result)

        if not isinstance(result, dict):
            raise HTTPException(
                400, "검수 결과가 없습니다."
            )

        if result.get("passed") is not True:
            raise HTTPException(
                400, "검수 결과가 PASS가 아닙니다."
            )

        # 검수 이후 제목·본문이 바뀌었는지 확인
        current_hash = get_hash(
            article["title"],
            article["content"],
        )

        if result.get("content_hash") != current_hash:
            raise HTTPException(
                400,
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
                400,
                "Fact Sheet가 정확히 하나 연결되어야 합니다.",
            )

        if sources[0]["verification_status"] != "PASSED":
            raise HTTPException(
                400, "Fact Sheet 검증이 필요합니다."
            )

        if result.get("fact_sheet_id") != sources[0]["id"]:
            raise HTTPException(
                400, "검수에 사용한 Fact Sheet가 다릅니다."
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

# ===== S03 기사 목록 조회 =====

@app.get("/api/articles")
def list_articles():
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    id,
                    title,
                    status,
                    validation_passed,
                    created_at,
                    updated_at
                FROM articles
                ORDER BY id DESC
            """)
        ).mappings().all()

    return [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "validation_passed": bool(row["validation_passed"]),
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
        for row in rows
    ]

# ===== S03 기사 편집·저장 API =====

class ArticleEditRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)
    original_updated_at: str


@app.put("/api/articles/{article_id}")
def update_article(article_id: int, request: ArticleEditRequest):
    title = request.title.strip()
    content = request.content.strip()

    if not title or not content:
        raise HTTPException(
            status_code=422,
            detail="제목과 본문은 비어 있을 수 없습니다.",
        )

    if "\n" in title:
        raise HTTPException(
            status_code=422,
            detail="제목은 한 줄로 작성해야 합니다.",
        )

    with engine.begin() as conn:
        article = get_article(conn, article_id)

        if article["status"] != "검토대기":
            raise HTTPException(
                status_code=409,
                detail="검토대기 기사만 수정할 수 있습니다.",
            )

        # 다른 작업에서 기사를 먼저 수정했는지 확인
        current_updated_at = article["updated_at"].isoformat()

        if current_updated_at != request.original_updated_at:
            raise HTTPException(
                status_code=409,
                detail="기사가 다른 작업에서 수정됐습니다. 다시 조회해 주세요.",
            )

        if (
            article["title"] == title
            and article["content"].strip() == content
        ):
            return {
                "article_id": article_id,
                "changed": False,
                "message": "변경된 내용이 없습니다.",
            }

        conn.execute(
            text("""
                UPDATE articles
                SET title = :title,
                    content = :content,
                    validation_result = NULL,
                    validation_passed = FALSE,
                    reviewed_by = NULL,
                    reviewed_at = NULL,
                    updated_at = NOW(6)
                WHERE id = :id
            """),
            {
                "id": article_id,
                "title": title,
                "content": content,
            },
        )

    return {
        "article_id": article_id,
        "changed": True,
        "status": "검토대기",
        "validation_passed": False,
        "message": "기사 수정 완료. 재검수가 필요합니다.",
    }
@app.get("/article-editor", include_in_schema=False)
def article_editor_page():
    return FileResponse(BASE_DIR / "article_editor.html")


# ===== S03 AI 기사 초안 생성 API =====

@app.post("/api/articles/generate")
def generate_article_api():
    from generate_article import (
        load_fact_sheet,
        generate_draft,
        save_article,
    )
    from rag import search_documents

    try:
        fact_sheet = load_fact_sheet()

        references = search_documents(
            fact_sheet["topic"],
            top_k=3,
        )

        if not references:
            raise HTTPException(
                status_code=422,
                detail="RAG 참고문서를 찾지 못했습니다.",
            )

        draft = generate_draft(fact_sheet, references)
        article_id = save_article(draft, references)

        return {
            "article_id": article_id,
            "title": draft.title,
            "status": "검토대기",
            "validation_passed": False,
            "message": "AI 기사 초안 생성 및 저장 완료",
        }

    except HTTPException:
        raise
    except Exception as error:
        print("AI 기사 생성 실패:", repr(error))
        raise HTTPException(
            status_code=500,
            detail="AI 기사 생성에 실패했습니다. 서버 로그를 확인하세요.",
        )

# ===== S04 자동 검수 실행 API =====

@app.post("/api/articles/{article_id}/validate")
def validate_article_api(article_id: int):
    from datetime import datetime
    from validate_article import (
        load_article,
        check_numbers,
        check_context,
        save_result,
        MODEL,
    )

    # 검토대기 기사만 검수 가능
    with engine.connect() as conn:
        article = conn.execute(
            text("""
                SELECT id, status
                FROM articles
                WHERE id = :id
            """),
            {"id": article_id},
        ).mappings().first()

    if article is None:
        raise HTTPException(404, "기사를 찾을 수 없습니다.")

    if article["status"] != "검토대기":
        raise HTTPException(
            409, "검토대기 기사만 자동 검수할 수 있습니다."
        )

    try:
        data = load_article(article_id)

        number_issues = check_numbers(
            data["article"],
            data["facts"],
        )

        semantic_issues = check_context(data)

        issues = number_issues + semantic_issues
        passed = len(issues) == 0

        result = {
            "article_id": article_id,
            "fact_sheet_id": data["fact_id"],
            "reference_document_ids": [
                ref["id"] for ref in data["references"]
            ],
            "passed": passed,
            "checked_at": datetime.now().isoformat(),
            "model": MODEL,
            "number_issue_count": len(number_issues),
            "semantic_issue_count": len(semantic_issues),
            "issues": issues,
            "content_hash": get_hash(
                data["article"]["title"],
                data["article"]["content"],
            ),
        }

        # 검수 당시 내용이 그대로인 경우에만 저장
        save_result(data, result)

        return {
            "article_id": article_id,
            "status": "PASS" if passed else "FAIL",
            "validation_passed": passed,
            "number_issue_count": len(number_issues),
            "semantic_issue_count": len(semantic_issues),
            "issues": issues,
            "message": (
                "자동 검수가 완료됐습니다. "
                "사람의 최종 승인은 별도로 필요합니다."
            ),
        }

    except HTTPException:
        raise
    except RuntimeError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        )
    except Exception as error:
        print("자동 검수 실행 오류:", repr(error))
        raise HTTPException(
            status_code=500,
            detail="자동 검수 중 오류가 발생했습니다. 서버 로그를 확인하세요.",
        )
# ===== S04 검수·승인 화면 =====

@app.get("/review", include_in_schema=False)
def review_page():
    return FileResponse(BASE_DIR / "review.html")

# ===== S04 기사별 통계·RAG 근거 조회 =====

@app.get("/api/articles/{article_id}/sources")
def get_article_sources(article_id: int):
    with engine.connect() as conn:
        article = conn.execute(
            text("""
                SELECT id
                FROM articles
                WHERE id = :id
            """),
            {"id": article_id},
        ).first()

        if article is None:
            raise HTTPException(
                status_code=404,
                detail="기사를 찾을 수 없습니다.",
            )

        rows = conn.execute(
            text("""
                SELECT
                    s.evidence_role,
                    fs.id AS fact_sheet_id,
                    fs.topic,
                    fs.verification_status,
                    rd.id AS document_id,
                    rd.title AS document_title,
                    rd.source_org,
                    rd.source_url,
                    rd.content AS document_content
                FROM article_sources AS s
                LEFT JOIN fact_sheets AS fs
                    ON s.fact_sheet_id = fs.id
                LEFT JOIN reference_documents AS rd
                    ON s.reference_document_id = rd.id
                WHERE s.article_id = :id
                ORDER BY s.id
            """),
            {"id": article_id},
        ).mappings().all()

    fact_sheets = []
    references = []

    for row in rows:
        if row["evidence_role"] == "STATISTIC":
            fact_sheets.append({
                "id": row["fact_sheet_id"],
                "topic": row["topic"],
                "status": row["verification_status"],
            })

        elif row["evidence_role"] == "CONTEXT":
            references.append({
                "id": row["document_id"],
                "title": row["document_title"],
                "source_org": row["source_org"],
                "source_url": row["source_url"],
                "content": row["document_content"],
            })

    return {
        "article_id": article_id,
        "fact_sheets": fact_sheets,
        "references": references,
    }

# ===== 전월세 거래 CRUD API =====

from transaction_crud import make_crud_router

app.include_router(make_crud_router(engine))

# ===== 기자 체크리스트 검수 =====
from review_checklist import make_checklist_router, require_approved_checklist
app.include_router(make_checklist_router(engine))

@app.get("/review-checklist", include_in_schema=False)
def review_checklist_page():
    return FileResponse(BASE_DIR / "review_checklist.html")
