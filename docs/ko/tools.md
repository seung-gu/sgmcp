[English](../en/tools.md) | [한국어](tools.md)

# MCP 도구

주소 `https://<your-app>.fly.dev/mcp`, streamable HTTP. `MCP_AUTH_ISSUER`가 설정되어 있으면 WorkOS 로그인 필요.

| 도구 | 기능 |
|---|---|
| `save_conversation(messages, source?, project?, key?)` | 대화 생성(키를 주지 않으면 서버가 `cw-`+UUID 키 발급). 키를 주면 기존 대화를 통째로 덮어써 복구 |
| `append_messages(key, new_messages, project?, source_url?)` | 아직 저장하지 않은 메시지 추가: 마지막 턴, 그리고 저장을 건너뛴 이전 턴. 이미 저장된 메시지는 한 번만 보관. `source_url`은 다시 열 채팅 주소 |
| `get_conversation(key, full=False, start?, count?)` | 대화 하나 불러오기. 요약 먼저. `full=true`, `start`, `count`를 주면 원문 메시지를 한 페이지(약 20,000자)씩 추가: `start`부터, 없으면 가장 최근 메시지부터. 짧은 대화는 한 번에 전체. `count`는 메시지 수 상한, `next_start`/`prev_start`로 이어 읽기. 메시지마다 보낸 시각(`at`) 포함 |
| `list_conversations(limit)` | 최근 대화 목록. 메시지 본문 제외 |
| `search_conversations(query, limit)` | 제목, 요약, 본문 키워드 검색. 결과마다 키워드가 나온 위치(`sections`, 최신순) 포함 |
| `search_conversations_by_meaning(query, limit)` | 정확한 표현을 모르는 주제의 의미 검색. 결과마다 주제를 다룬 구간(`sections`) 포함. 새 메시지는 저장 후 몇 초 안에 검색 가능 |

## 구간(sections)
검색 결과마다 일치한 메시지 범위인 `sections` 목록 포함.

| 필드 | 의미 |
|---|---|
| `start`, `end`, `length` | 메시지 범위와 그 길이 |
| `best_message` | 가장 잘 맞은 메시지(키워드 검색은 가장 최근 일치) |
| `excerpt` | 그 메시지의 일부 |
| `start_at`, `end_at` | 범위의 첫 메시지와 마지막 메시지를 보낸 시각 |

긴 구간은 그 주제를 한동안 다뤘다는 뜻, 메시지 한두 개짜리 구간은 지나가는 언급. 읽을 때는 `best_message`보다 몇 개 앞을 `start`로 `get_conversation` 호출.

## 페이지 넘김
`get_conversation`은 호출 한 번에 최대 약 20,000자 반환. Claude Code의 도구 출력 한도 안에 들어가는 크기. `showing`은 돌려준 범위, `message_count`는 전체 메시지 수. `next_start`(앞으로)나 `prev_start`(뒤로)로 이어 읽기. 한 페이지보다 긴 메시지 하나는 잘라서 `truncated` 표시.

## 원래 대화 다시 열기
| 출처 | 저장 값 | 사용 방법 |
|---|---|---|
| claude.ai | 채팅 주소. 어시스턴트가 사용자에게 붙여 넣기를 요청 | 대시보드의 링크 |
| Claude Code | `cd <project> && claude --resume <session-id>` | 복사 버튼이 있는 명령. 로컬 기록 파일이 필요해서 그 세션을 실행한 컴퓨터에서만 동작 |

보관 도구도 같은 값을 돌려주므로, 나중에 찾은 대화를 시작한 곳에서 이어 가기 가능.

MCP로 한 검색과 읽기는 모두 `recall_log`에 기록. 어시스턴트가 구간을 끝까지 읽는지, 첫 페이지에서 멈추는지 확인하는 용도.
