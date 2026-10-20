
import os
import sys
import json
import math
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv(
    "OPENAI_EMBEDDING_MODEL",
    "text-embedding-3-small",
)

SOURCE_FILE = Path("references/molit_officetel.txt")
SOURCE_TITLE = "국토교통부_오피스텔 전월세 실거래가 자료"
SOURCE_ORG = "국토교통부"
SOURCE_URL = "https://www.data.go.kr/data/15126475/openapi.do"

if not DATABASE_URL or not API_KEY:
    raise RuntimeError(
        ".env에 DATABASE_URL 또는 OPENAI_API_KEY가 없습니다."
    )

client = OpenAI(api_key=API_KEY)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def split_text(content, max_chars=600):
    """본문을 최대 600자 길이의 검색 구간으로 나눈다."""
    paragraphs = [
        p.strip()
        for p in content.split("\n")
        if p.strip()
    ]

    chunks = []
    current = ""

    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 1 > max_chars:
            if current:
                chunks.append(current)
                current = ""

        # 긴 문단도 빠뜨리지 않고 나눈다.
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]

        if paragraph:
            current = (
                current + "\n" + paragraph
                if current else paragraph
            )

    if current:
        chunks.append(current)

    return chunks


def embed(content):
    """OpenAI 임베딩 API를 이용해 텍스트를 벡터로 변환한다."""
    response = client.embeddings.create(
        model=MODEL,
        input=content,
    )
    return response.data[0].embedding


def register_document():
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(
            f"참고문서가 없습니다: {SOURCE_FILE}"
        )

    content = SOURCE_FILE.read_text(
        encoding="utf-8"
    ).strip()

    if not content:
        raise RuntimeError("참고문서가 비어 있습니다.")

    chunks = split_text(content)

    # 이미 등록된 문서는 중복 저장하지 않는다.
    with engine.connect() as conn:
        existing = conn.execute(
            text("""
                SELECT chunk_index, content
                FROM reference_documents
                WHERE source_url = :url
                ORDER BY chunk_index
            """),
            {"url": SOURCE_URL},
        ).mappings().all()

    if existing:
        saved_chunks = [row["content"] for row in existing]
        if saved_chunks == chunks:
            print("이미 동일한 문서가 등록되어 있습니다.")
            return

        raise RuntimeError(
            "같은 URL의 다른 문서 내용이 이미 있습니다. "
            "기존 근거를 덮어쓰지 않습니다."
        )

    print("문서 구간:", len(chunks), "개")

    records = []

    for index, chunk in enumerate(chunks):
        vector = embed(chunk)
        records.append({
            "title": SOURCE_TITLE,
            "org": SOURCE_ORG,
            "url": SOURCE_URL,
            "index": index,
            "content": chunk,
            "embedding": json.dumps(vector),
        })
        print(f"임베딩 생성: {index + 1}/{len(chunks)}")

    # 모든 임베딩이 만들어진 뒤 한 번에 DB 저장
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO reference_documents (
                    title, source_org, source_url,
                    published_at, chunk_index,
                    content, embedding, collected_at
                )
                VALUES (
                    :title, :org, :url,
                    NULL, :index,
                    :content, :embedding, NOW()
                )
            """),
            records,
        )

    print("RAG 참고문서 저장 완료:", len(records), "개 구간")


def cosine_similarity(a, b):
    """두 벡터가 의미상 얼마나 가까운지 계산한다."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if not norm_a or not norm_b:
        return 0.0

    return dot / (norm_a * norm_b)


def search_documents(question, top_k=3):
    question_vector = embed(question)

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, title, source_org,
                       source_url, chunk_index,
                       content, embedding
                FROM reference_documents
                WHERE embedding IS NOT NULL
            """)
        ).mappings().all()

    results = []

    for row in rows:
        vector = json.loads(row["embedding"])

        # 다른 차원의 임베딩과는 비교하지 않는다.
        if len(vector) != len(question_vector):
            continue

        score = cosine_similarity(
            question_vector, vector
        )

        results.append({
            "id": row["id"],
            "title": row["title"],
            "org": row["source_org"],
            "url": row["source_url"],
            "chunk_index": row["chunk_index"],
            "content": row["content"],
            "score": score,
        })

    results.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    return results[:top_k]


def main():
    if len(sys.argv) < 2:
        print("사용법:")
        print("python rag.py ingest")
        print('python rag.py search "검색할 질문"')
        return

    command = sys.argv[1]

    if command == "ingest":
        register_document()

    elif command == "search":
        question = (
            " ".join(sys.argv[2:]).strip()
            or "오피스텔 전월세 실거래가 자료의 공개 기준은?"
        )

        results = search_documents(question)

        if not results:
            print("검색 결과가 없습니다.")
            return

        for rank, result in enumerate(results, 1):
            print(f"\n===== 검색 결과 {rank} =====")
            print("근거 ID:", result["id"])
            print("관련도:", round(result["score"], 4))
            print("발행기관:", result["org"])
            print("문서명:", result["title"])
            print("원문 URL:", result["url"])
            print("문서 구간:", result["chunk_index"])
            print("본문:", result["content"])

    else:
        raise ValueError("ingest 또는 search를 입력하세요.")


if __name__ == "__main__":
    main()
