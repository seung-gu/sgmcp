/*
SQL 문법 읽기 요령
당장 쓸 수 있는 요령

쓸 때는 SQL 순서대로 쓰되, 읽을 때는 FROM부터 읽으세요.

FROM     ← ① 어디서
JOIN     ← ② 뭘 붙여서
WHERE    ← ③ 뭘 걸러서
GROUP BY ← ④ 어떻게 묶어서
SELECT   ← ⑤ 뭘 보여줄지

복잡한 쿼리를 만나면 SELECT를 손으로 가리고 FROM부터 보세요. 훨씬 빨리 읽힙니다.
*/

-- select title, tags from conversations;

-- select 0.1::numeric(15,2) + 0.2::numeric(15,2); -> 15자리, 소수점 2자리


-- CHAR(n) -> 고정 길이 문자열
-- VARCHAR(n) -> 가변 길이 문자열 (n은 최대 길이)
-- TEXT -> 가변 길이 문자열, 길이 제한 없음

--select 'Hello'::varchar(3);

-- DATE -> 날짜 (YYYY-MM-DD)
-- TIME -> 시간 (HH:MM:SS)
-- TIMESTAMP -> 날짜와 시간 (YYYY-MM-DD HH:MM:SS)
-- TIMESTAMPTZ -> 타임존이 포함된 날짜와 시간 (YYYY-MM-DD HH:MM:SS+TZ)

-- select '2024-01-15'::TIMESTAMPTZ;

-- create table 테이블 이름 (
--   컬럼이름 데이터타입 제약조건,
--   컬럼이름 데이터타입 제약조건,
--   ...
-- );

-- 기본 키: 행이 추가 될 때마다 자동으로 증가
-- -> ULID(Universally Unique Lexicographically Sortable Identifier), UUID(Universally Unique Identifier)

-- serial -> INTERGER 범위 : 약 21억
-- bigserial -> BIGINT 범위 : 약 920경
-- bigserial 추천 - 21억은 거래내역 같은건 금방 참

-- create table test (
--     customer_id bigserial primary key
--  UNIQUE: 같은 값을 허용하지 않는 제약 조건
--  DEFAULT: 컬럼에 값이 지정되지 않았을 때 기본값을 설정하는 제약 조건
--  NOT NULL: 컬럼에 NULL 값을 허용하지 않는 제약 조건
--  CHECK: 컬럼에 저장되는 값이 특정 조건을 만족하는지 검사하는 제약 조건
-- );

/*
create table if not exists customers (
    customer_id bigserial primary key,
    name varchar(100) not null,
    email varchar(255) unique not null,
    phone varchar(20),
    created_at timestamptz default now()
);

create table if not exists accounts (
    account_id bigserial primary key,
    customer_id bigint not null,
    account_type varchar(20) not null,
    balance numeric(15, 2) default 0,
    created_at timestamptz default now(),
    check (balance >= 0)
);

-- insert into customers (name, email, phone) values ('John Doe', 'john.doe@example.com', '123-456-7890');

insert into customers (name, email, phone) 
values 
    ('John Doe', 'john.doe@example.com', '123-456-7890'),
    ('Jane Smith', 'jane.smith@example.com', '098-765-4321'),
    ('Alice Johnson', 'alice.johnson@example.com', '555-1234')
returning *;

insert into accounts (customer_id, account_type, balance)
values
    (1, 'savings', 1000.00),
    (1, 'checking', 500.00),
    (2, 'savings', 1500.00),
    (3, 'checking', 200.00)
returning *;

-- 반드시 update, delete 에는 where 조건을 붙여야 한다. 안붙이면 모든 row가 수정됨
-- update customers set name = '김지수(수정)', phone = '010-1234-5678' where customer_id = 1 returning name, phone;
-- delete from customers where customer_id = 1 returning *;
*/

-- select distinct tags, title from conversations where tags is not null order by tags;

-- select px, py, pz, tags from conversations where px > 0 and py > 0; 
-- select px, py, pz from conversations where px <> py;  -- <> = !=
-- select * from conversations where id in (1, 2, 3, 4, 5) order by id desc;
-- select * from conversations where id between 1 and 15 order by id;

-- Like 연산자: 문자열 패턴 매칭, %. 대소문자 구분, 인덱스 타지 않음, ILIKE 연산자: 대소문자 구분하지 않음
--select * from conversations where title like '%AI%';  -- AI로 끝나는 문자열
--select * from conversations where title like 'AI%';  -- AI로 시작하는 문자열
--select * from conversations where title like '%AI%';  -- AI가 포함된 문자열

