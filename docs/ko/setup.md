[English](../en/setup.md) | [한국어](setup.md)

# 설정

## 로컬 실행
[uv](https://docs.astral.sh/uv/) 필요.

```bash
uv sync                                                    # 의존성 설치
DATABASE_URL="postgresql://..." uv run python server.py    # 8000번 포트로 실행
```

다른 포트는 `PORT`로 지정. MCP 주소는 `http://localhost:8000/mcp`, 대시보드는 `http://localhost:8000/dashboard`(WorkOS 로그인 필요). 동작 확인:

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}'
```

스키마는 처음 DB에 연결할 때 생성·갱신. 이미 실행한 문장은 `schema_migrations`에 기록되어 다음부터 건너뜀.

## 환경변수
| 변수 | 용도 |
|---|---|
| `DATABASE_URL` | Postgres 접속 문자열(예: Neon). 보관 도구와 대시보드에 필요 |
| `PUBLIC_URL` | 서버의 공개 주소(기본값 `http://localhost:8000`). MCP 리소스 주소와 로그인 콜백 주소의 기준 |
| `MCP_AUTH_ISSUER` | WorkOS AuthKit 도메인. 설정하면 `/mcp`는 AuthKit 액세스 토큰만 허용, 없으면 `/mcp` 개방 |
| `MCP_AUTH_ORG` | `/mcp`와 대시보드를 쓸 수 있는 WorkOS 조직. 이 조직에 추가하는 것이 곧 승인 |
| `WORKOS_CLIENT_ID`, `WORKOS_API_KEY` | 대시보드 로그인에 쓰는 WorkOS 애플리케이션. `<PUBLIC_URL>/auth/callback`을 Redirect URI로 등록. 대시보드에 필요 |
| `WORKOS_COOKIE_PASSWORD` | 대시보드 로그인 쿠키를 봉인하는 Fernet 키. 대시보드에 필요 |
| `INGEST_TOKEN` | `POST /api/ingest` 인증 토큰. Claude Code hook이 사용 |
| `OPENAI_API_KEY` | 임베딩, 의미 검색, 제목·태그·요약 생성. 목록과 키워드 검색은 없어도 동작 |
| `ARCHIVE_LANGUAGE` | 제목, 태그, 요약, 그룹 이름 등 파생 글의 언어(기본값: 한국어) |
| `EVAL_DATABASE_URL` | eval branch 접속 문자열([검색·그룹 평가](eval.md) 참고). eval과 pre-push hook만 사용 |

## 앱 연결
주소 끝에 `/` 금지. `/mcp/`는 307 리다이렉트를 돌려줘서 연결 실패.

| 앱 | 방법 |
|---|---|
| Claude Code | `claude mcp add --transport http sgmcp https://<your-app>.fly.dev/mcp` |
| claude.ai | Settings → Connectors → Add custom connector → `https://<your-app>.fly.dev/mcp` |
| ChatGPT | 같은 주소로 커스텀 커넥터 추가(개발자 모드) |

`MCP_AUTH_ISSUER`가 설정되어 있으면 처음 연결할 때 WorkOS 로그인 창 표시(Claude Code는 `/mcp` 실행 후 서버 선택). `MCP_AUTH_ORG` 멤버만 접근 가능. 아래 Claude Code hook은 영향 없음. `/api/ingest`는 별도의 `INGEST_TOKEN` 사용.

## 자동 저장

### claude.ai와 ChatGPT
앱은 요청이 있을 때만 저장 도구를 호출. 그래서 앱의 상시 지침에 몇 줄을 넣어 두면 모든 대화에서 자동 저장.

- claude.ai: Settings → General → "What personal preferences should Claude consider in responses?"
- ChatGPT: 맞춤형 지침(custom instructions)

고쳐 쓸 수 있는 예시:

```text
sgmcp 커넥터를 쓸 수 있으면:
- 대화의 첫 답변 뒤 save_conversation으로 저장하고, 돌려받은 conversation_key를 기억할 것.
- 이후 답변할 때마다 그 키로 append_messages를 호출해 아직 저장하지 않은 메시지를
  원문 그대로 추가할 것.
- 내가 예전 작업을 언급하면 먼저 sgmcp를 검색하고(search_conversations_by_meaning,
  정확한 이름은 search_conversations), 맞는 구간을 get_conversation으로 읽을 것.
```

제목, 태그, 요약은 서버가 직접 작성. 앱은 글을 보내고 읽기만 담당.

### Claude Code
`hooks/save_conversation.py`를 전역 Stop hook으로 실행. 턴마다 세션의 마지막 메시지 20개를 `/api/ingest`로 보내고, 서버가 더 뒤처져 있을 때만 세션 전체 전송. 로컬 기록 파일을 읽으므로 모델 토큰 사용 없음.

1. 저장소의 `.env`에 서버와 같은 값의 `INGEST_TOKEN` 입력
2. `hooks/save_conversation.py` 맨 위 `SERVER`를 내 앱 주소로 변경
3. `~/.claude/settings.json`에 hook 등록

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run --directory /path/to/sgmcp python /path/to/sgmcp/hooks/save_conversation.py",
            "timeout": 30,
            "async": true
          }
        ]
      }
    ]
  }
}
```

hook은 항상 0으로 종료하고 `~/.claude/sgmcp-hook.log`에 기록. 실패해도 세션을 방해하지 않고, 빠진 턴은 다음 턴에 함께 전송. 지난 세션은 `--backfill`로 일괄 전송.

백그라운드 작업으로 이어 간 세션은 새 ID의 복사본으로 시작해 같은 메시지가 두 번 저장됨. `scripts/merge_duplicates.py`로 이런 복사본을 찾고, `--apply`로 병합.

## 배포
```bash
docker build -t sgmcp . && docker run -p 8000:8000 sgmcp   # 로컬 컨테이너
fly deploy --ha=false                                      # Fly.io
```

Fly secrets: `DATABASE_URL`, `INGEST_TOKEN`, `OPENAI_API_KEY`, `PUBLIC_URL`, `MCP_AUTH_ISSUER`, `MCP_AUTH_ORG`, `WORKOS_CLIENT_ID`, `WORKOS_API_KEY`, `WORKOS_COOKIE_PASSWORD`.
