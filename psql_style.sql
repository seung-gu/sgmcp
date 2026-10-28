-- postgresql에서만 쓰이는 방식들

CREATE TABLE transactions (
    transaction_id BIGSERIAL PRIMARY KEY,
    account_id     BIGINT NOT NULL,
    type           VARCHAR(10) NOT NULL CHECK (type IN ('입금', '출금')),
    amount         NUMERIC(15,2) NOT NULL CHECK (amount > 0),
    description    TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    metadata       JSONB
);

INSERT INTO transactions (account_id, type, amount, description, created_at, metadata) VALUES
    (1, '입금', 500000,  '월급',          '2024-10-05 09:00:00+09', NULL),
    (1, '출금', 150000,  '관리비',         '2024-10-10 11:30:00+09', '{"payment_method": "transfer", "bank": "국민은행", "location": "온라인"}'),
    (1, '출금', 80000,   '식료품',         '2024-10-15 14:20:00+09', '{"payment_method": "card", "card_type": "신용", "merchant": "이마트", "location": "서울", "device": "mobile"}'),
    (1, '입금', 200000,  '부수입',         '2024-10-20 16:00:00+09', NULL),
    (2, '입금', 300000,  '적금 납입',      '2024-10-01 10:00:00+09', NULL),
    (2, '입금', 300000,  '적금 납입',      '2024-11-01 10:00:00+09', NULL),
    (3, '입금', 1000000, '전세보증금 반환', '2024-10-12 15:00:00+09', NULL),
    (3, '출금', 500000,  '이체',          '2024-10-18 13:00:00+09', '{"payment_method": "transfer", "bank": "하나은행", "location": "온라인"}'),
    (3, '출금', 200000,  '카드대금',       '2024-11-05 10:30:00+09', '{"payment_method": "card", "card_type": "신용", "merchant": "롯데마트", "location": "부산", "device": "mobile"}'),
    (4, '입금', 50000,   '용돈',          '2024-10-01 09:00:00+09', NULL),
    (4, '출금', 30000,   '편의점',         '2024-10-08 20:10:00+09', '{"payment_method": "card", "card_type": "체크", "merchant": "GS25", "location": "서울", "device": "mobile"}'),
    (4, '입금', 50000,   '용돈',          '2024-11-01 09:00:00+09', NULL),
    (5, '입금', 2000000, '보너스',         '2024-10-25 17:00:00+09', NULL),
    (5, '출금', 800000,  '여행경비',       '2024-10-28 08:00:00+09', NULL),
    (5, '출금', 450000,  '가전제품',       '2024-11-10 14:00:00+09', '{"payment_method": "card", "card_type": "체크", "merchant": "쿠팡", "location": "온라인", "device": "pc"}'),
    (6, '입금', 300000,  '적금 납입',      '2024-11-01 10:00:00+09', NULL),
    (6, '출금', 100000,  '외식',          '2024-11-15 19:30:00+09', '{"payment_method": "card", "card_type": "신용", "merchant": "올리브영", "location": "서울", "device": "mobile"}'),
    (7, '입금', 300000,  '적금 납입',      '2024-10-01 10:00:00+09', NULL),
    (7, '입금', 300000,  '적금 납입',      '2024-11-01 10:00:00+09', NULL),
    (8, '입금', 10000,   '이자',          '2024-10-31 00:00:00+09', NULL),
    (8, '입금', 10000,   '이자',          '2024-11-30 00:00:00+09', NULL),
    (1, '출금', 120000,  '통신비',         '2024-11-10 09:00:00+09', NULL),
    (3, '입금', 750000,  '월급',          '2024-11-05 09:00:00+09', NULL);

-- JSON: 입력된 테스트를 그대로 저장
-- -> 읽을 때마다 파싱

-- JSONB: 파싱된 이진 형태로 저장
-- -> 저장 시 처리 비용 있음. but, 읽을 때는 빠름

