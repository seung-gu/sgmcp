-- PostgreSQL의 확장형 API (pgvector, PostGIS, pg_search, TimescaleDB, Citus, pg_cron...)

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_search;  -- Neon 새 버전에서 지원 안됨

SELECT  name,default_version, comment
FROM pg_available_extensions;
--WHERE name IN ('postgis', 'vector', 'pg_search', 'pg_trgm', 'pg_stat_statements');


-- PostGIS

-- Type 2가지
-- geometry - 평면 좌표 (빠르고 함수가 많으나 위경도를 그대로 넣으면 거리 계산이 의미 없는 값 나옴)
-- geography - 곡면 좌표 (거리가 미터로 정확히 나오지만, 느리고 지원 함수가 적음)

-- SRID - 좌표계 번호
--      4326: WGS84 위경도, GPS가 주는 값
--      3857: Web Mercator,, 구글맵 등 웹 지도 표시용


-- ST_MakePoint(x, y): 하나의 점을 만드는 함수
-- ST_AsText: 공간 객체를 WKT 라는 사람이 읽기 좋은 형태로 변환
--> PostGIS의 좌표 순서는 (경도, 위도)
SELECT ST_AsText(ST_MakePoint(127.0276, 37.4979));

-- st_setsrid : WGS84 좌표계라는 정보
SELECT st_astext(
    st_setsrid(ST_MakePoint(127.0276, 37.4979), 4326)::geography
);

-- Web이 받기 편한 정보
SELECT st_asgeojson(
    st_setsrid(ST_MakePoint(127.0276, 37.4979), 4326)::geography
);


CREATE TABLE branches (
    branch_id  BIGSERIAL PRIMARY KEY,
    name       VARCHAR(100) NOT NULL,
    city       VARCHAR(50)  NOT NULL,
    location   GEOGRAPHY(POINT, 4326) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO branches (name, city, location) VALUES
    ('강남역 지점',   '서울', ST_SetSRID(ST_MakePoint(127.0276, 37.4979), 4326)),
    ('홍대입구 지점', '서울', ST_SetSRID(ST_MakePoint(126.9236, 37.5568), 4326)),
    ('여의도 지점',   '서울', ST_SetSRID(ST_MakePoint(126.9243, 37.5219), 4326)),
    ('명동 지점',     '서울', ST_SetSRID(ST_MakePoint(126.9830, 37.5635), 4326)),
    ('잠실 지점',     '서울', ST_SetSRID(ST_MakePoint(127.1000, 37.5133), 4326)),
    ('부평 지점',     '인천', ST_SetSRID(ST_MakePoint(126.7248, 37.4898), 4326)),
    ('서면 지점',     '부산', ST_SetSRID(ST_MakePoint(129.0593, 35.1581), 4326)),
    ('해운대 지점',   '부산', ST_SetSRID(ST_MakePoint(129.1636, 35.1630), 4326)),
    ('동성로 지점',   '대구', ST_SetSRID(ST_MakePoint(128.5932, 35.8699), 4326)),
    ('제주공항 지점', '제주', ST_SetSRID(ST_MakePoint(126.4930, 33.5070), 4326));


SELECT
    name,
    city,
    ST_X(location::geometry) AS lon,
    ST_Y(location::geometry) AS lat,
    ST_SRID(location) AS srid,
    ST_GeometryType(location::geometry) AS geom_type
FROM branches
ORDER BY branch_id;


-- 두 지점 사이의 거리
SELECT
    b2.name,
    b2.city,
    ROUND(ST_Distance(b1.location, b2.location)::NUMERIC, 0) AS distance_m
FROM branches b1, branches b2
WHERE b1.branch_id = 1
    AND b2.branch_id <> 1
ORDER BY distance_m;


-- 특정 위치에서 N km 이내의 지점을 찾는 쿼리
SELECT name, city,
    ROUND(
        ST_Distance(
            location,
            ST_SetSRID(ST_MakePoint(127.0276, 37.4979), 4326)::geography
        )::NUMERIC, 0
    ) AS distance_m
FROM branches
WHERE ST_DWithin(
    location, -- A
    ST_SetSRID(ST_MakePoint(127.0276, 37.4979), 4326)::geography, -- B
    10000 -- RADIUS
)
ORDER BY distance_m;
--> ST_DWithn: A와 B 사이의 거리가 10000 반지름 이하인지. 내부 공간 인덱스를 탈 수 있음


-- GiST
--> 반경이 주어지지 않은 가장 가까운 곳 찾기. ex) 가까운 카페 찾기
--> limit 같은거 쓰면 모든 행을 다 일일이 계산해야하기 때문에 KNN 전용 연산자 사용하는게 좋음 (GiST 인덱스)
SELECT name, city,
    ROUND(ST_Distance(
        location,
        ST_SetSRID(ST_MakePoint(127.0276, 37.4979), 4326)::geography
    )::NUMERIC, 0
) AS distance_m
FROM branches
ORDER BY location <-> ST_SetSRID(ST_MakePoint(127.0276, 37.4979), 4326)::geography --> 핵심
LIMIT 5;

