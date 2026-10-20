USE rentwatch_seoul;
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