/*
-- 예시
SELECT '{"b":1, "a":2, "a":3}'::json;
-- {"b":1, "a":2, "a":3}     ← 그대로

SELECT '{"b":1, "a":2, "a":3}'::jsonb;
-- {"a": 3, "b": 1}          ← 정리됨

세 가지가 바뀌었죠:
- 공백이 정리됨
- 키 순서가 바뀜 (b, a → a, b)
- 중복 키 a가 하나만 남음 (마지막 값 3)

제일 큰 차이는 인덱스입니다

JSON에는 인덱스를 못 겁니다. 데이터가 쌓이면 조회할 때마다 전체를 훑어야 해요. 이게 실무에서 JSONB를 쓰는 진짜 이유입니다.

연산자도 JSONB만 있는 게 많습니다

data @> '{"status": "done"}'   -- 이걸 포함하나? (jsonb 전용)
data ? 'user_id'               -- 이 키가 있나?  (jsonb 전용)

JSON Binary (JSONB) -> 실제 2진 데이터가 아니라, index를 이용해서 2진 데이터처럼 빠르게 조회할 수 있는 구조
*/


-- 1. -> or ->> 키: metadata (json 포맷) 관리 키 (dictionary get 같은 역할)
-- 차이: ->는 JSON 객체를 반환하고, ->>는 텍스트를 반환함
-- data -> 'user' -> 'address' ->> 'city'
--       └ jsonb  └ jsonb      └ text (끝)
-- -> 는 결과가 여전히 jsonb라 계속 이어붙일 수 있습니다. ->> 는 text가 돼서 거기서 끝이에요.

SELECT
    transactions.transaction_id,
    transactions.metadata -> 'payment_method' AS 결제수단_JSONB,
    transactions.metadata ->> 'payment_method' AS 결제수단_텍스트
FROM transactions WHERE metadata IS NOT NULL
LIMIT 5;

-- 2. #>> 
/*
배열이 섞여 있을 때 진가가 나옵니다
번호를 그냥 경로에 넣으면 돼요:

data #>> '{items,0,name}'                  -- items 배열의 첫 번째의 name
data -> 'items' -> 0 ->> 'name'            -- 같은 뜻인데 길죠

깊이 들어갈수록 #>> 가 훨씬 짧습니다.
주의할 점 하나 — 경로 중간에 키가 없으면 에러가 아니라 NULL이 나옵니다.

'{"a": 1}'::jsonb #>> '{b,c,d}'   -- NULL
*/
SELECT
    transaction_id,
    metadata #>> '{location}' AS 위치
FROM transactions
WHERE metadata IS NOT NULL;

-- JSONB는 BTREE 와 다른 인덱싱 방식 -> 키가 없을 수도 있기 때문
-- JSONB의 인덱싱 방식: 역 인덱스 (GIN- Generalized Inverted Index)
--> 검색엔진 주로 사용하는 구조
    --> 행 -> 값
    --> 역 인덱스 : 값 -> 행

-- 3. @> 오른쪽이 왼쪽에 포함되는지 (A @> B ↔ A ⊇ B,  A <@ B ↔ A ⊆ B)
SELECT transaction_id, amount, metadata
FROM transactions
WHERE metadata @> '{"payment_method": "card"}';


