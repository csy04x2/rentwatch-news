
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(".env에 DATABASE_URL이 없습니다.")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

DRAFT_FILE = Path("article_draft.txt")


def load_article(article_id):
    """MySQL에서 기사 제목과 본문을 가져온다."""
    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT id, title, content, status
                FROM articles
                WHERE id = :id
            """),
            {"id": article_id},
        ).mappings().first()

    if row is None:
        raise RuntimeError("기사를 찾을 수 없습니다.")

    if row["status"] != "검토대기":
        raise RuntimeError(
            "검토대기 상태의 기사만 수정할 수 있습니다."
        )

    return dict(row)


def export_article(article_id):
    """기사 내용을 텍스트 파일로 내보낸다."""
    article = load_article(article_id)

    if DRAFT_FILE.exists():
        raise RuntimeError(
            f"{DRAFT_FILE} 파일이 이미 있습니다. "
            "기존 수정 내용 보호를 위해 덮어쓰지 않습니다."
        )

    content = (
        article["title"].strip()
        + "\n\n"
        + article["content"].strip()
        + "\n"
    )

    DRAFT_FILE.write_text(content, encoding="utf-8")

    print("기사 ID:", article_id)
    print("편집 파일:", DRAFT_FILE)
    print("VS Code에서 수정한 후 저장하세요.")


def save_article(article_id):
    """편집 파일의 내용을 DB에 저장한다."""
    original = load_article(article_id)

    if not DRAFT_FILE.exists():
        raise RuntimeError(
            "article_draft.txt 파일이 없습니다."
        )

    content = DRAFT_FILE.read_text(
        encoding="utf-8"
    ).strip()

    parts = content.split("\n\n", 1)

    if len(parts) != 2:
        raise RuntimeError(
            "첫 줄에 제목을 쓰고 한 줄을 비운 뒤 "
            "본문을 작성하세요."
        )

    title = parts[0].strip()
    body = parts[1].strip()

    if not title or not body:
        raise RuntimeError(
            "제목과 본문은 비어 있을 수 없습니다."
        )

    if "\n" in title or len(title) > 500:
        raise RuntimeError(
            "제목은 한 줄이며 최대 500자입니다."
        )

    if (
        title == original["title"]
        and body == original["content"].strip()
    ):
        print("변경된 내용이 없습니다.")
        return

    # 수정과 검수 결과 무효화를 하나의 DB 작업으로 처리
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE articles
                SET title = :title,
                    content = :body,
                    validation_result = NULL,
                    validation_passed = FALSE,
                    status = '검토대기',
                    reviewed_by = NULL,
                    reviewed_at = NULL,
                    updated_at = NOW()
                WHERE id = :id
                  AND status = '검토대기'
                  AND title = :old_title
                  AND content = :old_body
            """),
            {
                "title": title,
                "body": body,
                "id": article_id,
                "old_title": original["title"],
                "old_body": original["content"],
            },
        )

        if result.rowcount != 1:
            raise RuntimeError(
                "편집 도중 원본 기사가 변경됐습니다. "
                "저장을 중단합니다."
            )

    print("기사 수정 저장 완료")
    print("기사 ID:", article_id)
    print("검수 상태: 무효화")
    print("기사 상태: 검토대기")
    print("다음 작업: python validate_article.py", article_id)


def main():
    if len(sys.argv) != 3:
        print("사용법:")
        print("python edit_article.py export 1")
        print("python edit_article.py save 1")
        return

    action = sys.argv[1]
    article_id = int(sys.argv[2])

    if action == "export":
        export_article(article_id)
    elif action == "save":
        save_article(article_id)
    else:
        raise ValueError("export 또는 save를 입력하세요.")


if __name__ == "__main__":
    main()