-- 다각형 안에 포함되어 있는지 확인
-- ex) 서울시 안에 들어있는 도서관
-- ST_Contains, ST_Intersects 등등 함수도 있음
SELECT name, city
FROM branches
WHERE ST_Within(
    location::geometry,
    ST_GeomFromText(
        'POLYGON((126.80 37.70, 127.20 37.70, 127.20 37.40, 126.80 37.40, 126.80 37.70))',
        4326
    )
);

-- Gist 테스트

-- 더미데이터 생성
INSERT INTO branches (name, city, location)
SELECT
    '더미 지점 ' || g AS name,
    '더미시' AS city,
    ST_SetSRID(
        ST_MakePoint(
            124.0 + random() * 8.0,
            33.0 + random() * 6.0
        ),
        4326
    )::geography AS location
FROM generate_series(1, 100000) g;

SELECT COUNT(*) FROM branches;

ANALYZE branches;

EXPLAIN ANALYZE
SELECT COUNT(*)
FROM branches
WHERE ST_DWithin(
    location,
    ST_SetSRID(ST_MakePoint(127.0276, 37.4979), 4326)::geography,
    5000
);

-- GiST 인덱스 생성 (일반적인 B-Tree는 공간데이터에서 사용 불가)
CREATE INDEX idx_branches_location ON branches USING GIST (location);

DELETE FROM branches WHERE city = '더미시';



-- pg_search
-- 보통은 검색을 위해선 DB에 별도 Elasticsearch 같은 검색 엔진을 도입해야하나 (검색용 Index 조회)
-- 별도 검색 엔진을 도입하면, DB와 같이 쓰기에 번거로움: 트랜젝션, ACID, 동기화 등등 데이터 정합성 (이중 쓰기) 문제
-- PostgreSQL은 pg_search라는 확장을 제공해서 복잡도를 낮춰줌
-- 기존의 PostgreSQL의 기본 검색은 단점이 명확: 
--  랭킹에 약함 (Elasticsearch의 BM25 수준 점수 체계 부재)
--  Fuzzy 매칭 약함 (오타/부분/유사어 처리 제한)
--  고급 기능 부재
-- -> pg_search가 이를 해결
-- 
-- pg_search의 기능: BM25 스코어링
--  TF 포화 적용 (단어 등장 빈도 많을수록 (무한정은 X) 스코어 높음)
--  문서가 짧을수록 스코어 높음 (짧은 문서에서 특정 단어 등장하는게 매칭도가 더 큼)
--  IDF 희소성: 드문 단어일수록 스코어 높음
--
-- pg_search는 PostgreSQL가 만든게 아니라, 
--   Elastic Search와 Solr의 자바 라이브러리에서 영감을 받아 Tantivy Rust 위에 만들어짐

