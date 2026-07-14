# sgmcp

개인용 커스텀 MCP(Model Context Protocol) 서버. FastMCP 기반이며 streamable HTTP transport로 동작한다.

## 제공 툴

| 툴 | 설명 |
|---|---|
| `greet(name)` | 이름을 받아 인사말을 돌려준다 |
| `add(a, b)` | 두 수를 더한다 |
| `save_conversation(messages, source?, project?, key?)` | 대화 최초 생성 (키 비우면 서버가 `cw-`+UUID 발급) 또는 전체 덮어쓰기 수리. 제목·태그·요약은 서버가 자동 생성 |
| `append_messages(key, new_messages, project?)` | 직전 턴 메시지만 덧붙임 — 매 턴 저장용, 중복 자동 방어 |
| `get_conversation(key, full=False)` | 저장된 대화 로드. 기본은 요약 중심, `full=true`면 메시지 원문 포함 |
| `list_conversations(limit)` | 저장된 대화 목록 (최근 갱신순, 본문 제외) |
| `search_conversations(query, limit)` | 제목+요약+본문 키워드 검색 |

## 대시보드

같은 서버가 웹 대시보드를 서빙한다: `https://<호스트>/?token=<DASHBOARD_TOKEN>`

- 대화 목록·검색·열람 (메시지 채팅 뷰)
- 그룹 트리 (대분류 → 소분류) 와 임베딩 3D 프로젝션 (PCA)
- "그룹 새로고침" 버튼: 새 메시지 임베딩 → 드리프트 판정. 걸린 대화가 없으면 여기서 끝(그룹·이름·좌표 유지),
  있으면 제목·태그·요약 재생성 → 태그 병합 → 소분류 묶기(좌표) → 변경된 소분류만 LLM이 이름 짓고
  대분류에 배정 → 좌표 재계산 (`OPENAI_API_KEY` 필요). 저장 경로(append/ingest)는 저장만 한다

## 환경변수

| 변수 | 용도 |
|---|---|
| `DATABASE_URL` | Postgres 접속 문자열 (Neon 등). 대화 저장 툴·대시보드에 필요 |
| `DASHBOARD_TOKEN` | 대시보드 접근 토큰. 미설정 시 대시보드 비활성 |
| `OPENAI_API_KEY` | 임베딩 생성용 (text-embedding-3-small). 없어도 목록/검색은 동작 |
| `INGEST_TOKEN` | `POST /api/ingest` 인증 토큰 (Claude Code hook이 사용) |

## Claude Code 자동 적재 (hook)

`hooks/save_conversation.py`가 Claude Code의 Stop hook으로 등록되면 매 턴 종료 시
세션 transcript(로컬 JSONL)에서 대화 텍스트만 추출해 `/api/ingest`로 업서트한다.
토큰 비용 0, 원문 100%. 로컬 `.env`에 `INGEST_TOKEN`이 있어야 하며,
`--backfill` 플래그로 과거 세션 전체를 일괄 적재할 수 있다.

`greet`/`add`는 아무 환경변수 없이 동작한다.

## 로컬 실행

[uv](https://docs.astral.sh/uv/)가 필요하다.

```bash
uv sync                                       # 의존성 설치
DATABASE_URL="postgresql://..." uv run python server.py    # 기본 8000 포트에서 실행
```

포트를 바꾸려면:

```bash
PORT=8010 uv run python server.py
```

MCP 엔드포인트는 `http://localhost:8000/mcp`. 동작 확인:

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```

## Docker 실행

```bash
docker build -t sgmcp .
docker run -p 8000:8000 sgmcp
```

## 배포 (Fly.io)

```bash
fly deploy
```

## Claude에 연결

엔드포인트 URL 끝에 슬래시를 붙이지 않는다. (`/mcp/`로 등록하면 307 리다이렉트가 발생해 연결에 실패한다.)

- **Claude Code**:

  ```bash
  claude mcp add --transport http sgmcp https://sgmcp-seunggu.fly.dev/mcp
  ```

- **claude.ai**: 설정 → 커넥터 → 커스텀 커넥터 추가 → `https://sgmcp-seunggu.fly.dev/mcp`
