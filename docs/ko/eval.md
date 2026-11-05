[English](../en/eval.md) | [한국어](eval.md)

# 검색 평가

`eval/run.py`로 eval 두 가지 실행. 정답과, 정답을 만들 때의 보관함은 별도 Neon branch(`EVAL_DATABASE_URL`)에 보관. 그래서 새 대화가 쌓여도 점수가 흔들리지 않고, 정답이 공개 저장소에 올라가지 않음.

| eval | 평가 대상 |
|---|---|
| meaning | 검토를 마친 질문 62개로 의미 검색 평가: 바꿔 말한 질문, 한 지점을 찾는 질문, 대화 전체를 묻는 질문, 없는 주제 질문 |
| groups | 작은 주제 그룹 평가. LLM 두 번의 분류가 일치한 대화 쌍 기준 |

둘 다 LLM을 호출하지 않아서 같은 코드면 항상 같은 점수. 실행할 때마다 결과를 branch에 저장하고, 기준선으로 받아들인 실행과 비교. 점수가 떨어지면 종료 코드 1.

```bash
uv run --group eval --env-file .env python eval/run.py            # 점수 계산, 기준선과 비교
uv run --group eval --env-file .env python eval/run.py --accept   # 이번 실행을 기준선으로
git config core.hooksPath .githooks   # 검색·그룹 코드를 바꾼 push 전에 자동 실행
```

키워드 검색은 eval 없음. 문자열을 찾거나 못 찾거나 둘 중 하나라서 점수로 조정할 값이 없기 때문.