-- NULL 값 비교: IS NULL, IS NOT NULL
--select * from conversations where title is null;
--select * from conversations where title is not null;

-- COALESCE 함수: NULL 값을 다른 값으로 대체
--select coalesce(title, 'No Title') as title from conversations;
-- nullif 함수: 두 값이 같으면 NULL 반환, 다르면 첫 번째 값 반환
-- select coalesce(nullif(title, ''), 'No Title') as title from conversations; -- title이 ''이면 null 반환, null이면 'No Title' 반환

/*SELECT round((1 - (a.embedding_vec <=> b.embedding_vec))::numeric, 4) as similarity, 
        a.title as title_a,
        b.title as title_b
from conversations a join conversations b on a.id > b.id order by a.embedding_vec <=> b.embedding_vec;
*/

-- UPSERT 개념
-- 데이터가 없으면 INSERT, 데이터가 있으면 UPDATE
-- select로 있는지 확인 하고 없으면 insert, 있으면 update 하는 방식으로 구현 가능 => 두 단계
-- query 2개 실행 사이에 다른 트랜잭션이 끼어들 수 있음
-- 따라서 이 두 단계를 하나의 트랜젝션으로 (쿼리로) 처리함
-- MySQL: INSERT ... ON DUPLICATE KEY UPDATE
-- PostgreSQL: INSERT ... ON CONFLICT (컬럼) DO UPDATE SET 컬럼 = 값

-- insert into customers (name, email) values ('John Doe', 'john.doe@example.com');
---- error: duplicate key value violates unique constraint "customers_email_key"
---- DETAIL: Key (email)=(john.doe@example.com) already exists.

---- insert on conflict do nothing
-- insert into customers (name, email) values ('John Doe', 'john.doe@example.com') on conflict (email) do nothing;

---- insert on conflict do update set
--insert into customers (name, email, phone) values ('John Doe', 'john.doe@example.com', '123-456-7890') 
--    on conflict (email) do update set phone = excluded.phone returning *;  
---- excluded: 충돌이 발생한 행의 값, 즉 삽입하려던 값
---- select 대신 insert로 집어 넣고 혹시 중복된 값이 있으면 (select랑 같은 로직) update로 처리하는 방식 (excluded 이용해서) => UPSERT
/*
drop table if EXISTS accounts;
drop table if EXISTS customers;

CREATE TABLE customers (
    customer_id BIGSERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email       VARCHAR(255) UNIQUE NOT NULL,
    phone       VARCHAR(20),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

create table accounts (
    account_id   BIGSERIAL     PRIMARY KEY,
    customer_id  BIGINT        NOT NULL,
    account_type VARCHAR(20)   NOT NULL CHECK (account_type IN ('예금', '적금', '입출금')),
    balance      NUMERIC(15,2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at   TIMESTAMPTZ   DEFAULT NOW()
);

INSERT INTO customers (name, email, phone) VALUES
    ('김지수', 'jisu@example.com',   '010-1234-5678'),
    ('박민준', 'minjun@example.com', '010-9876-5432'),
    ('이하은', 'haeun@example.com',  '010-5555-1234'),
    ('최서준', 'seojun@example.com', '010-2222-3333'),
    ('정유나', 'yuna@example.com',   NULL);

INSERT INTO accounts (customer_id, account_type, balance) VALUES
    (1, '예금',    1500000.00),
    (1, '적금',    3000000.00),
    (2, '예금',     250000.00),
    (3, '입출금',    80000.00),
    (4, '예금',    5200000.00),
    (4, '적금',    1800000.00),
    (5, '적금',    1200000.00),
    (5, '입출금',       0.00);

UPDATE accounts
SET balance = 1750000.00
WHERE customer_id = 1 AND account_type = '예금'
RETURNING account_id, account_type, balance;
*/
--SELECT balance FROM accounts ORDER BY balance DESC;
---- DESC : 내림차순 ASC : 오름차순 (아무것도 안붙이면 ASC)

--SELECT accounts.account_type, accounts.balance FROM accounts
--    ORDER BY account_type ASC, balance DESC;
---- account_typ을 먼저 오름차순, 같은 account_type이면 balance를 내림차순으로 정렬