CREATE TABLE transactions (
    transaction_id BIGSERIAL PRIMARY KEY,
    account_id     BIGINT NOT NULL,
    customer_tier  VARCHAR(20) NOT NULL,
    description    TEXT NOT NULL,
    amount         NUMERIC(15,2) NOT NULL,
    branch_id      BIGINT REFERENCES branches(branch_id),
    metadata       JSONB,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO transactions (account_id, customer_tier, description, amount, branch_id, metadata, created_at) VALUES
    (101, 'VIP',     '강남 백화점 카드 결제',              450000, 1, '{"channel":"offline","category":"shopping"}', NOW() - INTERVAL '1 day'),
    (102, 'GENERAL', '편의점 카드 결제',                    12500, 1, '{"channel":"offline","category":"food"}',     NOW() - INTERVAL '2 day'),
    (103, 'GENERAL', '온라인 쇼핑몰 결제',                 89000, 2, '{"channel":"online","category":"shopping"}',  NOW() - INTERVAL '3 day'),
    (104, 'VIP',     '해외 카드 결제 환전 수수료 포함',    720000, 3, '{"channel":"offline","category":"shopping"}', NOW() - INTERVAL '5 day'),
    (105, 'GENERAL', 'ATM 현금 출금',                      100000, 4, '{"channel":"atm","category":"cash"}',         NOW() - INTERVAL '7 day'),
    (106, 'GENERAL', '계좌 이체 송금 완료',               500000, 5, '{"channel":"mobile","category":"transfer"}',  NOW() - INTERVAL '10 day'),
    (107, 'VIP',     '급여 입금 처리',                    3200000, 3, '{"channel":"batch","category":"income"}',     NOW() - INTERVAL '15 day'),
    (108, 'GENERAL', '월급 입금 급여 계정',               2500000, 4, '{"channel":"batch","category":"income"}',     NOW() - INTERVAL '15 day'),
    (101, 'VIP',     '공과금 자동 납부 전기 요금',          52000, 1, '{"channel":"auto","category":"utility"}',     NOW() - INTERVAL '20 day'),
    (102, 'GENERAL', '관리비 자동 이체',                  180000, 2, '{"channel":"auto","category":"utility"}',     NOW() - INTERVAL '20 day'),
    (109, 'VIP',     '자동차 할부 결제',                  450000, 5, '{"channel":"auto","category":"loan"}',        NOW() - INTERVAL '22 day'),
    (110, 'GENERAL', '카드론 이자 납부',                   75000, 6, '{"channel":"auto","category":"loan"}',        NOW() - INTERVAL '25 day'),
    (111, 'VIP',     '예금 이자 입금',                     38000, 7, '{"channel":"batch","category":"income"}',     NOW() - INTERVAL '28 day'),
    (112, 'GENERAL', '보험료 자동 납부',                  125000, 8, '{"channel":"auto","category":"insurance"}',   NOW() - INTERVAL '30 day'),
    (113, 'VIP',     '마트 할인 쇼핑 카드 결제',          235000, 9, '{"channel":"offline","category":"shopping"}', NOW() - INTERVAL '3 day'),
    (114, 'GENERAL', '주차료 결제',                         5000, 1, '{"channel":"offline","category":"transport"}',NOW() - INTERVAL '4 day'),
    (115, 'GENERAL', '주유소 결제 카드',                   85000, 2, '{"channel":"offline","category":"transport"}',NOW() - INTERVAL '5 day'),
    (116, 'VIP',     '레스토랑 결제 고액',                550000, 4, '{"channel":"offline","category":"food"}',     NOW() - INTERVAL '6 day'),
    (117, 'GENERAL', '카페 결제',                           8500, 5, '{"channel":"offline","category":"food"}',     NOW() - INTERVAL '6 day'),
    (118, 'GENERAL', '대중교통 정기권 결제',                55000, 1, '{"channel":"offline","category":"transport"}',NOW() - INTERVAL '8 day'),
    (119, 'VIP',     '부동산 중개 수수료 납부',          1200000, 3, '{"channel":"offline","category":"realestate"}',NOW() - INTERVAL '12 day'),
    (120, 'GENERAL', '학원 수강료 납부',                  320000, 6, '{"channel":"online","category":"education"}', NOW() - INTERVAL '14 day'),
    (121, 'GENERAL', '병원 진료비 결제',                   45000, 7, '{"channel":"offline","category":"health"}',   NOW() - INTERVAL '16 day'),
    (122, 'VIP',     '해외 송금 완료 달러',              2800000, 8, '{"channel":"mobile","category":"transfer"}',  NOW() - INTERVAL '18 day'),
    (123, 'GENERAL', '통신비 자동 납부',                   88000, 9, '{"channel":"auto","category":"utility"}',     NOW() - INTERVAL '21 day'),
    (124, 'VIP',     '골프장 이용료 카드 결제',           420000, 8,'{"channel":"offline","category":"leisure"}', NOW() - INTERVAL '23 day'),
    (125, 'GENERAL', '서점 도서 결제',                     32000,10, '{"channel":"offline","category":"shopping"}', NOW() - INTERVAL '26 day'),
    (126, 'GENERAL', '온라인 스트리밍 정기 결제',          14900, 1, '{"channel":"online","category":"subscription"}',NOW() - INTERVAL '27 day'),
    (127, 'VIP',     '항공권 결제 일등석',               4500000, 2, '{"channel":"online","category":"travel"}',    NOW() - INTERVAL '29 day'),
    (128, 'GENERAL', '호텔 숙박 결제',                    280000, 3, '{"channel":"online","category":"travel"}',    NOW() - INTERVAL '40 day');


CREATE  INDEX trans_search ON transactions
USING BM25 (transaction_id, description)
WITH(key_field = 'transaction_id');

SELECT transaction_id, description, amount
FROM transactions
WHERE description @@@'카드 결제'
ORDER BY transaction_id;

SELECT
    transaction_id,
    description,
    paradedb.score(transaction_id) AS score
    FROM transactions
WHERE description @@@ '카드 결제'
ORDER BY score DESC
LIMIT  10;

SELECT transaction_id, description
    FROM transactions
WHERE description @@@ '결제했다';

-- '편의점 카드 결제' -> ["편의점", "카드", "결제"]
-- 결제했다, 결제하면, 결제는, 결제도

-- n-gram

DROP INDEX trans_search;

CREATE INDEX trans_search_idx ON transactions
USING bm25 (transaction_id, description)
WITH (
key_field = 'transaction_id',
text_fields = '{
        "description": {
            "tokenizer": {
                "type": "ngram",
                "min_gram": 2,
                "max_gram": 3,
                "prefix_only": false
            }
        }
    }'
);
-- "결제했다" -> `["결제", "제했", "했다", "결제했", "제했다"]`

