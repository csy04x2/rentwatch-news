
import os
import sys
import json
import hashlib
from app import get_hash
import re
from datetime import datetime
from decimal import Decimal

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

# ==================================================
# 1. 환경 설정
# ==================================================

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

if not DATABASE_URL or not API_KEY:
    raise RuntimeError(
        ".env의 DATABASE_URL 또는 OPENAI_API_KEY를 확인하세요."
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
client = OpenAI(api_key=API_KEY)


# ==================================================
# 2. AI 검수 결과 형식
# ==================================================

class Finding(BaseModel):
    category: str
    excerpt: str
    is_error: bool
    reason: str
    suggestion: str


class AIReview(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


def read_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def make_issue(category, excerpt, reason, suggestion):
    return {
        "category": category,
        "excerpt": excerpt,
        "reason": reason,
        "suggestion": suggestion,
    }


# ==================================================
# 3. 기사와 연결 근거 조회
# ==================================================

def load_article(article_id):
    with engine.connect() as conn:

        article = conn.execute(
            text("""
                SELECT id, title, content,
                       status, validation_passed
                FROM articles
                WHERE id = :id
            """),
            {"id": article_id},
        ).mappings().first()

        if article is None:
            raise RuntimeError("해당 기사가 없습니다.")

        sources = conn.execute(
            text("""
                SELECT evidence_role,
                       fact_sheet_id,
                       reference_document_id
                FROM article_sources
                WHERE article_id = :id
            """),
            {"id": article_id},
        ).mappings().all()

        fact_ids = {
            row["fact_sheet_id"]
            for row in sources
            if row["evidence_role"] == "STATISTIC"
        }

        if len(fact_ids) != 1 or None in fact_ids:
            raise RuntimeError(
                "기사에 Fact Sheet가 정확히 하나 연결되어야 합니다."
            )

        fact_id = next(iter(fact_ids))

        fact = conn.execute(
            text("""
                SELECT id, topic, region_code,
                       property_type, period_start,
                       period_end, facts_json,
                       evidence_json, verification_status
                FROM fact_sheets
                WHERE id = :id
            """),
            {"id": fact_id},
        ).mappings().first()

        if fact is None:
            raise RuntimeError("Fact Sheet를 찾을 수 없습니다.")

        if fact["verification_status"] != "PASSED":
            raise RuntimeError(
                "PASSED 상태의 Fact Sheet만 사용할 수 있습니다."
            )

        document_ids = {
            row["reference_document_id"]
            for row in sources
            if row["evidence_role"] == "CONTEXT"
        }

        if None in document_ids:
            raise RuntimeError("RAG 근거 ID가 비어 있습니다.")

        references = []

        for doc_id in sorted(document_ids):
            document = conn.execute(
                text("""
                    SELECT id, title, source_org,
                           source_url, content
                    FROM reference_documents
                    WHERE id = :id
                """),
                {"id": doc_id},
            ).mappings().first()

            if document is None:
                raise RuntimeError(
                    f"RAG 문서 {doc_id}를 찾을 수 없습니다."
                )

            references.append(dict(document))

    return {
        "article": dict(article),
        "fact_id": fact_id,
        "topic": fact["topic"],
        "region_code": fact["region_code"],
        "property_type": fact["property_type"],
        "period_start": fact["period_start"].isoformat(),
        "period_end": fact["period_end"].isoformat(),
        "facts": read_json(fact["facts_json"]),
        "evidence": read_json(fact["evidence_json"]),
        "references": references,
    }


# ==================================================
# 4. Python 수치 검사
# ==================================================

def check_numbers(article, facts):
    issues = []

    content = article["title"] + "\n" + article["content"]

    total = int(facts["total_count"])
    jeonse = int(facts["jeonse_count"])
    wolse = int(facts["wolse_count"])

    # Fact Sheet 자체의 기본 계산 관계 확인
    if jeonse + wolse != total:
        raise RuntimeError(
            "Fact Sheet의 전세·월세 합계가 일치하지 않습니다."
        )

    monthly = {
        row["month"]: row
        for row in facts["monthly"]
    }

    if sum(
        int(row["total_count"])
        for row in monthly.values()
    ) != total:
        raise RuntimeError(
            "Fact Sheet의 월별 건수 합계가 일치하지 않습니다."
        )

    # 기사에 명시된 '전체 거래 N건' 확인
    total_pattern = (
        r"(?:전체\s*거래(?:\s*건수)?|"
        r"전월세\s*거래(?:\s*건수)?)"
        r"(?:는|가|는\s*총|가\s*총|가\s*집계됐다)?"
        r"[\s:]*"
        r"(\d[\d,]*)\s*건"
    )

    for match in re.finditer(total_pattern, content):
        reported = int(match.group(1).replace(",", ""))

        if reported != total:
            issues.append(make_issue(
                "NUMBER",
                match.group(0),
                f"전체 거래는 {total:,}건이지만 "
                f"기사에는 {reported:,}건으로 작성됐습니다.",
                f"전체 거래 건수를 {total:,}건으로 수정하세요.",
            ))

    # 월세 비중이 명시된 문장 확인
    ratio = Decimal(str(facts["wolse_ratio_percent"]))

    ratio_pattern = (
        r"월세(?:\s*거래)?(?:\s*비중)?"
        r"[^.!?\n]{0,60}?"
        r"(\d+(?:\.\d+)?)\s*%"
    )

    for match in re.finditer(ratio_pattern, content):
        reported = Decimal(match.group(1))

        # 소수 첫째 자리 표시의 반올림 차이 허용
        if abs(reported - ratio) > Decimal("0.051"):
            issues.append(make_issue(
                "PERCENT",
                match.group(0),
                f"Fact Sheet의 월세 비중은 {ratio}%입니다.",
                "월세 비중을 Fact Sheet 수치와 일치시키세요.",
            ))

    # 명시적으로 'YYYY년 M월 전체 거래 N건'인 경우 대조
    # 전세·월세 건수가 혼재된 문장은 AI 문맥 검사에 맡긴다.
    month_pattern = (
        r"(\d{4})년\s*(\d{1,2})월"
        r"\s*(?:전체\s*)?거래(?:\s*건수)?"
        r"\s*(?:는|가|은|총)?\s*"
        r"(\d[\d,]*)\s*건"
    )

    for match in re.finditer(month_pattern, content):
        year = int(match.group(1))
        month = int(match.group(2))
        reported = int(match.group(3).replace(",", ""))

        key = f"{year}-{month:02d}"

        if key not in monthly:
            continue

        expected = int(monthly[key]["total_count"])

        if reported != expected:
            issues.append(make_issue(
                "NUMBER",
                match.group(0),
                f"{key} 전체 거래는 {expected}건입니다.",
                f"{expected}건으로 수정하세요.",
            ))

    return issues


# ==================================================
# 5. AI 문맥·근거 검사
# ==================================================

def check_context(data):
    article = data["article"]

    # AI에 수천 개의 거래 ID를 보내지 않는다.
    evidence = data["evidence"]

    evidence_context = {
        "source_org": evidence.get("source_org"),
        "source_url": evidence.get("source_url"),
        "calculation": evidence.get("calculation"),
        "limitations": evidence.get("limitations"),
    }

    instructions = """
당신은 한국 데이터 저널리즘 기사의 사실 검수자다.

목적은 기사에서 실제로 수정해야 하는 문제를 찾는 것이다.

[가장 중요한 원칙]

- 정확한 문장은 오류가 아니다.
- 정상적인 수치, 출처 설명, 한계 설명을 오류로 보고하지 마라.
- 긍정적인 평가나 칭찬은 findings에 넣지 마라.
- 문제가 없는 문장에 대해 설명할 필요가 없다.
- 단순한 문체 취향은 오류로 취급하지 마라.
- 같은 문제를 반복 지적하지 마라.
- 실제 문제가 없다면 findings=[]를 반환하라.

[검사할 문제]

1. Fact Sheet와 다른 수치, 계약월 또는 거래 유형
2. 월별 증감이 반복되는데 지속적인 추세로 과장한 표현
3. 근거 없는 정책 효과, 인과관계, 시장 위축 단정
4. 공식 원자료와 자체 수집·처리 방식의 혼동
5. RAG 문서가 뒷받침하지 않는 사실 설명
6. 통계의 의미를 부당하게 확장한 해석
7. 신고 지연이나 자료 정정 가능성을 누락하거나 왜곡한 설명

[특히 주의할 사항]

국토교통부 API는 개별 거래 자료를 제공한다.
우리가 계산한 월별 거래 건수는 자체 분석 결과다.
둘을 혼동해서는 안 된다.

같은 계약 조건의 거래를 각각 보존한 것은
수집 프로그램의 처리 방식이다.
국토교통부의 공식 거래 식별 기준이라고 서술해서는 안 된다.

정책이나 시장 변화의 원인은 입력 근거가
충분하지 않으면 단정하지 않는다.

전세·월세 비중은 거래 건수 기준이며
주택 재고 비중이나 임차인 전체의 비중이 아니다.

기사 속 숫자는 Fact Sheet의 해당 월과
전세·월세 유형에 맞는지 확인하라.

[결과 작성 규칙]

finding마다 다음 필드를 채운다.

category:
NUMBER, TREND, CAUSALITY, SOURCE,
INTERPRETATION, LIMITATION 중 하나

excerpt:
기사에 실제로 있는 문제 표현을 정확히 인용

is_error:
실제 수정이 필요한 문제면 true
정상적인 내용이면 false

reason:
어떤 근거와 충돌하는지 구체적으로 설명

suggestion:
Fact Sheet와 RAG에 근거한 수정 방향

확실한 오류나 근거 부족 주장이 아닌 경우
finding을 만들지 마라.

새로운 수치를 계산하지 마라.
기사 본문을 수정하거나 승인하지 마라.
"""

    review_input = {
        "article": {
            "title": article["title"],
            "content": article["content"],
        },
        "topic": data["topic"],
        "period_start": data["period_start"],
        "period_end": data["period_end"],
        "facts": data["facts"],
        "evidence": evidence_context,
        "references": data["references"],
    }

    response = client.responses.parse(
        model=MODEL,
        instructions=instructions,
        input=json.dumps(
            review_input,
            ensure_ascii=False,
            default=str,
        ),
        text_format=AIReview,
    )

    if response.output_parsed is None:
        raise RuntimeError("AI 검수 결과를 읽지 못했습니다.")

    issues = []
    article_text = article["title"] + "\n" + article["content"]

    for finding in response.output_parsed.findings:

        # 정상 판정은 오류 목록에서 제외
        if not finding.is_error:
            continue

        excerpt = finding.excerpt.strip()

        # 실제 기사에 존재하는 표현인지 확인
        if not excerpt or excerpt not in article_text:
            print(
                "[참고] 원문과 일치하지 않는 AI 지적 제외:",
                excerpt[:80],
            )
            continue

        issues.append(make_issue(
            finding.category,
            excerpt,
            finding.reason,
            finding.suggestion,
        ))

    return issues


# ==================================================
# 6. 검수 결과 저장
# ==================================================

def save_result(data, result):
    article = data["article"]

    with engine.begin() as conn:
        updated = conn.execute(
            text("""
                UPDATE articles
                SET validation_result = :result,
                    validation_passed = :passed,
                    updated_at = NOW()
                WHERE id = :id
                  AND title = :original_title
                  AND content = :original_content
                  AND status = '검토대기'
            """),
            {
                "id": article["id"],
                "original_title": article["title"],
                "original_content": article["content"],
                "result": json.dumps(
                    result,
                    ensure_ascii=False,
                ),
                "passed": result["passed"],
            },
        )

        if updated.rowcount != 1:
            raise RuntimeError(
                "검수 도중 기사 내용이나 상태가 변경됐습니다. "
                "다시 검수하세요."
            )


# ==================================================
# 7. 전체 검수 실행
# ==================================================

def main():
    article_id = (
        int(sys.argv[1])
        if len(sys.argv) > 1
        else 1
    )

    print("===== RentWatch Seoul 자동 검수 =====")
    print("기사 ID:", article_id)

    data = load_article(article_id)
    article = data["article"]

    if article["status"] != "검토대기":
        raise RuntimeError(
            "검토대기 상태의 기사만 검수할 수 있습니다."
        )

    print("Fact Sheet ID:", data["fact_id"])
    print("Fact Sheet 검증: PASSED")

    print("\n1. Python 수치 검사")
    number_issues = check_numbers(
        article,
        data["facts"],
    )

    print("수치 검사 오류:", len(number_issues), "건")

    print("\n2. AI 문맥·근거 검사")
    semantic_issues = check_context(data)

    print("문맥 검사 오류:", len(semantic_issues), "건")

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
    article["title"],
    article["content"],
),
    }

    save_result(data, result)

    print("\n===== 검수 결과 =====")

    if issues:
        for index, issue in enumerate(issues, 1):
            print(f"\n문제 {index}")
            print("유형:", issue["category"])
            print("문장:", issue["excerpt"])
            print("이유:", issue["reason"])
            print("수정 방향:", issue["suggestion"])
    else:
        print("이번 검수에서 발견된 문제가 없습니다.")

    print("\n최종 검수:", "PASS" if passed else "FAIL")
    print("문제 건수:", len(issues))
    print("MySQL 검수 결과 저장: 완료")
    print("기사 상태: 검토대기")
    print("사람의 최종 승인: 별도 필요")


if __name__ == "__main__":
    main()