SELECT name, phone FROM customers ORDER BY phone ASC NULLS LAST; 
-- NULLS FIRST, NULLS LAST: NULL 값이 먼저 나오게 할지, 마지막에 나오게 할지 지정

---- LIMIT, OFFSET
---- LIMIT: 결과 행의 최대 개수를 제한
---- OFFSET: 결과 행의 시작 위치를 지정 (0부터 시작)
SELECT balance FROM accounts ORDER BY balance DESC LIMIT 3;
SELECT balance FROM accounts ORDER BY balance DESC LIMIT 3 OFFSET 3;
SELECT balance FROM accounts ORDER BY balance DESC LIMIT 3 OFFSET 6;


SELECT NOW();   -- 현재 날짜, 시각, 타임존 정보. +09 한국 표준 시 (UTC +9)
SELECT CURRENT_DATE;
SELECT CURRENT_TIMESTAMP; -- == NOW()

-- DATE_TRUNC
SELECT name, created_at, DATE_TRUNC('hour', created_at) AS 가입월 FROM customers ORDER BY 가입월;
-- DATE_TRUNC('day', created_at) : 00:00:00
-- DATE_TRUNC('hour', created_at) : 정각
-- year, week, quarter, minute

-- EXTRACT
SELECT 
    name,
    EXTRACT(YEAR FROM created_at) AS 가입년도,
    EXTRACT(MONTH FROM created_at) AS 가입월,
    EXTRACT(DAY FROM created_at) AS 가입일,
    EXTRACT(DOW FROM created_at) AS 요일번호 -- 요일 (0: 일요일, 1: 월요일, ..., 6: 토요일)
FROM customers;

-- Timezone 변환
SELECT
    NOW() AS 서버시각,
    NOW() AT TIME ZONE 'UTC' AS UTC시각,
    NOW() AT TIME ZONE 'Asia/Seoul' AS 서울시각,
    NOW() AT TIME ZONE 'America/New_York' AS 뉴욕시각,
    NOW() AT TIME ZONE 'Europe/London' AS 런던시각;

-- 집계 함수
SELECT COUNT(*) FROM customers; -- 전체 행 개수 (COUNT *), COUNT(컬럼명): NULL 제외한 행 개수
SELECT COUNT(phone) FROM customers; -- NULL 제외한 행 개수

SELECT
    SUM(balance) AS 총잔액,
    ROUND(AVG(balance), 2) AS 평균잔액,
    MIN(balance) AS 최소잔액,
    MAX(balance) AS 최대잔액
FROM accounts;

SELECT * FROM accounts;
-- 예금 계좌의 합계, 적금 계좌의 합계 
-- -> GROUP BY를 사용하면 SELECT에는 GROUP BY에 포함된 컬럼과 집계 함수만 올 수 있음
SELECT account_type, SUM(balance) AS 타입별총잔액 FROM accounts GROUP BY account_type;

-- HAVING
-- WHERE: pre-filtering, HAVING: post-filtering
SELECT account_type, SUM(balance) AS 타입별총잔액
FROM accounts
WHERE balance > 0
GROUP BY account_type
HAVING SUM(balance) > 1000000; -- 집계 한 이후에 계산해야하니까 HAVING을 사용해야 됨 (이 경우)

