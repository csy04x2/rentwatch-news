
import os
import json

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from rag import search_documents

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

FACT_SHEET_ID = 1
PROMPT_VERSION = "v0.1"

if not DATABASE_URL or not OPENAI_API_KEY:
    raise RuntimeError(
        ".env에 DATABASE_URL 또는 OPENAI_API_KEY가 없습니다."
    )

client = OpenAI(api_key=OPENAI_API_KEY)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


class ArticleDraft(BaseModel):
    title: str
    content: str


def load_fact_sheet():
    """검증을 통과한 Fact Sheet를 가져온다."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    id, topic, region_code, property_type,
                    period_start, period_end,
                    facts_json, evidence_json,
                    verification_status
                FROM fact_sheets
                WHERE id = :id
            """),
            {"id": FACT_SHEET_ID},
        ).mappings().first()

    if row is None:
        raise RuntimeError("Fact Sheet를 찾을 수 없습니다.")

    if row["verification_status"] != "PASSED":
        raise RuntimeError(
            "검증을 통과하지 않은 Fact Sheet입니다."
        )

    return {
        "id": row["id"],
        "topic": row["topic"],
        "region_code": row["region_code"],
        "property_type": row["property_type"],
        "period_start": row["period_start"].isoformat(),
        "period_end": row["period_end"].isoformat(),
        "facts": json.loads(row["facts_json"]),
        "evidence": json.loads(row["evidence_json"]),
    }


def generate_draft(fact_sheet, references):
    """Fact Sheet와 RAG 문서를 이용해 기사 초안을 생성한다."""

    # 숫자는 Fact Sheet에서만 가져온다.
    facts_input = {
        "topic": fact_sheet["topic"],
        "period_start": fact_sheet["period_start"],
        "period_end": fact_sheet["period_end"],
        "region_code": fact_sheet["region_code"],
        "property_type": fact_sheet["property_type"],
        "facts": fact_sheet["facts"],
        "limitations": fact_sheet["evidence"].get(
            "limitations", []
        ),
    }

    references_input = [
        {
            "id": ref["id"],
            "source_org": ref["org"],
            "source_url": ref["url"],
            "content": ref["content"],
        }
        for ref in references
    ]

    instructions = """
당신은 한국의 데이터 저널리즘 기자다.
입력된 Fact Sheet와 참고문서만 이용해 한국어 기사 초안을 작성한다.

반드시 다음 규칙을 지켜라.

1. 거래 건수와 비율은 Fact Sheet에 제공된 값만 사용한다.
2. 새로운 통계 수치를 임의로 계산하거나 만들어내지 않는다.
3. RAG 문서는 실거래 자료의 제공 기준 등 맥락 설명에만 사용한다.
4. RAG 문서에 나온 내용을 거래 통계의 근거로 대체하지 않는다.
5. 거래량 증감의 원인을 추측하거나 단정하지 않는다.
6. 신고 지연·정정 가능성을 본문에 명시한다.
7. 2026년 9월 거래 건수가 적다는 이유만으로
   시장 위축이라고 단정하지 않는다.
8. 거래 건수를 임차인의 주거비 부담 또는
   전체 임대주택 시장의 변화로 확대해석하지 않는다.
9. 공식 출처는 국토교통부로 명시한다.
10. 기사 제목과 본문을 작성하되 검증 완료 기사처럼 표현하지 않는다.

기사 형식:
- 객관적이고 간결한 뉴스 기사
- 제목 1개
- 리드 1개
- 본문 3~5개 문단
- 확인되지 않은 원인 분석이나 전문가 발언은 넣지 않는다.
"""

    response = client.responses.parse(
        model=MODEL,
        instructions=instructions,
        input=(
            "다음 자료를 바탕으로 기사 초안을 작성하라.\n\n"
            "FACT SHEET:\n"
            + json.dumps(
                facts_input,
                ensure_ascii=False,
            )
            + "\n\nRAG 참고자료:\n"
            + json.dumps(
                references_input,
                ensure_ascii=False,
            )
        ),
        text_format=ArticleDraft,
    )

    if response.output_parsed is None:
        raise RuntimeError("기사 생성 결과를 파싱하지 못했습니다.")

    article = response.output_parsed

    if not article.title.strip() or not article.content.strip():
        raise RuntimeError("생성된 제목 또는 본문이 비어 있습니다.")

    if len(article.title) > 500:
        raise RuntimeError("제목이 DB 허용 길이를 초과했습니다.")

    return article


def save_article(article, references):
    """기사와 근거 연결을 하나의 DB 작업으로 저장한다."""

    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO articles (
                    title,
                    content,
                    status,
                    generated_model,
                    prompt_version,
                    validation_result,
                    validation_passed,
                    reviewed_by,
                    reviewed_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    :title,
                    :content,
                    '검토대기',
                    :model,
                    :version,
                    NULL,
                    FALSE,
                    NULL,
                    NULL,
                    NOW(),
                    NOW()
                )
            """),
            {
                "title": article.title.strip(),
                "content": article.content.strip(),
                "model": MODEL,
                "version": PROMPT_VERSION,
            },
        )

        article_id = result.lastrowid

        # 수치 근거: Fact Sheet
        conn.execute(
            text("""
                INSERT INTO article_sources (
                    article_id,
                    fact_sheet_id,
                    reference_document_id,
                    evidence_role,
                    created_at
                )
                VALUES (
                    :article_id,
                    :fact_sheet_id,
                    NULL,
                    'STATISTIC',
                    NOW()
                )
            """),
            {
                "article_id": article_id,
                "fact_sheet_id": FACT_SHEET_ID,
            },
        )

        # 맥락 근거: 실제 검색된 RAG 문서
        for ref in references:
            conn.execute(
                text("""
                    INSERT INTO article_sources (
                        article_id,
                        fact_sheet_id,
                        reference_document_id,
                        evidence_role,
                        created_at
                    )
                    VALUES (
                        :article_id,
                        NULL,
                        :reference_id,
                        'CONTEXT',
                        NOW()
                    )
                """),
                {
                    "article_id": article_id,
                    "reference_id": ref["id"],
                },
            )

    return article_id


def main():
    print("===== RentWatch Seoul 기사 생성 =====")

    fact_sheet = load_fact_sheet()

    print("Fact Sheet ID:", fact_sheet["id"])
    print("Fact Sheet 검증: PASSED")

    print("\nRAG 참고문서 검색 중...")

    references = search_documents(
        fact_sheet["topic"],
        top_k=3,
    )

    if not references:
        raise RuntimeError(
            "RAG 검색 결과가 없어 기사 생성을 중단합니다."
        )

    print("검색된 참고문서:", len(references), "개")

    print("\nGPT-4.1-mini 기사 초안 생성 중...")

    article = generate_draft(
        fact_sheet,
        references,
    )

    print("\n===== 생성된 기사 초안 =====")
    print("\n제목:", article.title)
    print("\n본문:\n")
    print(article.content)

    article_id = save_article(
        article,
        references,
    )

    print("\n===== MySQL 저장 결과 =====")
    print("기사 ID:", article_id)
    print("상태: 검토대기")
    print("자동 검수: 미실시")
    print("승인 가능 여부: 불가")
    print("Fact Sheet 연결:", FACT_SHEET_ID)
    print(
        "RAG 근거 연결:",
        [ref["id"] for ref in references],
    )


if __name__ == "__main__":
    main()
