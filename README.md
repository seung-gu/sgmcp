# sgmcp

개인용 커스텀 MCP(Model Context Protocol) 서버. FastMCP 기반이며 streamable HTTP transport로 동작한다.

## 제공 툴

| 툴 | 설명 |
|---|---|
| `greet(name)` | 이름을 받아 인사말을 돌려준다 |
| `add(a, b)` | 두 수를 더한다 |

## 로컬 실행

[uv](https://docs.astral.sh/uv/)가 필요하다.

```bash
uv sync                    # 의존성 설치
uv run python server.py    # 기본 8000 포트에서 실행
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