CREATE TABLE transactions (
    transaction_id BIGSERIAL PRIMARY KEY,
    account_id     BIGINT NOT NULL,
    type           VARCHAR(10) NOT NULL CHECK (type IN ('입금', '출금')),
    amount         NUMERIC(15,2) NOT NULL CHECK (amount > 0),
    description    TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO transactions (account_id, type, amount, description, created_at) VALUES
    (1, '입금', 500000,  '월급',           '2024-10-05 09:00:00+09'),
    (1, '출금', 150000,  '관리비',          '2024-10-10 11:30:00+09'),
    (1, '출금', 80000,   '식료품',          '2024-10-15 14:20:00+09'),
    (1, '입금', 200000,  '부수입',          '2024-10-20 16:00:00+09'),
    (2, '입금', 300000,  '적금 납입',       '2024-10-01 10:00:00+09'),
    (2, '입금', 300000,  '적금 납입',       '2024-11-01 10:00:00+09'),
    (3, '입금', 1000000, '전세보증금 반환',  '2024-10-12 15:00:00+09'),
    (3, '출금', 500000,  '이체',           '2024-10-18 13:00:00+09'),
    (3, '출금', 200000,  '카드대금',        '2024-11-05 10:30:00+09'),
    (4, '입금', 50000,   '용돈',           '2024-10-01 09:00:00+09'),
    (4, '출금', 30000,   '편의점',          '2024-10-08 20:10:00+09'),
    (4, '입금', 50000,   '용돈',           '2024-11-01 09:00:00+09'),
    (5, '입금', 2000000, '보너스',          '2024-10-25 17:00:00+09'),
    (5, '출금', 800000,  '여행경비',        '2024-10-28 08:00:00+09'),
    (5, '출금', 450000,  '가전제품',        '2024-11-10 14:00:00+09'),
    (6, '입금', 300000,  '적금 납입',       '2024-11-01 10:00:00+09'),
    (6, '출금', 100000,  '외식',           '2024-11-15 19:30:00+09'),
    (7, '입금', 300000,  '적금 납입',       '2024-10-01 10:00:00+09'),
    (7, '입금', 300000,  '적금 납입',       '2024-11-01 10:00:00+09'),
    (8, '입금', 10000,   '이자',           '2024-10-31 00:00:00+09'),
    (8, '입금', 10000,   '이자',           '2024-11-30 00:00:00+09'),
    (1, '출금', 120000,  '통신비',          '2024-11-10 09:00:00+09'),
    (3, '입금', 750000,  '월급',           '2024-11-05 09:00:00+09');


-- 외래 키 : 한 테이블의 컬럼이 다른 테이블의 기본 키를 참조
-- ALTER: 기존 테이블에 컬럼 추가, 삭제, 수정, 제약 조건 추가, 삭제 등
-- RESTRICT: 참조하는 데이터가 있다면 삭제/수정 불가
-- CASCADE: 참조하는 행을 함께 삭제/수정
-- SET NULL: 참조하는 행이 삭제/수정되면 외래 키 컬럼을 NULL로 설정 -> 댓글에 보통 사용
ALTER TABLE accounts DROP CONSTRAINT IF EXISTS fk_accounts_customer;
ALTER TABLE accounts ADD CONSTRAINT fk_accounts_customer FOREIGN KEY (customer_id) REFERENCES customers(customer_id) ON DELETE CASCADE ON UPDATE CASCADE;

-- CASCADE 테스트
--customer_id 1번 고객 수정
UPDATE customers SET name = '김지수(수정)' WHERE customer_id = 10;
UPDATE customers SET customer_id = 1 WHERE customer_id = 10;

ALTER TABLE transactions
    ADD CONSTRAINT fk_transactions_account
        FOREIGN KEY (account_id)
            REFERENCES accounts(account_id);


-- 정규화
-- 자주 변경되지 않는 데이터는 별도의 테이블로 분리하여 관리
CREATE TABLE customer_profiles (
    profile_id  BIGSERIAL PRIMARY KEY,
    customer_id BIGINT UNIQUE NOT NULL,
    birth_date  DATE,
    address     TEXT,
    CONSTRAINT fk_profile_customer
    FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)
        ON DELETE CASCADE
);

-- customers -> accounts -> transcations
-- 1:N 관계
-- 한 고객은 여러 개의 계좌를 가질 수 있음

-- 1:1 관계
-- 한 고객은 하나의 프로필을 가질 수 있음

-- 은행 지점과 계좌  (N:M)
--> 하나의 계좌는 여러 지점에서 서비스를 받을 수 있죠.
--> 하나의 지점도 여러 계좌에 대한 서비스를 제공
--> mapper 테이블을 혼용해서 구성 (중간 단계)



-- JOIN: join도 가급적 rdbms일지라도 줄이는게 성능에 좋음
INSERT INTO accounts
    (customer_id, account_type, balance)
VALUES (3, '입출금', 0);

-- INNER JOIN: 두 테이블에서 일치하는 ON 조건을 만족하는 행만 반환 (가장 많이 사용)
SELECT
    c.name,
    a.account_id,
    a.account_type,
    a.balance
FROM customers AS c
    INNER JOIN accounts AS a 
        ON c.customer_id = a.customer_id
ORDER BY c.customer_id, a.account_id;

-- LEFT JOIN: 왼쪽 테이블의 모든 행은 기본적으로 포함 (RIGHT JOIN도 있지만 거의 LEFT JOIN만 씀)
SELECT
    a.account_id,
    a.account_type,
    COUNT(t.transaction_id) AS 거래건수
