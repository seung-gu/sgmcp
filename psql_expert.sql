-- Tranjaction (트랜젝션)

DROP TABLE accounts;
CREATE TABLE accounts (
    account_id   BIGSERIAL     PRIMARY KEY,
    customer_id  BIGINT        NOT NULL,
    account_type VARCHAR(20)   NOT NULL CHECK (account_type IN ('예금', '적금', '입출금')),
    balance      NUMERIC(15,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at   TIMESTAMPTZ   DEFAULT NOW()
);

INSERT INTO accounts (customer_id, account_type, balance) VALUES
    (1, '예금',    1500000.00),
    (1, '적금',    3000000.00),
    (2, '예금',     250000.00),
    (3, '입출금',    80000.00),
    (4, '예금',    5200000.00),
    (4, '적금',    1800000.00),
    (5, '적금',    1200000.00),
    (5, '입출금',       0.00);


-- 트랜젝션 보장 코드
BEGIN;

UPDATE accounts SET balance = balance - 100 WHERE account_id = 1;
UPDATE accounts SET balance = balance - 100 WHERE account_id = 2;

COMMIT;

-- ACID 
-- A: Atomicity (원자성 - 전부 성공/실패 (부분적 X))
-- C: Consistency (일관성 - 제약 계속 유지)
-- I: Isolation (격리성 - 다른 동시에 실행되는 쿼리에 영향 받으면 안됨)
-- D: Durability (지속성 - 시스템 장애에도 사라지거나 하면 안됨)
-- -> WAL (Write Ahead Log)

BEGIN;
UPDATE accounts SET balance = balance - 100000 WHERE account_id = 1;
-- 이 시점에서 잔액을 확인해보니 음수가 됨을 발견
SELECT balance FROM accounts WHERE account_id = 1;
ROLLBACK;  --> BEGIN 실행될때의 상황으로 ROLLBACK 됨


-- SavePoint (잘 안 쓰임)

BEGIN;

INSERT INTO transactions (account_id, type, amount, description)
VALUES (1, 'withdrawal', 50000, '첫 번째 출금');

SAVEPOINT sp1;

INSERT INTO transactions (account_id, type, amount, description)
VALUES (1, 'withdrawal', 9999999, '실수로 큰 금액 출금');

-- 두 번째 INSERT만 취소
ROLLBACK TO SAVEPOINT sp1;

-- 첫 번째 INSERT는 살아있음
COMMIT;


-- 격리 수준 (다음 세가지를 얼마나 잘 다뤘는지)
-- Dirty Read: 다른 트랜잭션이 아직 커밋하지 않은 데이터를 읽는 현상
-- Non Repeatable Read: 같은 트랜잭션 안에서 같은 행을 두 번 읽었는데, 값이 달라지는 현상
-- Phantom Read: 같은 트랜잭션 안에서 같은 조건으로 두 번 조회했는데, 행 수가 달라지는 현상

-- 1. Read Committed: Default -> 커밋된 데이터만 읽는다
-- Session 2
BEGIN;
SELECT balance FROM accounts WHERE account_id = 1;

-- Session 1
BEGIN;
UPDATE accounts SET balance = balance - 100000 WHERE account_id = 1;
COMMIT;

-- Session 2 -> Session 2는 BEGIN만 해놓고 끝나도, 다른 Session에서 Commit을 하면 Session 2에서도 업뎃됨
SELECT balance FROM accounts WHERE account_id = 1;
COMMIT;


-- 2. Repetable Read : 트랜잭션이 시작되는 시점의 스냅샷을 유지

-- Session 1
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
SELECT balance FROM accounts WHERE account_id = 1;

-- Session 2
BEGIN;
UPDATE accounts SET balance = balance - 100000 WHERE account_id = 1;
COMMIT;

-- Session 1
SELECT balance FROM accounts WHERE account_id = 1;  -- 바뀌지 않음


-- 3. Serializable : 가장 높은 격리 수준 -> 완전 순차성을 보장
-- 성능이 떨어지기 때문에 실제론 거의 안쓰나 금융에선 정확성이 중요하기에 많이 쓰임

BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
-- ... 작업
COMMIT;


-- 열려 있는 트랜잭션 찾기
SELECT pid, state, now() - xact_start AS 경과, query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
    AND pid <> pg_backend_pid();        -- ← 자기 자신 제외

-- 열려있는 트랜잭션 강제 종료
-- SELECT pg_terminate_backend([pid]);


-- LOCK
-- 1. Row Level Lock: Update, DELETE 실행될때 해당 쿼리에만 LOCK
-- 2. Table Level Lock: 테이블 구조 변경 (ALTER) 혹은 명시적 테이블 락 실행시, Table 읽고 쓰는 다른 모든 쿼리 대기

-- FOR UPDATE
-- 1번 계좌 행만 잠김. 2번, 3번은 자유롭게 쓸 수 있음

-- SESSION 1
BEGIN;
SELECT balance FROM accounts WHERE account_id = 1 FOR UPDATE;

-- Session 2 (다른 창이어야 함 -> UPDATE이 멈춤)
BEGIN;
UPDATE accounts SET balance = balance - 50000 WHERE account_id = 1;
COMMIT;

-- Session 1
COMMIT;


-- 교착 상태 (Deadlock): 서로 상대가 쥔 잠금을 기다려 안 풀리는 상태
--   PostgreSQL이 감지 후 한쪽 강제 롤백 + 에러 반환
--   예방: 여러 행 잠글 때 항상 같은 순서로 (예: id 오름차순)

-- LOCK을 쓰면 정합성은 보장되지만 동시성이 죽어서 성능 저하
-- PostgreSQL은 이를 해결하려고 MVCC 도입
-- MVCC: 하나의 행에 여러 버전을 동시에 보존 (덮어쓰지 않고 새 버전 추가)

--  단, MVCC가 없애준 건 "읽기 <-> 쓰기" 충돌뿐.
--  쓰기 <-> 쓰기는 여전히 잠금으로 줄을 선다 (UPDATE가 자동으로 행을 잠금)
--  잠금이 풀리면 그 행의 최신 버전을 다시 읽어서 UPDATE (그래서 값이 안 틀어짐)

-- 기본적으로 MVCC는 자동. 켜고 끄는 게 아님
-- 다만 아래 두 경우는 직접 써야 함
--   [1] 여러 쿼리가 다 성공해야 할 때  -> BEGIN / COMMIT (출금+입금)
--   [2] 읽고 -> 계산 -> 쓰기 할 때     -> SELECT ... FOR UPDATE
--       (UPDATE 한 줄로 끝나는 쿼리면 자동으로 잠기니 불필요, 여러 쿼리에 걸쳐 UPDATE가 계산될 때 필요)

-- Dead Tuple: 아무도 참조하지 않는 구버전 행. MVCC의 대가
--   VACUUM이 회수 (보통 autovacuum이 자동)
--   idle in transaction 세션이 오래 남으면 VACUUM이 못 치워서


-- PostgreSQL 이 지원하는 Plug-in

-- pgvector
CREATE EXTENSION IF NOT EXISTS vector;
-- DROP EXTENSION vector;

CREATE TABLE doc_embeddings (
    id          SERIAL PRIMARY KEY,
    content     TEXT,
    embedding   VECTOR(8) -- 8차원 벡터를 저장
);

INSERT INTO doc_embeddings (content, embedding) VALUES
    ('카드 결제',           '[0.82, 0.14, 0.63, 0.45, 0.71, 0.28, 0.56, 0.39]'),
    ('신용카드 납부',        '[0.80, 0.16, 0.61, 0.47, 0.69, 0.30, 0.54, 0.41]'),
    ('계좌 이체',           '[0.55, 0.72, 0.38, 0.61, 0.29, 0.84, 0.17, 0.66]'),
    ('송금',               '[0.53, 0.74, 0.36, 0.63, 0.27, 0.82, 0.19, 0.68]'),
    ('급여 입금',           '[0.31, 0.89, 0.12, 0.77, 0.08, 0.95, 0.24, 0.51]'),
    ('월급',               '[0.29, 0.91, 0.10, 0.79, 0.06, 0.93, 0.22, 0.53]'),
    ('ATM 출금',           '[0.67, 0.33, 0.78, 0.22, 0.89, 0.11, 0.44, 0.76]'),
    ('현금 인출',           '[0.65, 0.35, 0.76, 0.24, 0.87, 0.13, 0.42, 0.74]'),
    ('공과금 납부',          '[0.43, 0.58, 0.29, 0.84, 0.16, 0.72, 0.91, 0.35]'),
    ('전기요금',            '[0.41, 0.60, 0.27, 0.86, 0.14, 0.70, 0.89, 0.37]');

-- ============================================
-- 거리/유사도 연산자 (pgvector)
-- ============================================
-- 위의 예시는 벡터 길이가 정규화되지 않아서(길이 != 1) 4개 결과가 다 다르게 나오지만,
-- 정규화된 벡터에서는 내적, L2, 코사인의 "순위"가 같음 (값은 달라도 등수는 동일)
-- 따라서 실무에선 거의 코사인만 쓰임. OpenAI 임베딩은 이미 정규화되어 나옴

--  연산자  이름        계산                        특성
--  <=>    코사인거리   1 - cos(θ)                 각도만 봄. 길이 무시
--  <->    L2          √(Σ(a-b)²)                 직선 거리. 길이 차이가 반영됨
--  <#>    음의 내적    -(Σ a·b)                   길이에 가장 민감. 계산이 제일 쌈
--  <+>    L1          Σ|a-b|                     절댓값 합. 튀는 값에 덜 흔들림

-- [코사인] 텍스트 임베딩의 표준
--   문서 길이가 달라도 주제가 같으면 가깝다고 판정 -> 의미 검색에 적합
-- [L2] 좌표/위치처럼 "실제 거리"가 의미를 가질 때. 클러스터링(K-means)도 L2 기반
--   이 프로젝트: 검색은 코사인, 클러스터링은 중심화 후 절대거리 사용 (grouping.py 주석)
-- [내적] 정규화된 벡터면 코사인과 순위 동일하나 길이 계산을 건너뛰어 더 빠름
--   정규화 안 된 벡터에선 "크기 = 중요도"로 쓰임 (추천 시스템의 인기도 반영 등)
--   pgvector는 작은 값이 가깝다고 보므로 부호를 뒤집어 둠 -> 값을 볼 땐 -(a <#> b)
-- [L1] 이상치에 강함. 차이가 여러 차원에 흩어져 있으면 L2보다 크게 나옴
--   (10,0,0,0) -> L2=10, L1=10  /  (5,5,5,5) -> L2=10, L1=20
--   임베딩 검색에선 거의 안 씀

-- ============================================
-- 주의: 인덱스와 연산자가 짝이 맞아야 인덱스를 탄다
-- ============================================
--   vector_cosine_ops  <->  <=>
--   vector_l2_ops      <->  <->
--   vector_ip_ops      <->  <#>
--   vector_l1_ops      <->  <+>
-- 짝이 안 맞으면 에러 없이 그냥 Seq Scan (조용히 느려짐)
--   CREATE INDEX ... USING hnsw (embedding_vec vector_cosine_ops);  -> <=> 로만 검색

-- 정규화 여부 확인 (길이가 1이면 정규화된 것)
--   SELECT content, embedding <-> array_fill(0::real, ARRAY[8])::vector AS 길이
--   FROM doc_embeddings;
-- 정규화 함수: l2_normalize(embedding)   -- pgvector 0.7+

-- 1. 코사인 유사도: 두 벡터가 같은 방향을 가리키는지를 측정
SELECT
    content,
    embedding <=> (SELECT embedding FROM doc_embeddings WHERE content = '카드 결제') AS 코사인거리
FROM doc_embeddings
ORDER BY 코사인거리;

-- 2. L2: 방향이 아니라, 직선 거리로 유사도 측정 (유클리드 거리)
SELECT
    content,
    embedding <-> (SELECT embedding FROM doc_embeddings WHERE content = '카드 결제') AS L2거리
FROM doc_embeddings
ORDER BY L2거리;

-- 3. 내적도: 벡터의 방향과 크기 모두 반영하는 형태
SELECT
    content,
    embedding <#> (SELECT embedding FROM doc_embeddings WHERE content = '카드 결제') AS 내적거리
FROM doc_embeddings
ORDER BY 내적거리;

-- vector의 index들

-- KNN(K-Approximate Nearest Neighbor): 가장 유사한 K개를 찾는 검색 -> Sequential Scan
-- K-mean랑 다름. K-means는 K개로 클러스터링 하는거라면, 이건 현재 값이 가장 유사한 그룹 K개를 찾는거임

EXPLAIN ANALYZE
SELECT content
FROM doc_embeddings
ORDER BY embedding <=> '[0.82, 0.14, 0.63, 0.45, 0.71, 0.28, 0.56, 0.39]'
LIMIT 5;

-- IVFFLAT (벡터를 여러 클러스터로 저장하고 검색할때만 가장 가까운 클러스터 확인)
-- 단점: 가장 가까운 클러스터만 체크해서, 모든 클러스터를 체크하지 않음
CREATE INDEX idx_ivfflat_embedding
    ON doc_embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 3);

-- lists : 클러스터의 수
-- vector_cosine_ops : 코사인 거리를 기준으로 인덱스를 만든다
-- -> L2 거리 : vector_l2_ops

-- 표본이 적을 경우 index 안타는게 빨라서 pgsql이 알아서 seqscan 할 수 있음
-- 그럴때 잠시 seqscan을 꺼놓는 설정 (테스트 위해서만)
SET enable_seqscan = OFF;  


-- ============================================
-- HNSW 인덱스 (벡터 검색용)
-- ============================================
-- Hierarchical Navigable Small World (계층적 탐색 가능 작은 세계)
--   Small World : 이웃끼리 연결 + 멀리 가는 지름길 몇 개 -> 몇 다리면 어디든 닿음
--   Navigable   : 목표 쪽으로 계속 다가갈 수 있게 연결
--   Hierarchical: 층을 나눔. 위층은 성기고(긴 링크) 아래층은 촘촘(짧은 링크)
--
--   위층    ●─────────●─────────●     고속도로: 대충 그 동네까지 점프
--   중간층  ●───●───●─●───●───●─●     국도
--   아래층  ●─●─●─●─●─●─●─●─●─●─●     골목: 정확한 집 찾기
--
--   => 100만 개 중에서도 몇십 번 비교로 답을 냄
--   [근사 검색] 진짜 1등을 놓칠 수 있음. 그 대가로 빠름

-- ============================================
-- 두 단계: 길을 놓는 일 vs 놓인 길로 다니는 일
-- ============================================
-- [1] 인덱스 만들 때 (CREATE INDEX / INSERT) : 연결선을 놓는다
--       새 점이 들어오면 "누구와 이어줄지" 정하고, 그 연결선을
--       점 추가 -> 후보 64개(ef_construction) 탐색 -> 그중 16개(m) 선택 -> 저장
--       점 하나당 한 번. 검색할 때 다시 계산하지 않음
--
-- [2] 검색할 때 (SELECT) : 놓인 연결선을 따라 답을 찾는다
--       이미 만들어진 길로 걸어다니며 가까운 행을 수집
--       후보 40개(ef_search) 유지하며 탐색 -> 가장 가까운 것들 반환
--       쿼리마다 매번. 결과는 저장 안 됨
--
--   ef_construction 이 낮으면 -> 길 자체가 엉성 -> 나중에 ef_s
--   ef_search 가 낮으면       -> 길은 좋은데 대충 찾음 -> 올리면 바로 개선

-- ============================================
-- ef 가 하는 일 (두 단계 공통 알고리즘)
-- ============================================
-- "지금까지 본 것 중 가장 가까운 N개" 명단을 들고 그래프를 걸
--   ① 아무 점에서 출발 -> ② 이웃 확인해 명단에 추가 -> ③ 명단에서 가장 가까운 점으로 이동
--   ④ 명단이 넘치면 먼 것부터 버림 -> ⑤ 더 가까운 게 안 나오면 종료
--
--   명단이 좁으면 근처에서 만족하고 멈춤 (지역 최소값에 갇힘)
--   명단이 넓으면 어중간한 후보도 들고 더 멀리 탐색 -> 더 좋은
--   ef > m 이어야 함 (명단이 연결할 개수보다 적으면 고를 게 없음)

-- ============================================
-- 파라미터 3개
-- ============================================
--   이름               무엇                    언제        올리면              바꾸려면
--   m                 점 하나의 연결 개수      빌드       정확 생성
--   ef_construction   이웃 고를 때 명단 칸수    빌드       정확도↑ 빌드↑         인덱스 재생성
--   hnsw.ef_search    검색할 때 명단 칸수      검색       정확도↑ 검색 느려짐    SET 한 줄
--   기본값: m=16, ef_construction=64, ef_search=40
--   m 을 두 배로 하면 인덱스 크기도 대략 두 배 / ef_construction 은 크기는 그대로, 빌드만 느려짐

-- ============================================
-- 튜닝 순서
-- ============================================
--   ① 기본값으로 시작
--   ② 검색 품질이 아쉬우면 ef_search 부터 (재생성 불필요 = 공
--        SET hnsw.ef_search = 100;   -- LIMIT 보다는 커야 의미 있음
--   ③ 그래도 부족하면 m 을 24~32로 올려 재생성
--   대부분 기본값으로 충분

-- IVFFlat는 만들기는 빠르고 검색 품질은 HNSW가 낫습니다. 요즘은 대개 HNSW를 씀.

DROP INDEX idx_ivfflat_embedding;
CREATE INDEX idx_hnsw
    ON doc_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);


-- ============================================
-- LIKE vs FTS (Full-Text Search)
-- ============================================
-- LIKE : 글자 나열에서 부분 문자열을 찾음
-- FTS  : 문장을 단어로 쪼개 놓고 단어 단위로 찾음

-- 실측
--   문장                    LIKE '%run%'   FTS 'run'   FTS가 쪼갠 결과
--   The server is running        t             t        'run':4 'server':2
--   Truncate the table           t             f        'tabl':3 'truncat':1   <- 차이!
--   He ran fast                  f             f        'fast':3 'ran':2
--
--   LIKE 는 T-r-u-n-c-a-t-e 안의 'run' 을 잡아버림 (단어 중간이어도 걸림)
--   FTS 는 'truncat' 이라는 한 단어로 보므로 안 걸림
--   the, is, he 는 불용어라 제거됨 / :4 는 문장 내 몇 번째 단어인지


-- ============================================
-- FTS 구성
-- ============================================
--   to_tsvector('english', 문장)   문서를 단어 목록으로 (소문자화 + 어간추출 + 불용어제거)
--   to_tsquery('english', 조건)    검색 조건
--   @@                             매칭 연산자
--
--   SELECT * FROM docs
--   WHERE to_tsvector('english', body) @@ to_tsquery('english', 'fox & run');
--
--   조건 조합:  'fox & run' (AND) / 'fox | cat' (OR) / 'fox & !dog' (NOT)
--              'quick <-> brown' (바로 붙어 있는 것만)
--   순위:      ts_rank(tsvector, tsquery)  -> 관련도 순 정렬 가능
--   plainto_tsquery / websearch_to_tsquery : 사용자 입력을 그대로 넘길 때 (파싱 에러 방지)

-- ============================================
-- 어간 추출 (stemming)
-- ============================================
--   connect connects connected connecting connection  ->  'connect' 하나로
--   databases -> 'databas'  /  queries -> 'queri'  /  running -> 'run'
--
--   [한계] 규칙 기반이라 불규칙 변화는 못 잡음
--     running -> run  (o, -ing 제거)      ran -> ran  (x, 불규칙)
--   [주의] 결과가 실제 단어가 아님 ('databas', 'queri')
--     검색 인덱스 내부용 키일 뿐 -> 사전 조회용 원형으로는 못 씀
--     진짜 원형이 필요하면 lemmatization (Stanza 등, 사전 기반이라 무겁고 정확)

-- ============================================
-- 인덱스
-- ============================================
--   CREATE INDEX ON docs USING GIN (to_tsvector('english', body));
--   -> LIKE '%...%' 는 인덱스를 못 타지만 FTS는 GIN 인덱스로 빠름
--   컬럼으로 만들어두면 더 편함:
--     ALTER TABLE docs ADD COLUMN tsv tsvector
--       GENERATED ALWAYS AS (to_tsvector('english', body)) STORED;
--     CREATE INDEX ON docs USING GIN (tsv);

-- ============================================
-- 한국어
-- ============================================
--   PostgreSQL 기본에 한국어 형태소 분석기가 없음
--     to_tsvector('simple', '카드 결제를 했습니다') -> '결제를' '카드' '했습니다'
--     조사가 그대로 붙어 '결제를' 과 '결제가' 가 다른 단어가 됨
--   확장을 따로 깔아야 제대로 됨 (pg_bigm 등)
--   => 이 프로젝트(sgmcp)는 FTS 대신 임베딩 기반 의미 검색을 씀

-- ============================================
-- 언제 뭘 쓰나
-- ============================================
--   LIKE 'abc%'      접두사 매칭. 짧고 간단. 인덱스도 탐 (C 로케일 or text_pattern_ops)
--   LIKE '%abc%'     중간 매칭. 인덱스 못 탐 -> 데이터 많으면 pg_trgm GIN
--   FTS              영어 문서에서 키워드 검색 + 관련도 순위가 필요할 때
--   벡터 검색        의미가 비슷한 걸 찾을 때 ("송금"으로 "계좌 이체"도 걸림)
--   실무에선 FTS + 벡터를 합쳐 쓰기도 함 (하이브리드 검색)

-- FTS 검색 결과 (rank 포함)
--> '카드'로 검색한 결과와 ts_rank 점수를 가져와요
WITH fts_results AS (
    SELECT
        id,
        content,
        ts_rank(
                to_tsvector('simple', content),
                to_tsquery('simple', '카드')
        ) AS fts_score
    FROM doc_embeddings
    WHERE to_tsvector('simple', content) @@ to_tsquery('simple', '카드')
),
-- 벡터 검색 결과 (거리 포함)
    --> 카드 결제 벡터와 코사인 유사도가 높은 순으로 상위 5개
    vec_results AS (
        SELECT
            id,
            content,
            1 - (embedding <=> '[0.82, 0.14, 0.63, 0.45, 0.71, 0.28, 0.56, 0.39]') AS vec_score
        FROM doc_embeddings
        ORDER BY embedding <=> '[0.82, 0.14, 0.63, 0.45, 0.71, 0.28, 0.56, 0.39]'
        LIMIT 5
    )
SELECT
    COALESCE(f.id, v.id)          AS id,
    COALESCE(f.content, v.content) AS content,
    COALESCE(f.fts_score, 0)      AS fts_score,
    COALESCE(v.vec_score, 0)      AS vec_score
FROM fts_results f
    FULL OUTER JOIN vec_results v ON f.id = v.id -- FTS에만 있는 항목과 벡터 검색에만 있는 항목 모두 포함
ORDER BY (COALESCE(f.fts_score, 0) + COALESCE(v.vec_score, 0)) DESC;


-- RRF : 순위를 기반으로 통합
--> 각 방법에서의 순위를 1/(k + 순위) -> k는 상수 (60)
--> 앞서 본 FTS에서는 vector 유사도 값이 다 너무 높아서 fts_score의 역할이 미미함
--> 따라서 vec_score의 점수말고 순위를 사용함 (다들 시험을 너무 잘봐서 절대평가 대신 상대평가로 평가하는 방식)

--> ts_rank 점수 내림차순으로 ROW_NUMBER를 부여
WITH fts_ranked AS (
    SELECT
        id,
        content,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank(
                    to_tsvector('simple', content),
                    to_tsquery('simple', '카드')
                    ) DESC
            ) AS fts_rank
    FROM doc_embeddings
    WHERE to_tsvector('simple', content) @@ to_tsquery('simple', '카드')
),
    vec_ranked AS (
        SELECT
            id,
            content,
            ROW_NUMBER() OVER (
                ORDER BY embedding <=> '[0.82, 0.14, 0.63, 0.45, 0.71, 0.28, 0.56, 0.39]'
                ) AS vec_rank
        FROM doc_embeddings
        LIMIT 5
    )
SELECT
    COALESCE(f.id, v.id)              AS id,
    COALESCE(f.content, v.content)    AS content,
    ROUND((
            COALESCE(1.0 / (60 + f.fts_rank), 0) +
            COALESCE(1.0 / (60 + v.vec_rank), 0)
        )::NUMERIC, 6)                AS rrf_score
FROM fts_ranked f
    FULL OUTER JOIN vec_ranked v ON f.id = v.id
ORDER BY rrf_score DESC;