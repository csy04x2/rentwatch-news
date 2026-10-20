"""사람의 체크리스트 검수: 저장된 결과를 승인 API에서 강제 검증한다."""
import hashlib
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

router = APIRouter(tags=["사람 검수 체크리스트"])

CHECKS = [
    "수치가 Fact Sheet와 일치",
    "단위·시각·지역 정확",
    "근거 없는 원인·전망·과장 없음",
    "출처·관측 시각 표기",
]


def content_hash(title, content):
    return hashlib.sha256(json.dumps([title, content], ensure_ascii=False).encode()).hexdigest()


class ChecklistRequest(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=100)
    opinion: str = Field(default="", max_length=3000)
    checks: list[bool] = Field(min_length=4, max_length=4)


def require_approved_checklist(conn, article, reviewed_by):
    """최종 승인 호출 때 반드시 새 검수 기록과 동일한 본문인지 재확인."""
    if article["status"] != "검토대기":
        raise HTTPException(400, "검토대기 상태의 기사만 승인할 수 있습니다.")
    row = conn.execute(text("""
        SELECT result, checks_json, content_hash, reviewed_by
        FROM review
        WHERE article_id = :id
        ORDER BY id DESC LIMIT 1
    """), {"id": article["id"]}).mappings().first()
    if not row:
        raise HTTPException(400, "사람 검수 기록이 없습니다. 체크리스트 4개를 먼저 확인해 주세요.")
    checks = row["checks_json"]
    if isinstance(checks, str):
        checks = json.loads(checks)
    if row["result"] != "PASS" or checks != [True]*4:
        raise HTTPException(400, "최신 체크리스트에 실패 항목이 있어 승인할 수 없습니다.")
    if row["content_hash"] != content_hash(article["title"], article["content"]):
        raise HTTPException(400, "체크리스트 검수 이후 본문이 변경됐습니다. 다시 검수해 주세요.")
    if row["reviewed_by"] != reviewed_by:
        raise HTTPException(400, "승인 검토자와 체크리스트 검토자가 일치해야 합니다.")


def make_checklist_router(engine):
    @router.post("/api/articles/{article_id}/review-checklist")
    def submit_checklist(article_id: int, payload: ChecklistRequest):
        reviewer = payload.reviewed_by.strip()
        if not reviewer:
            raise HTTPException(422, "검토자 이름을 입력해 주세요.")
        passed = all(payload.checks)
        with engine.begin() as conn:
            article = conn.execute(text("SELECT * FROM articles WHERE id=:id FOR UPDATE"), {"id": article_id}).mappings().first()
            if not article:
                raise HTTPException(404, "기사를 찾을 수 없습니다.")
            if article["status"] != "검토대기":
                raise HTTPException(400, "검토대기 상태의 기사만 체크리스트 검수할 수 있습니다.")
            conn.execute(text("""
                INSERT INTO review (article_id, reviewed_by, result, checks_json, opinion, content_hash, reviewed_at)
                VALUES (:id, :reviewer, :result, :checks, :opinion, :hash, NOW())
            """), {
                "id": article_id, "reviewer": reviewer, "result": "PASS" if passed else "FAIL",
                "checks": json.dumps(payload.checks), "opinion": payload.opinion,
                "hash": content_hash(article["title"], article["content"]),
            })
            if not passed:
                conn.execute(text("""
                    UPDATE articles SET status='보류', reviewed_by=:reviewer,
                        reviewed_at=NOW(), updated_at=NOW() WHERE id=:id
                """), {"id": article_id, "reviewer": reviewer})
        return {"article_id": article_id, "result": "PASS" if passed else "FAIL",
                "status": "검토대기" if passed else "보류",
                "message": "체크리스트 4개 통과. 자동 검수 PASS인 경우 승인할 수 있습니다." if passed
                           else "실패 항목이 있어 기사를 보류했습니다."}

    @router.get("/api/articles/{article_id}/review-history")
    def review_history(article_id: int):
        with engine.connect() as conn:
            found = conn.execute(text("SELECT id FROM articles WHERE id=:id"), {"id": article_id}).first()
            if not found:
                raise HTTPException(404, "기사를 찾을 수 없습니다.")
            rows = conn.execute(text("""
                SELECT id, reviewed_by, result, checks_json, opinion, reviewed_at, content_hash
                FROM review WHERE article_id=:id ORDER BY id DESC
            """), {"id": article_id}).mappings().all()
        return [{**dict(r), "reviewed_at":r["reviewed_at"].isoformat()} for r in rows]

    return router