FROM accounts a
    LEFT JOIN transactions t ON t.account_id = a.account_id
GROUP BY a.account_id, a.account_type
ORDER BY a.account_id;

-- FULL OUTER JOIN: 양쪽 테이블의 모든 행을 반환, 일치하지 않는 경우 NULL로 채움. 간간이 서버측에서 데이터 검증 용도로 사용 
/*
두 데이터가 같아야 하는 상황을 생각해보세요. 옮기기 전 테이블과 옮긴 후 테이블, 또는 우리 장부와 상대방 장부요. 이때 어긋나는 방식이 세 가지예요.

1. A에는 있는데 B에 없음
2. B에는 있는데 A에 없음
3. 양쪽 다 있는데 값이 다름

INNER JOIN을 쓰면 1번과 2번이 아예 안 보여요. 짝 없는 줄은 결과에서 빠지니까요. 없어진 데이터를 찾으려는데 없어진 게 안 보이는 셈이죠.

LEFT JOIN은 1번은 잡아도 2번을 못 잡아요. 그래서 두 번 돌려야 하고요.

FULL OUTER JOIN은 양쪽을 다 남기니까 셋을 한 번에 봅니다.
*/
SELECT
    c.customer_id,
    c.name,
    a.account_id,
    a.account_type
FROM customers c
    FULL OUTER JOIN accounts a ON a.customer_id = c.customer_id
ORDER BY c.customer_id NULLS LAST, a.account_id;

SELECT c.customer_id, c.name, a.account_id
FROM customers c
    FULL OUTER JOIN accounts a ON a.customer_id = c.customer_id
WHERE c.customer_id IS NULL OR a.account_id IS NULL;


-- CROSS JOIN: 두 테이블의 모든 조합을 반환 (카테시안 곱 - 행렬의 곱: NxS x SxM = NxM) 성능 때문에 잘 안쓰임
SELECT
    a.account_type,
    c.name
FROM (SELECT DISTINCT account_type FROM accounts) a
    CROSS JOIN (SELECT name FROM customers LIMIT 3) c
ORDER BY a.account_type, c.name;


SELECT
    c.customer_id,
    c.name AS 고객명,
    COUNT(a.account_id) AS 계좌수,
    COALESCE(SUM(a.balance), 0) AS 총잔액
FROM customers c
    LEFT JOIN accounts a ON a.customer_id = c.customer_id
GROUP BY c.customer_id, c.name
ORDER BY 총잔액 DESC;


-- 휴면 계좌 찾기
-- customers 테이블은 반드시 필요하니 inner join
-- transactions 테이블에서 transaction이 없는걸 찾으니 left join
SELECT
    a.account_id,
    c.NAME AS 고객명,
    a.account_type AS 계좌유형,
    a.balance AS 잔액,
    a.created_at AS 개설일
FROM accounts a
    INNER JOIN customers c ON a.customer_id = c.customer_id
    LEFT JOIN transactions t ON a.account_id = t.account_id
WHERE t.transaction_id IS NULL
ORDER BY a.created_at DESC;


-- 서브쿼리

SELECT *
    FROM accounts
    WHERE balance > (SELECT AVG(balance) FROM accounts)
ORDER BY balance DESC;
-- (SELECT AVG(balance) FROM accounts) 같은게 먼저 실행되는 서브 쿼리

-- 인라인 뷰 : FROM 에 사용되는 서브 쿼리
-- 계좌별 거래 건수 먼저 집계 뒤 그 결과에서 거래가 3건 이상인 경우만 조회
SELECT
    account_id,
    COUNT(transaction_id) AS 거래건수
    FROM transactions
GROUP BY account_id 
HAVING COUNT(transaction_id) >=3;

--> GROUP BY를 하고 나야 거래건수를 볼 수 있기 때문에 GROUP BY 전에 실행되는 WHERE로는 할 수 없음
-- 그렇다면 HAVING 말고 PRE_FILTER로 할 수 있는 방법 : 서브쿼리

SELECT account_id, 거래건수
FROM (
    SELECT account_id, COUNT(*) AS 거래건수
    FROM transactions
    GROUP BY account_id
) AS 계좌별집계
WHERE 거래건수 >= 3
ORDER BY 거래건수 DESC;
-- 서브쿼리 쓸때는 '계좌별집계' 처럼 FROM과 함께 꼭 별칭을 지어야 함