-- 인덱스 만드는 쿼리
-- CREATE INDEX ON logs USING GIN (data);   -- jsonb만 됨
-- CREATE INDEX ON logs USING GIN (data jsonb_path_ops);  -- jsonb_path_ops라는 옵션이 필요없는 key까지 전부 인덱싱 하는걸 방지 (효율적)
-- 인덱스 만드는 비용은 크지만 (Rt log Rt). (R은 행의 개수, t는 각 행의 key, item 갯수)
--    정렬의 비용이 nlogn이고 여기서 n은 Rt이기 때문. jsonb_path_ops 옵션을 쓰면, key가 반복되어서 저장되지 않기 때문에,
--    item만 가지고 정렬하게 됨. 따라서 Rt'logRt' (t'= t-n(key))
-- 조회하는 비용은 logk + m (k는 정렬된 리스트의 total 수, m은 해당 단어의 중복되어 쓰인 갯수)
/*
기호

┌──────┬─────────────────────────────────────────────┐
│ 기호  │                     뜻                       │
├──────┼─────────────────────────────────────────────┤
│ R    │ 테이블 행 수                                   │
├──────┼─────────────────────────────────────────────┤
│ t    │ 행 하나에서 뽑는 조각 수 (jsonb 키·값 개수)         │
├──────┼─────────────────────────────────────────────┤
│ N    │ 인덱스의 고유 조각 수 (중복 뺀 종류)                │
├──────┼─────────────────────────────────────────────┤
│ k    │ 검색 조건 항목 수                               │
├──────┼─────────────────────────────────────────────┤
│ m    │ 결과 행 수                                    │
└──────┴─────────────────────────────────────────────┘

---
비교 — 조건이 k개일 때

┌─────────────┬───────────┬────────────────┬────────────┬─────────────────┐
│             │ 인덱스 수   │      조회       │    삽입      │      생성       │
├─────────────┼───────────┼────────────────┼────────────┼─────────────────┤
│ 인덱스 없음    │ 0         │ O(R)           │ O(1)       │ —               │
├─────────────┼───────────┼────────────────┼────────────┼─────────────────┤
│ GIN         │ 1         │ O(k·log N + m) │ O(t·log N) │ O(R·t·log(R·t)) │
├─────────────┼───────────┼────────────────┼────────────┼─────────────────┤
│ B-tree 개별  │ k         │ O(k·log R + m) │ O(k·log R) │ O(k·R·log R)    │
├─────────────┼───────────┼────────────────┼────────────┼─────────────────┤
│ B-tree 복합  │ 1         │ O(log R + m)   │ O(log R)   │ O(R·log R)      │
└─────────────┴───────────┴────────────────┴────────────┴─────────────────┘

복합이 모든 수치에서 제일 좋습니다. 단, 왼쪽부터 순서대로 쓸 때만 탑니다.

---
기능 차이 — 여기가 실제로 갈리는 곳

┌─────────────────────────┬────────┬─────────────────┐
│                         │  GIN   │   B-tree 복합    │
├─────────────────────────┼────────┼─────────────────┤
│ 조건 조합이 제각각           │ ✓      │ ✗ (왼쪽 접두사)   │
├─────────────────────────┼────────┼─────────────────┤
│ 범위 검색 (>, BETWEEN)    │ ✗      │ ✓               │
├─────────────────────────┼────────┼─────────────────┤
│ ORDER BY 공짜            │ ✗      │ ✓               │
├─────────────────────────┼────────┼─────────────────┤
│ 미리 조합을 정해야 하나       │ 아니오  │ 예               │
└─────────────────────────┴────────┴─────────────────┘

---
고르는 법

jsonb 안을 검색, 어떤 키 쓸지 모름     →  GIN (jsonb_path_ops)
일반 컬럼, WHERE + ORDER BY 조합 고정  →  B-tree 복합
특정 jsonb 키 하나만 계속, 범위·정렬   →  B-tree 표현식

읽는 법

- log N → 트리 안에서 찾는 비용 (트리 칸 수가 N)
- log R·t → 정렬 비용 (정렬할 항목이 R·t개)
- + m → 인덱스가 준 번호로 테이블을 실제 읽는 비용
- t vs k → GIN은 모든 키 색인(t), B-tree는 고른 키만(k)
*/


-- jsonb_each : 통계 
-- key 값 통계
SELECT key, COUNT(*) AS 출현횟수
FROM transactions,
    jsonb_each(metadata) AS kv(key, value)
WHERE metadata IS NOT NULL
GROUP BY key
ORDER BY 출현횟수 DESC;
-- value 값 통계
SELECT value, COUNT(*) AS 출현횟수
FROM transactions,
    jsonb_each(metadata) AS kv(key, value)
WHERE metadata IS NOT NULL
GROUP BY value
ORDER BY 출현횟수 DESC;

-- json_array_elements: value값이 배열이라면 펼치고 싶을 때
SELECT
    transaction_id,
    jsonb_array_elements_text(metadata -> 'tags') AS 태그
FROM transactions
WHERE metadata ? 'tags';

-- jsonb elem 삽입
UPDATE transactions
SET metadata = metadata || '{"verified": true, "risk_score": 0}'
WHERE transaction_id = 3;

-- jsonb elem 삭제
UPDATE transactions
SET metadata = metadata - 'risk_score'
WHERE transaction_id = 3;

-- primary key: 기본 B-tree index
-- account_id를 사용할 떄 index 사용하게 함
CREATE INDEX idx_transaction_account_id ON transactions (account_id);
-- 복합 인덱스를 사용할 떈 순서가 굉장히 중요함
-- 그리고 아래의 복합 인덱스를 쓰면 위의 단일 인덱스는 복합 인덱스 내에 포함된거라 또 쓸 필요가 없음
-- 하지만 단일 created_at의 단일 인덱스로는 사용 할 수 없음. 
-- 왜냐하면 account_id로 먼저 정렬한 내에서 created_at을 정렬하기 때문
CREATE INDEX idx_transactions_account_created ON transactions (account_id, created_at);

-- 인덱스 삭제
DROP INDEX idx_transcation_account_id;

-- 인덱스 종류
-- Hash 인덱스 -> 버킷 방식 (버킷에 직접 접근 O(1) )
-- 실제론 B-tree 방식에 WHERE + 등치조건 (>, <, BETWEEN) 을 붙여도 충분히 빠르기에, Hash 잘 안쓰임 (Hash는 순서 비교 불가능) 
CREATE INDEX idx_transactions_type ON transactions USING HASH (type);
-- type값을 어떻게 쓰냐에 따라 다름 --> type 의 종류가 많다면 유용할 수 있지만, 중복된 type의 값이 많다면 성능 다운
-- type='deposit' 이런식으로 조회하는데, 이 value값이 Unique한 값을 요구하지 않기 때문
-- 아무튼 여러모로 B-TREE에 밀림

-- GiST (Generalized Search Tree) 인덱스: 범위 조건이 필요하거나 특수한 데이터 타입
/*
"일반화"의 뜻 — 인덱스의 틀만 제공하고, 안에 들어갈 비교 규칙은 갈아 끼울 수 있게 만들었다는 겁니다. 새 데이터 타입을 만들면 거기 맞는 규칙을 짜 넣어 인덱스를 쓸 수 있어요.

어디 쓰이냐면

지도·좌표 (PostGIS)   →  "이 사각형 안에 있는 지점들"
범위 타입             →  "이 기간과 겹치는 예약"
유사 문자열 (pg_trgm) →  "이것과 비슷한 단어"
최근접 검색           →  "여기서 가장 가까운 5곳"
*/

-- BRIN: 물리적으로 연속해서 저장된 데이터에서 유리
-- 각 블록의 최소,최대값 저장 (날짜 같은 연속적인 데이터)
-- 쌓기만 하는 로그        →  BRIN 적합. 관리도 거의 불필요
-- 과거 데이터를 자주 수정  →  BRIN 부적합. B-tree 쓰세요
CREATE INDEX idx_transcation_account_id ON transactions USING BRIN (account_id);

/* 
정리

PostgreSQL 인덱스 6종

┌─────────┬─────────────────────────────────────────┐
│  종류    │                언제 쓰나                   │
├─────────┼─────────────────────────────────────────┤
│ B-tree  │ 기본. 등호·범위·정렬 전부                     │
├─────────┼─────────────────────────────────────────┤
│ Hash    │ 등호만. B-tree로 충분해서 거의 안 씀           │
├─────────┼─────────────────────────────────────────┤
│ GIN     │ 한 칸에 여러 개 (jsonb, 배열, 전문검색).       │
├─────────┼─────────────────────────────────────────┤
│ GiST    │ 겹침·가까움 (좌표, 범위 타입)                 │
├─────────┼─────────────────────────────────────────┤
│ SP-GiST │ 겹치지 않게 쪼개는 것 (접두사, IP 주소)         │
├─────────┼─────────────────────────────────────────┤
│ BRIN    │ 쌓이는 순서 = 값의 순서 (로그, 시계열)          │
└─────────┴─────────────────────────────────────────┘

SP-GiST도 빠져 있었네요. Space-Partitioned GiST의 줄임말입니다. GiST는 영역이 겹쳐도 되고, SP-GiST는 겹치지 않게 자릅니다. 트라이 구조라서 접두사 검색에 강해요.

실무에서 만나는 빈도로 따지면:

B-tree   ████████████████████  거의 전부
GIN      ███                   jsonb 쓸 때
BRIN     █                     대용량 로그
GiST     █                     PostGIS 쓸 때
SP-GiST  ·                     드묾
Hash     ·                     거의 없음

B-tree와 GIN 둘만 알아도 실무 대부분이 커버됩니다.
*/


-- Explain으로 index 성능 비교
-- Mock data
CREATE TABLE transactions_perf (
    transaction_id  BIGSERIAL PRIMARY KEY,
    account_id      INTEGER,
    type            VARCHAR(10),
    amount          NUMERIC(15, 2),
    description     TEXT,
    created_at      TIMESTAMPTZ
);
INSERT INTO transactions_perf (account_id, type, amount, description, created_at)
SELECT
    (random() * 999 + 1)::INTEGER                   AS account_id,
    CASE WHEN random() < 0.6 THEN 'deposit'
        ELSE 'withdrawal'
        END                                             AS type,
    ROUND((random() * 9900000 + 100000)::NUMERIC, 2) AS amount,
    md5(clock_timestamp()::TEXT)                    AS description,
    NOW() - (random() * INTERVAL '3 years')         AS created_at
FROM generate_series(1, 1000000);

-- EXPLAIN, EXPLAIN ANALYZE
--> EXPLAIN : 쿼리를 실제로 실행하지 않고 실행 계획만을 수행 문법
EXPLAIN SELECT * FROM transactions_perf WHERE account_id = 42;
-- parallel_setup_cost : 1000

-- Gather  (cost=1000.00..19008.83 rows=995 width=70)  => cost가 1000~19008.83 (기본이 1000)
--        Workers Planned: 2  => leader, worker1, worker2 총 3개의 worker가 parallel로 돌아감
--   ->  Parallel Seq Scan on transactions_perf  (cost=0.00..17909.33 rows=415 width=70), rows: 한 워커 당 예상 행
--        Filter: (account_id = 42)

--   995 / 415 = 2.4 인 이유:
-- Leader 프로세스는 결과 모으는일을 함.
-- leader 기여도 = 1 - 0.3 × 워커수 = 1 - 0.6 = 0.4
-- 총 참여도    = 2 + 0.4 = 2.4
-- PostgreSQL 소스에 박혀 있는 값입니다. costsize.c 의 get_parallel_divisor() 함수예요.
-- leader_contribution = 1.0 - (0.3 * parallel_workers);
-- if (leader_contribution > 0)
--     parallel_divisor += leader_contribution

SET max_parallel_workers_per_gather = 0;  -- No 병렬처리 (현재 세션에서만 유효). 다시 DB 연결하면 설정 초기화 됨

-- 결과
-- 1. Seq Scan: Table 처음부터 끝까지 읽음 (인덱스 없이 돌아가거나, 반환하는 행이 많아서 인덱스보다 전수 조사가 나은 경우)
-- 2. Index Scan: 인덱스에서 조건에 맞는 행을 찾고 데이터를 이동해가면서 읽음 (반환하는 행이 적을 때 빠름)
-- 3. Bitmap Heap Scan: 인덱스에서 맞는 조건의 행들의 위치를 비트맵으로 수집하고 한꺼번에 데이터 읽음
EXPLAIN ANALYZE SELECT * FROM transactions_perf WHERE account_id = 42;  -- EXPLAIN: 예측, ANALYZE: 실측

-- index 추가 전 : Execution Time: 177.071 ms
-- index 추가 후 : Execution Time: 2.030 ms
CREATE INDEX idx_perf_account_id ON transactions_perf (account_id);


-- =======================================
-- 최적화 패턴 & 전략
-- =======================================

-- index 추가
CREATE INDEX idx_perf_created_at ON transactions_perf (created_at);

-- created_at이 index에 추가되었어도 EXTRACT(YEAR FROM created_at) 은 다른 값이므로 index scan 이 안나오고 seq scan 나옴
EXPLAIN ANALYZE SELECT * FROM transactions_perf WHERE EXTRACT(YEAR FROM created_at) = 2024;
-- 해결 방법: created_at을 그대로 쓰고, 함수 대신 필터를 써줌
EXPLAIN ANALYZE SELECT * FROM transactions_perf 
    WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01';

-- 컬럼 값을 가공하면 index 타지 않음
EXPLAIN SELECT * FROM transactions_perf WHERE account_id::text = '42';

-- Collation (정렬 규칙)
-- 문자를 어떤 순서로 놓고 어떻게 비교할지 정하는 규칙
-- locale: 더 넓은 개념 (정렬 + 날짜 + 통화 + 문자분류)
-- DB 생성 시 결정되고 나중에 못 바꿈 (DB 새로 만들어야 함)
-- [1] C / POSIX: 글자 코드값 그대로 비교 (ASCII)
-- [2] 언어 로케일 (en_US, ko_KR, de_DE): 사람이 보기 좋은 순서
-- C 로케일: ABC, ABCx, abb, abc, abcx, abd 순서. 따라서 ab%를 해도 aba ~ abz 까지 다 검색 됨 (인덱스로)
-- en_US 로케일: abb, abc, ABC, abcx, ABCx, abd (대소문자 반복 순서). 따라서 ab%를 해도 aba~abz 사이에 ABC, ABCx등도 끼어있음
--             DB는 ABC, ABCx가 끼어있는걸 구분할 방법이 없으므로, 그냥 인덱스 서칭을 포기하고 테이블 모든 행을 sequence로 전수조사함
CREATE INDEX idx_perf_description ON transactions_perf (description);
EXPLAIN ANALYZE SELECT * FROM transactions_perf WHERE description LIKE 'a1b2%';
-- locale 확인
SELECT datcollate FROM pg_database WHERE datname=current_database();

-- %a1b2 같은 경우는 범위 자체(시작과 끝)를 알 수가 없어서 전수 조사 Seq Scan 이 됨
EXPLAIN ANALYZE SELECT * FROM transactions_perf WHERE description LIKE '%a1b2';


-- offset에 따른 비용

-- 첫 페이지 : 0.42..1.96  --> 0에서 20개를 읽고 반환
EXPLAIN SELECT * FROM transactions_perf ORDER BY created_at LIMIT 20 OFFSET 0; 
-- 50만 번째 페이지 : 38391.83..38393.36  --> 500000만까지 읽고 여기부터 20개 반환. 500000만개 읽는 비용이 너무 큼 (그냥 버림)
EXPLAIN SELECT * FROM transactions_perf ORDER BY created_at LIMIT 20 OFFSET 500000; 

-- 커서 페이징
-- WHERE created_at > ['직전 페이지 마지막 값'] ORDER BY created_at LIMIT 20; 이렇게 되면 빠름
EXPLAIN SELECT * FROM transactions_perf
WHERE (created_at, transaction_id) > ('2024-11-05 09:00:00+09', 12345)
ORDER BY created_at, transaction_id LIMIT 20;
-- 인덱스는 값 -> 위치는 알아도, 순번 -> 위치는 일일이 세어야지 알 수 있음.
-- B-tree는 값이 정렬돼 담겨 있을 뿐, 각 항목에 번호표가 붙어 있지 않습니다. 그러니 50만 번째를 찾으려면 처음부터 하나씩 세면서 가는 수밖에 없어요.
-- 사전으로 보면
--      "apple 찾아줘"       →  a 부분 펼쳐서 바로 감      (값으로 찾기)
--      "5000번째 단어 줘"    →  1번부터 세어야 함          (순번으로 찾기)

-- statatistics
-- 실행 결과:
-- attname      n_distinct  most_common_vals                        correlation
-- account_id	1000	    {393,949,27,262,178,660,793,253,794}    -0.0048781694
-- n_distinct: account_id가 몇 종류 인가
-- most_common_vals: 자주 나오는 account_id들
-- correlation: 1에 가까움 => 값 순서대로 저장, 0에 가까움 => 무작위로 흩어져 있음. -1에 가까움 => 역순으로 저장
SELECT attname, n_distinct, most_common_vals, correlation
FROM pg_stats
WHERE tablename = 'transactions_perf' AND attname = 'account_id';

-- analyze를 하면 rows나 다른 acutal과 예상치의 차이를 업데이트 해줌 (postgresql은 autovacuum으로 주기적으로 업뎃)
ANALYZE transactions_perf;


-- 파티션

-- Range 기반의 파티셔닝
CREATE TABLE transactions_part (
    transaction_id  BIGSERIAL,
    account_id      INTEGER,
    type            VARCHAR(10),
    amount          NUMERIC(15, 2),
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL -- not null 이어야함 (null이면 파티션 기준이 없어짐)
) PARTITION BY RANGE (created_at);

CREATE TABLE transactions_part_2023
    PARTITION OF transactions_part
        FOR VALUES FROM ('2023-01-01') TO ('2024-01-01');

CREATE TABLE transactions_part_2024
    PARTITION OF transactions_part
        FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

CREATE TABLE transactions_part_2025
    PARTITION OF transactions_part
        FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');

-- 파티션 insert
INSERT INTO transactions_part (account_id, type, amount, description, created_at)
SELECT account_id, type, amount, description, created_at
FROM transactions_perf
WHERE created_at >= '2023-01-01'
    AND created_at <  '2026-01-01';

-- [23514] ERROR: no partition of relation
-- "transactions_part" found for row Detail:
-- Partition key of the failing row contains
-- (created_at) = (2026-03-19 05:20:08.501366+00).

-- 특정 파티션 transactions_part_2024 에서만 읽음
EXPLAIN ANALYZE 
SELECT * FROM transactions_part
    WHERE created_at >= '2024-01-01'
        AND created_at < '2024-04-01'


-- List 파티셔닝 : 특정 값을 조건으로 분할
-- 주의할 점: 데이터가 균등하게 파티션끼리 잘 분배되어야함

CREATE TABLE transactions_by_type (
    transaction_id  BIGSERIAL,
    account_id      INTEGER,
    type            VARCHAR(10) NOT NULL,
    amount          NUMERIC(15, 2),
    description     TEXT,
    created_at      TIMESTAMPTZ
) PARTITION BY LIST (type);  -- type값 기준으로 분할

CREATE TABLE transactions_deposit
    PARTITION OF transactions_by_type
        FOR VALUES IN ('deposit');

CREATE TABLE transactions_withdrawal
    PARTITION OF transactions_by_type
        FOR VALUES IN ('withdrawal');

