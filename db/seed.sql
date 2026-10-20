-- 가상 테스트 거래 5건 (실제 국토교통부 신고 자료 아님)
USE rentwatch_seoul;
INSERT INTO rental_transactions
(property_type,district_code,district_name,building_name,contract_date,area_sqm,
 deposit_manwon,rent_manwon,floor_no,raw_data,source_row_key,collected_at)
VALUES
('OFFICETEL','11620','관악구','가상 오피스텔 A','2025-10-15',24.30,1000,65,5,JSON_OBJECT('sample',true,'sample_id',1),'SEED-SAMPLE-001',NOW()),
('OFFICETEL','11620','관악구','가상 오피스텔 B','2025-11-04',31.50,2000,75,7,JSON_OBJECT('sample',true,'sample_id',2),'SEED-SAMPLE-002',NOW()),
('OFFICETEL','11620','관악구','가상 오피스텔 C','2026-01-20',26.10,1500,70,3,JSON_OBJECT('sample',true,'sample_id',3),'SEED-SAMPLE-003',NOW()),
('OFFICETEL','11620','관악구','가상 오피스텔 D','2026-05-09',42.00,17000,0,10,JSON_OBJECT('sample',true,'sample_id',4),'SEED-SAMPLE-004',NOW()),
('OFFICETEL','11620','관악구','가상 오피스텔 E','2026-09-17',29.80,500,80,6,JSON_OBJECT('sample',true,'sample_id',5),'SEED-SAMPLE-005',NOW())
ON DUPLICATE KEY UPDATE source_row_key=source_row_key;