-- 스칼라 서브쿼리: SELECT에 사용하는 서브 쿼리
-- 행마다 실행되기 때문에 성능에 영향을 줄 수 있음 (잘 고려해서 사용)

-- 계좌의 잔액과 전체 평균 잔액
SELECT
    account_id,
    account_type,
    balance,
    (SELECT ROUND(AVG(balance), 0) FROM accounts) AS 전체평균잔액,
    balance - (SELECT ROUND(AVG(balance), 0) FROM accounts) AS 평균대비차이
FROM accounts;

-- EXISTS: 서브쿼리의 결과가 존재하는지 여부를 확인 (boolean 반환)
-- 안쪽이 바깥 값을 참조하는 서브쿼리를 상관 서브쿼리라고 합니다. 
-- 앞에서 본 인라인 뷰와 다른 점이 이거예요. 인라인 뷰는 혼자 실행되지만, 이건 바깥 행마다 값이 바뀌니까 혼자서는 실행이 안 됩니다.
SELECT account_id, account_type, balance
FROM accounts a
WHERE EXISTS (
    SELECT 1  -- 관행
    FROM transactions t
    WHERE t.account_id = a.account_id
)
ORDER BY account_id;


-- CTE (WITH 절): 서브쿼리 블록화
WITH 계좌별집계 AS (
    SELECT account_id, COUNT(*) AS 거래건수
    FROM transactions
    GROUP BY account_id
)
SELECT account_id, 거래건수 FROM 계좌별집계
WHERE 거래건수 >= 3
ORDER BY 거래건수 DESC;

-- 위에것과 같은 표현 (성능 차이도 없으나 CTE가 가독성도 좋고 재사용성도 좋음)
SELECT account_id, 거래건수
FROM (
    SELECT account_id, COUNT(*) AS 거래건수
    FROM transactions
    GROUP BY account_id
) AS 계좌별집계
WHERE 거래건수 >= 3
ORDER BY 거래건수 DESC;


-- 고객별 총 거래 금액을 구하고, 평균 이상의 거래 금액을 가진 고객만 조회
WITH 계좌별집계 AS (
    SELECT account_id, SUM(amount) AS 거래금액
    FROM transactions
    GROUP BY account_id
)
SELECT account_id, 거래금액
FROM 계좌별집계
WHERE 거래금액 > (SELECT ROUND(AVG(거래금액), 0) FROM 계좌별집계)
ORDER BY 거래금액 DESC;

WITH 계좌별거래합계 AS (
    SELECT account_id, SUM(amount) AS 총거래금액
    FROM transactions
    GROUP BY account_id
),
고객별합계 AS (
    SELECT a.customer_id, SUM(c.총거래금액) AS 고객총거래금액
    FROM accounts AS a
    JOIN 계좌별거래합계 AS c ON c.account_id = a.account_id
    GROUP BY a.customer_id
),
전체평균 AS (
    SELECT AVG(고객총거래금액) AS 평균거래금액
    FROM 고객별합계
)
SELECT
    cu.name          AS 고객명,
    g.고객총거래금액,
    ROUND(p.평균거래금액, 0) AS 전체평균
FROM 고객별합계 g
        JOIN customers cu ON cu.customer_id = g.customer_id
        CROSS JOIN 전체평균 p
WHERE g.고객총거래금액 >= p.평균거래금액
ORDER BY g.고객총거래금액 DESC;