SELECT transaction_id, description
FROM transactions
WHERE description @@@ paradedb.match('description', '결제했다') LIMIT 5;

-- tsvector

--> to_tsquery("카드 & 결제")
--> score기반, 즉 점수 기반 정렬 (ts_rank), BM25


-- 분산, 크론 스케쥴링
-- 데이터의 종류 확장 (pg_search, postGIS) 같은게 아닌, 처리 모델 자체의 확장 (인프라 영역)

-- 코디네이터, 샤딩 -> 분산 데이터 (다중 스레드 처럼) 관리
--   보통 cron/Airflow 스케줄러를 별도로 달고 하는데 그러면 복잡
-- PostgreSQL에선 pg_cron이라는거를 내부에서 달아놓으면 추가 유지보수, 복잡성 없이 가능
-- 파티셔닝은 한 서버 안에서 테이블을 조각내는 것
-- 샤딩은 여러 서버에 조각을 나눠 배치하는 것
/*
Citus
    coordinator   "account_id=42는 7번 샤드네"
        │
   ┌────┼─────┬──────┐
┌──▼─┐┌─▼──┐┌─▼───┐┌─▼───┐
│샤드 ││샤드 ││샤드  ││샤드  │   ← worker 4대에 나눠 담음
│1-8 ││9-16││17-24││25-32│      각 2500만 행씩
└────┘└────┘└─────┘└─────┘
*/

