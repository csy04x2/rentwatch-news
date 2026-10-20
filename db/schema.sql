CREATE DATABASE IF NOT EXISTS rentwatch_seoul CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE rentwatch_seoul;
-- MySQL 8.4 / API 응답 확인 전 source_row_key 생성 규칙은 미확정
CREATE TABLE rental_transactions (
 id BIGINT PRIMARY KEY AUTO_INCREMENT,
 property_type VARCHAR(30) NOT NULL, district_code CHAR(5) NOT NULL,
 district_name VARCHAR(50) NOT NULL, building_name VARCHAR(200),
 contract_date DATE NOT NULL, area_sqm DECIMAL(10,2) NOT NULL,
 deposit_manwon BIGINT NOT NULL, rent_manwon BIGINT NOT NULL,
 floor_no SMALLINT, raw_data JSON NOT NULL,
 source_row_key VARCHAR(128) NOT NULL UNIQUE,
 collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT chk_area CHECK(area_sqm > 0),
 CONSTRAINT chk_deposit CHECK(deposit_manwon >= 0),
 CONSTRAINT chk_rent CHECK(rent_manwon >= 0)
) ENGINE=InnoDB;
CREATE TABLE reference_documents (
 id BIGINT PRIMARY KEY AUTO_INCREMENT,
 title VARCHAR(300) NOT NULL, source_org VARCHAR(100) NOT NULL,
 source_url TEXT NOT NULL, published_at DATE,
 chunk_index INT NOT NULL, content TEXT NOT NULL,
 embedding JSON, collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT chk_chunk CHECK(chunk_index >= 0)
) ENGINE=InnoDB;
CREATE TABLE fact_sheets (
 id BIGINT PRIMARY KEY AUTO_INCREMENT,
 topic VARCHAR(300) NOT NULL, region_code CHAR(5) NOT NULL,
 property_type VARCHAR(30) NOT NULL, period_start DATE NOT NULL,
 period_end DATE NOT NULL, query_filters JSON NOT NULL,
 facts_json JSON NOT NULL, evidence_json JSON NOT NULL,
 verification_status ENUM('PENDING','PASSED','FAILED') NOT NULL DEFAULT 'PENDING',
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT chk_period CHECK(period_end >= period_start)
) ENGINE=InnoDB;
CREATE TABLE articles (
 id BIGINT PRIMARY KEY AUTO_INCREMENT,
 title VARCHAR(500) NOT NULL, content LONGTEXT NOT NULL,
 status ENUM('검토대기','승인','보류') NOT NULL DEFAULT '검토대기',
 generated_model VARCHAR(100) NOT NULL, prompt_version VARCHAR(50) NOT NULL,
 validation_result JSON, validation_passed BOOLEAN NOT NULL DEFAULT FALSE,
 reviewed_by VARCHAR(100), reviewed_at DATETIME,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 CONSTRAINT chk_approved CHECK(status <> '승인' OR
    (validation_passed = TRUE AND validation_result IS NOT NULL
     AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
) ENGINE=InnoDB;
CREATE TABLE article_sources (
 id BIGINT PRIMARY KEY AUTO_INCREMENT, article_id BIGINT NOT NULL,
 fact_sheet_id BIGINT, reference_document_id BIGINT,
 evidence_role VARCHAR(30) NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT fk_article FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE RESTRICT,
 CONSTRAINT fk_fact FOREIGN KEY(fact_sheet_id) REFERENCES fact_sheets(id) ON DELETE RESTRICT,
 CONSTRAINT fk_doc FOREIGN KEY(reference_document_id) REFERENCES reference_documents(id) ON DELETE RESTRICT,
 CONSTRAINT chk_source_target CHECK(
   (fact_sheet_id IS NOT NULL AND reference_document_id IS NULL)
   OR (fact_sheet_id IS NULL AND reference_document_id IS NOT NULL)),
 CONSTRAINT chk_source_role CHECK(
   (evidence_role='STATISTIC' AND fact_sheet_id IS NOT NULL)
   OR (evidence_role='CONTEXT' AND reference_document_id IS NOT NULL))
) ENGINE=InnoDB;

-- 기자 검수 기록 (기존 DB에는 review_migration.sql을 별도 실행)

CREATE TABLE IF NOT EXISTS review (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  article_id BIGINT NOT NULL,
  reviewed_by VARCHAR(100) NOT NULL,
  result ENUM('PASS','FAIL') NOT NULL,
  checks_json JSON NOT NULL,
  opinion TEXT NOT NULL,
  content_hash CHAR(64) NOT NULL,
  reviewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_review_article FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE RESTRICT,
  INDEX idx_review_article_latest (article_id, id)
) ENGINE=InnoDB;