-- 윈도우 함수
INSERT INTO transactions (account_id, type, amount, description, created_at) VALUES
    (1, '입금', 500000,  '월급',      '2024-12-05 09:00:00+09'),
    (1, '출금', 200000,  '보험료',    '2024-12-12 10:00:00+09'),
    (1, '출금', 90000,   '식료품',    '2024-12-20 15:00:00+09'),
    (1, '입금', 500000,  '월급',      '2025-01-05 09:00:00+09'),
    (1, '출금', 150000,  '관리비',    '2025-01-10 11:00:00+09'),
    (2, '입금', 300000,  '적금 납입', '2024-12-01 10:00:00+09'),
    (2, '입금', 300000,  '적금 납입', '2025-01-01 10:00:00+09'),
    (3, '출금', 350000,  '카드대금',  '2024-12-05 10:00:00+09'),
    (3, '입금', 750000,  '월급',      '2024-12-05 09:00:00+09'),
    (3, '출금', 400000,  '카드대금',  '2025-01-05 10:00:00+09'),
    (3, '입금', 750000,  '월급',      '2025-01-05 09:00:00+09'),
    (4, '입금', 50000,   '용돈',      '2024-12-01 09:00:00+09'),
    (4, '출금', 40000,   '편의점',    '2024-12-18 20:00:00+09'),
    (4, '입금', 50000,   '용돈',      '2025-01-01 09:00:00+09'),
    (5, '입금', 2000000, '보너스',    '2024-12-20 17:00:00+09'),
    (5, '출금', 600000,  '여행경비',  '2024-12-26 08:00:00+09'),
    (5, '입금', 500000,  '월급',      '2025-01-05 09:00:00+09'),
    (6, '입금', 300000,  '적금 납입', '2024-12-01 10:00:00+09'),
    (6, '출금', 80000,   '외식',      '2024-12-22 19:00:00+09'),
    (6, '입금', 300000,  '적금 납입', '2025-01-01 10:00:00+09'),
    (7, '입금', 300000,  '적금 납입', '2024-12-01 10:00:00+09'),
    (7, '입금', 300000,  '적금 납입', '2025-01-01 10:00:00+09'),
    (8, '입금', 10000,   '이자',      '2024-12-31 00:00:00+09'),
    (8, '입금', 10000,   '이자',      '2025-01-31 00:00:00+09');


SELECT account_id, SUM(amount) AS 총거래금액
FROM transactions
GROUP BY account_id;

-- 윈도우 함수: 집계 함수와 달리 그룹핑하지 않고, 각 행마다 계산 가능
/*
GROUP BY는 행을 접습니다:

account_id | 합계
-----------+-----------
    1     | 1,050,000     ← 5건이 1줄로 접힘
    2     |   600,000     ← 2건이 1줄로

PARTITION BY는 행을 그대로 두고 옆에 붙입니다:

account_id |  amount  | 계좌총액
-----------+----------+-----------
    1     |  500,000 | 1,050,000
    1     |  150,000 | 1,050,000     ← 원본 5줄이 그대로 살아있고
    1     |   80,000 | 1,050,000       합계가 옆에 반복해서 붙음
    1     |  200,000 | 1,050,000
    1     |  120,000 | 1,050,000
    2     |  300,000 |   600,000
    2     |  300,000 |   600,000

개별 거래도 보면서 그 계좌 합계도 같이 보고 싶을 때 씁니다. GROUP BY로는 개별 거래가 사라져서 못 해요.
SUM(amount) OVER ()   -- 전 계좌 통합 합계가 모든 행에 붙음

자주 같이 쓰는 것

ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY created_at)
"계좌별로 나눠서, 각각 시간순 번호 매기기" — 계좌마다 1, 2, 3... 이 다시 시작합니다. 계좌별 최근 거래 3건만 뽑을 때 이렇게 씁니다.

account_id |   총액    | ROW_NUMBER | RANK | DENSE_RANK
-----------+-----------+------------+------+-----------
    5     | 3,250,000 |     1      |  1   |     1
    3     | 2,450,000 |     2      |  2   |     2
    1     | 1,050,000 |     3      |  3   |     3
    2     |   600,000 |     4      |  4   |     4    ← 동점
    7     |   600,000 |     5      |  4   |     4    ← 동점
    6     |   400,000 |     6      |  6   |     5
    4     |   130,000 |     7      |  7   |     6
    8     |    20,000 |     8      |  8   |     7
*/
SELECT
    transaction_id,
    account_id,
    amount,
    SUM(amount) OVER (PARTITION BY account_id) AS 계좌별총거래금액
FROM transactions
ORDER BY account_id, transaction_id;

-- LAG, LEAD
--> LAG: 현재 행의 이전 행 값을 참조
--> LEAD: 현재 행의 다음 행 값을 참조
SELECT
    transaction_id,
    account_id,
    amount,
    created_at,
    LAG(amount) OVER (
        PARTITION BY account_id
        ORDER BY created_at
    ) AS 직전거래금액,
    amount - LAG(amount) OVER (
        PARTITION BY account_id
        ORDER BY created_at
    ) AS 직전대비증감
FROM transactions
ORDER BY account_id;

-- 기본 프레임 : ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
--                  --> 파티션 시작부터 현재 행까지
--            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
--                  --> 처음부터 현재 행까지 : 이걸 범위로 본다