CREATE EXTENSION citus;

-- 코디네이터는 컬럼에 따라 샤딩된 샤드들 (1,2,3,4,...)를 어떻게 참고할지 고려하는데,
-- 이때 master table이라는 table도 제공함. 이는 전체 테이블들에 공통적으로 자주 참조해야하는 테이블로써,
-- 시작할 때 각 샤드들에 이 master table을 다 복사해 넣고, 바로바로 join해서 참조 할 수 있게 함

-- master table (참조 테이블) 등록:
SELECT create_reference_table('branches'); -- select 함수 (citus 전용 함수)

-- Primary key는 각 워커 안에서만 유니크함이 보장되므로, 항상 acocunt_id 같은 분산 컬럼을 같이 복합키로 쓸 것


-- PostgreSQL 서버 프로세스
--   -> server_process.png 참고: PostgreSQL 백그라운드에 몇개의 worker들이 자동으로 돌아감 pg_cron도 그 중 하나

SELECT cron.schedule('healthcheck', '5 seconds', 
    $$INSERT INTO system_log(message) VALUES ('OK')$$
);
-- cron.schedule: cron 스케쥴러 등록
-- 'healthcheck': 이름, '5 seconds': 빈도, args[2]: 명령어
-- 기존 분단위는 * 1 * * * 이런식으로 썼지만, v1.5부터 '{} seconds'로 초단위도 지원 

/*
Datadog과 역할이 겹치지 않습니다.

Datadog    바깥에서 관찰. 지표 수집, 알림, 대시보드
pg_cron    안에서 실행. 정리·집계·유지보수 작업

Datadog은 "느려졌다"고 알려주지만, 오래된 파티션을 지워주지는 않습니다.

주로 이런 일에 씁니다.

-- 다음 달 파티션 미리 만들기
SELECT cron.schedule('new-partition', '0 0 25 * *', $$SELECT create_next_partition()$$);

-- 90일 지난 파티션 버리기
SELECT cron.schedule('drop-old', '0 4 * * *', $$S90)$$);

-- 집계 테이블 갱신
SELECT cron.schedule('rollup', '* /10 * * * *', $$REFRESH MATERIALIZED VIEW daily_summary$$);

전부 SQL로 해야 하는 일들입니다. 외부에서 하려면 접속해서 쿼리를 던지고 결과를 받아야 하는데, DB 안에서 하면 그 왕복이 없어요.

로그를 남기는 용도로도 쓰이긴 합니다.
다만 이건 부수적 용도고, Datadog이 있다면 그쪽이 더 잘합니다. pg_cron의 본령은 유지보수 작업 자동화입니다.
*/


-- 마지막종합실습.png
-- 분산 워커 사용하고 코디네이터, pg_cron, 통계 대시보드 자동 갱신/모니터링 종합으로 하는 실습

CREATE TABLE transaction_stats (
    tier         VARCHAR(20) PRIMARY KEY,
    total_amount NUMERIC(20,2) NOT NULL,
    txn_count    BIGINT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);

SELECT create_reference_table('transaction_stats');

SELECT cron.schedule('refresh_stats', '5 seconds', $$
    INSERT INTO transaction_stats (tier, total_amount, txn_count, updated_at)
    SELECT  
-- 최근 1분안에 들어온 거래만 골라서 등급별로 합계 & 카운트 
-- -> 이 select를 두 워커에 흩뿌리고 각자 shad가 부분합을 내게 하고, 코디네이터가 모아서 group by 한다 이건 citus까지 진행    
        customer_tier,
        SUM(amount),
        COUNT(*),
        NOW()
    FROM transactions
    WHERE created_at > NOW() - INTERVAL '1 minute'
    GROUP BY customer_tier
    ON CONFLICT (tier) DO UPDATE SET
        total_amount = EXCLUDED.total_amount,
        txn_count    = EXCLUDED.txn_count,
        updated_at   = EXCLUDED.updated_at
$$);