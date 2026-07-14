# 임베딩 생성 + 클러스터링 + 3D 투영 — 대시보드 그룹 뷰 재계산

import json
import os
from collections import Counter

import httpx
import numpy as np

import db

EMBED_MODEL = "text-embedding-3-small"
NAMING_MODEL = "gpt-4.1-mini"  # 제목·태그·요약 생성용 경량 모델
# 현재 평균과 앵커의 코사인이 이보다 낮아지면 제목·태그·요약 재생성.
# 실측(2026-07-14): 마지막 10% 성장 ≈ 0.999(스킵), 2배 성장 ≈ 0.983~0.989(발동),
# 다른 주제 25% 유입 ≈ 0.980~0.995(대체로 발동). 오발동 비용이 싸서 민감한 쪽으로 설정.
DRIFT_MIN_COS = 0.99
# 임계값은 센터링된 벡터의 실측 유사도 분포 기준 (같은 주제 0.40~0.92, 다른 주제 -0.43~-0.12)
T_SMALL = 0.35  # 소분류 병합 임계 (cosine) — 대분류 소속은 좌표가 아니라 LLM 배정
MIN_CHARS = 30  # 이보다 짧은 메시지("고마워" 등)는 대표 벡터 계산에서 제외
MIN_GROUP_FOR_CENTERING = 4  # 이보다 작은 언어 그룹은 전체 평균으로 폴백


def _embed_texts(texts: list[str]) -> list[list[float]]:
    api_key = os.environ["OPENAI_API_KEY"]
    vectors: list[list[float]] = []
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(texts), 100):
            response = client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": EMBED_MODEL, "input": texts[i : i + 100]},
            )
            response.raise_for_status()
            vectors.extend(item["embedding"] for item in response.json()["data"])
    return vectors


def _update_conversation_vector(row: dict, texts: list[str]) -> int:
    """새 메시지들을 배치로 임베딩해 기존 가중 평균에 증분 병합. 임베딩된 벡터 수를 반환.

    메시지 벡터는 글자 수로 가중한다 — 긴 본문이 짧은 잡담보다 대화의 위치를 더 많이 결정.
    embedded_vector_count 컬럼은 이 가중치(글자 수)의 누적 합을 저장한다.
    벡터 전체를 메모리에 쌓지 않고 배치(100개)마다 합산 후 버린다 — 256MB 머신 OOM 방지.
    """
    old_weight = row["embedded_vector_count"]
    total_messages = len(row["messages"])
    if not texts:
        # 새 메시지가 전부 필터에 걸림 — 카운터만 최신화
        db.set_embedding_state(row["id"], row["embedding"], total_messages, old_weight)
        return 0
    weighted_sum = None
    new_weight = 0.0
    for i in range(0, len(texts), 100):
        chunk = texts[i : i + 100]
        vectors = np.array(_embed_texts(chunk), dtype=np.float32)
        weights = np.array([len(t) for t in chunk], dtype=np.float32)
        batch = (vectors * weights[:, None]).sum(axis=0)
        weighted_sum = batch if weighted_sum is None else weighted_sum + batch
        new_weight += float(weights.sum())
    if row["embedding"] is not None and old_weight > 0:
        mean = (np.array(row["embedding"], dtype=np.float32) * old_weight + weighted_sum) / (
            old_weight + new_weight
        )
    else:
        mean = weighted_sum / new_weight
    db.set_embedding_state(
        row["id"], [float(x) for x in mean], total_messages, int(old_weight + new_weight)
    )
    return len(texts)


def _language_of(row: dict) -> str:
    """제목+첫 메시지의 한글 비율로 언어 그룹을 정한다 (센터링 그룹 구분용)."""
    text = row["title"] + " " + (row.get("sample") or "")
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "unknown"
    hangul_ratio = sum(1 for c in letters if "가" <= c <= "힣") / len(letters)
    return "ko" if hangul_ratio > 0.3 else "other"


def _center_by_language(matrix: np.ndarray, rows: list[dict]) -> np.ndarray:
    """언어 그룹별 평균을 빼서 '언어·말투' 공통 성분을 제거한다.

    같은 언어끼리 공유하는 방향이 소거돼 주제 차이만 남는다. 소수 그룹은
    자기 평균을 빼면 왜곡되므로(1건이면 벡터가 0이 됨) 전체 평균으로 폴백.
    """
    if len(rows) < MIN_GROUP_FOR_CENTERING:
        return matrix
    centered = matrix.copy()
    global_mean = matrix.mean(axis=0)
    groups: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(_language_of(row), []).append(i)
    for members in groups.values():
        center = (
            matrix[members].mean(axis=0)
            if len(members) >= MIN_GROUP_FOR_CENTERING
            else global_mean
        )
        centered[members] -= center
    return centered


def _agglomerate(vectors: np.ndarray, threshold: float) -> list[list[int]]:
    """중심 코사인 유사도가 threshold 이상인 클러스터 쌍을 반복 병합. 멤버 인덱스 목록 반환."""
    clusters = [[i] for i in range(len(vectors))]
    centroids = vectors.copy()
    while len(clusters) > 1:
        unit = centroids / (np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-9)
        sim = unit @ unit.T
        np.fill_diagonal(sim, -1.0)
        i, j = divmod(int(sim.argmax()), sim.shape[1])
        if sim[i, j] < threshold:
            break
        clusters[i].extend(clusters[j])
        del clusters[j]
        centroids[i] = vectors[clusters[i]].mean(axis=0)
        centroids = np.delete(centroids, j, axis=0)
    return clusters


def _generate_metadata(messages: list[dict]) -> dict:
    """대화 표본을 읽고 제목·태그·요약을 자유롭게 생성한다 (경량 모델).
    표기 통일은 여기서 하지 않는다 — 생성 후 _consolidate_tags가 전역에서 합친다."""
    excerpt = "\n".join(f"[{m['role']}] {m['content'][:400]}" for m in messages)
    prompt = (
        "다음은 한 대화의 표본이다. 이 대화의 메타데이터를 JSON으로만 답하라:\n"
        '{"title": "한 줄 제목 (한국어, 60자 이내)", '
        '"tags": ["주제 태그 2~4개 — 중요한 것부터 순서대로"], '
        '"summary": "핵심 내용·결론 요약 2~4문장 (한국어)"}\n'
        "태그는 대화의 '내용 주제'를 나타내야 한다. 대화에 쓰인 도구나 규칙"
        "(MCP, 지침, 대화 저장 등)은 그것 자체가 주제인 대화가 아니면 태그로 쓰지 마라.\n\n"
        f"{excerpt}"
    )
    with httpx.Client(timeout=60) as client:
        response = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={
                "model": NAMING_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        data = json.loads(response.json()["choices"][0]["message"]["content"])
    return {
        "title": str(data.get("title", ""))[:80],
        "tags": [str(t)[:30] for t in data.get("tags", [])][:5],
        "summary": str(data.get("summary", ""))[:600],
    }


def _refresh_metadata(rows: list[dict]) -> set[int]:
    """앵커가 없거나 드리프트가 임계를 넘은 대화의 제목·태그·요약을 재생성한다.
    재생성된 대화의 id 집합을 반환한다 (이름 단계의 변경 감지에 쓰임)."""
    regenerated: set[int] = set()
    for row in rows:
        current = np.array(row["embedding"], dtype=np.float32)
        anchor = row["tagged_embedding"]
        if anchor is not None:
            anchor_vec = np.array(anchor, dtype=np.float32)
            cos = float(
                current @ anchor_vec
                / ((np.linalg.norm(current) * np.linalg.norm(anchor_vec)) + 1e-9)
            )
            if cos >= DRIFT_MIN_COS:
                continue  # 앵커 이후 의미 있는 이동 없음
        try:
            meta = _generate_metadata(db.messages_excerpt(row["id"]))
            db.set_metadata(
                row["id"], meta["title"], meta["tags"], meta["summary"],
                [float(x) for x in current],
            )
            row["title"], row["tags"], row["summary"] = (
                meta["title"], meta["tags"], meta["summary"],
            )
            regenerated.add(row["id"])
        except Exception:
            continue  # 실패한 대화는 다음 새로고침이 재시도
    return regenerated


def _merge_suggestions(vocabulary: list[str]) -> dict[str, str]:
    """태그 목록에서 같은 뜻인 것들의 병합표를 받아온다 (경량 모델 1회 호출)."""
    prompt = (
        "다음은 대화 아카이브에 쓰인 태그 목록이다. 같은 주제를 다른 표기로 쓴 것들만 합쳐라.\n"
        "확실히 같은 뜻일 때만 합치고, 애매하면 놔둬라. 대표 표기는 가장 간결하고 일반적인 것으로.\n"
        'JSON으로만 답하라: {"merge": {"없앨태그": "대표태그", ...}} — 합칠 게 없으면 {"merge": {}}\n\n'
        + ", ".join(vocabulary)
    )
    with httpx.Client(timeout=60) as client:
        response = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={
                "model": NAMING_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        data = json.loads(response.json()["choices"][0]["message"]["content"])
    return {str(k): str(v) for k, v in data.get("merge", {}).items()}


def _consolidate_tags() -> dict[str, str]:
    """전역 태그 병합: 병합표를 받아 연쇄를 풀고 DB에 적용한다. 적용된 병합표 반환."""
    vocabulary = db.all_tags()
    if len(vocabulary) < 2:
        return {}
    try:
        raw = _merge_suggestions(vocabulary)
    except Exception:
        return {}  # 병합 실패는 치명적이지 않음 — 다음 새로고침이 재시도
    # 연쇄 병합(a→b, b→c)을 끝까지 따라가 평탄화
    mapping = {}
    for source in raw:
        target, seen = source, set()
        while target in raw and target not in seen:
            seen.add(target)
            target = raw[target]
        if source != target and source in vocabulary:
            mapping[source] = target
    if mapping:
        db.apply_tag_mapping(mapping)
    return mapping


def _roof_digest(rows: list[dict], small_clusters: list[list[int]], kept: dict) -> str:
    """변경 없는 소분류들을 대분류별로 묶은 명단 — 이름 + 대표 태그 2개 (배정 후보용)."""
    roofs: dict[str, list[str]] = {}
    for ci, (small_label, big_label) in kept.items():
        top = Counter(
            t for m in small_clusters[ci] for t in (rows[m]["tags"] or [])
        ).most_common(2)
        rep = ", ".join(tag for tag, _ in top)
        roofs.setdefault(big_label or "미분류", []).append(f"{small_label}({rep})")
    return "\n".join(f"- {big}: " + " / ".join(s) for big, s in roofs.items()) or "(아직 없음)"


def _assign_and_name(
    rows: list[dict], small_clusters: list[list[int]], changed: list[int], roofs: str
) -> dict[int, dict] | None:
    """변경된 소분류만 이름 짓고 대분류에 배정한다 (mini 1회). 실패 시 None."""
    if not changed:
        return {}
    payload = {
        str(ci): [rows[m]["tags"] or [] for m in small_clusters[ci]] for ci in changed
    }
    prompt = (
        "대화 아카이브의 그룹 구조를 관리한다. 아래 '변경된 소분류'들에 대해서만:\n"
        "1) 멤버 대화들의 태그(중요순)를 관통하는 소분류 이름을 지어라 (한국어 2~5단어)\n"
        "2) 기존 대분류 중 알맞은 곳의 이름을 적거나, 어디에도 맞지 않으면 새 대분류 이름을 지어라\n"
        "기존 대분류·소분류는 절대 바꾸지 마라. 변경된 것만 답하라.\n"
        'JSON으로만 답하라: {"변경": {"소분류번호": {"이름": "...", "대분류": "..."}}}\n\n'
        f"기존 대분류 명단 (이름 — 소분류들[대표 태그]):\n{roofs}\n\n"
        f"변경된 소분류 (멤버 대화별 태그, 중요순):\n{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        with httpx.Client(timeout=60) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={
                    "model": NAMING_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            data = json.loads(response.json()["choices"][0]["message"]["content"])
        result = {}
        for key, value in data.get("변경", {}).items():
            result[int(key)] = {
                "이름": str(value.get("이름", ""))[:40],
                "대분류": str(value.get("대분류", ""))[:40],
            }
        return result
    except Exception:
        return None


def _label(rows: list[dict], members: list[int]) -> str:
    tag_counts = Counter(tag for m in members for tag in (rows[m]["tags"] or []))
    if tag_counts:
        return tag_counts.most_common(1)[0][0]
    shortest = min((rows[m]["title"] for m in members), key=len)
    return shortest[:30]


def _pca3(vectors: np.ndarray) -> np.ndarray:
    centered = vectors - vectors.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    coords = centered @ vt[: min(3, vt.shape[0])].T
    if coords.shape[1] < 3:  # 레코드가 아주 적으면 부족한 축을 0으로 채움
        coords = np.pad(coords, ((0, 0), (0, 3 - coords.shape[1])))
    return coords


def refresh() -> dict:
    """오래된 임베딩 갱신 → 전체 재클러스터링 → 3D 좌표 재계산."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다 (목록/검색은 키 없이 동작합니다)")

    # 대화별로 "아직 반영 안 된 새 메시지"만 임베딩해 증분 평균에 병합.
    # 대화 단위로 즉시 저장하므로 중간에 실패해도 다음 실행이 이어서 처리한다.
    embedded_count = 0
    for row in db.rows_needing_embedding():
        new_messages = row["messages"][row["embedded_message_count"] :]
        texts = [
            m["content"][:6000]
            for m in new_messages
            if len(m.get("content", "").strip()) >= MIN_CHARS
        ]
        embedded_count += _update_conversation_vector(row, texts)

    rows = db.all_embedded()
    if not rows:
        return {
            "embedded": embedded_count,
            "metadata_generated": 0,
            "tags_merged": 0,
            "total": 0,
            "clusters_small": 0,
            "clusters_big": 0,
        }

    # 드리프트 판정 + 걸린 대화만 제목·태그·요약 재생성
    regenerated = _refresh_metadata(rows)
    if not regenerated:
        # 아무 대화도 안 걸림 → 그룹·이름·좌표 전부 지난번 그대로 (전역 작업 전체 생략)
        return {
            "embedded": embedded_count,
            "metadata_generated": 0,
            "tags_merged": 0,
            "total": len(rows),
            "clusters_small": 0,
            "clusters_big": 0,
            "skipped": "변화 없음",
        }

    # 전역 태그 병합: 같은 뜻 다른 표기를 대표 표기로 통일 (메모리의 rows에도 반영)
    mapping = _consolidate_tags()
    if mapping:
        for row in rows:
            merged = []
            for tag in row["tags"] or []:
                canon = mapping.get(tag, tag)
                if canon not in merged:
                    merged.append(canon)
            row["tags"] = merged

    matrix = np.array([row["embedding"] for row in rows], dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    matrix = _center_by_language(matrix, rows)
    small_clusters = _agglomerate(matrix, T_SMALL)

    # 변경 감지: 멤버 구성이 지난번과 똑같고 재생성된 대화도 없는 소분류는 이름을 유지
    prev_members: dict[int, set] = {}
    for row in rows:
        if row["cluster_small"] is not None:
            prev_members.setdefault(row["cluster_small"], set()).add(row["id"])
    kept: dict[int, tuple[str, str]] = {}
    changed: list[int] = []
    for ci, members in enumerate(small_clusters):
        ids = {rows[m]["id"] for m in members}
        prev_ids = {rows[m]["cluster_small"] for m in members}
        if (
            len(prev_ids) == 1
            and None not in prev_ids
            and prev_members.get(next(iter(prev_ids))) == ids
            and not (ids & regenerated)
            and rows[members[0]]["cluster_small_label"]
        ):
            first = rows[members[0]]
            kept[ci] = (first["cluster_small_label"], first["cluster_big_label"] or "")
        else:
            changed.append(ci)

    # 변경분만 이름 짓고 배정 (mini 1회). 실패하면 최빈 태그 이름으로 혼자 지붕
    names = _assign_and_name(rows, small_clusters, changed, _roof_digest(rows, small_clusters, kept))
    small_label: dict[int, str] = {}
    big_label: dict[int, str] = {}
    for ci in range(len(small_clusters)):
        if ci in kept:
            small_label[ci], big_label[ci] = kept[ci]
        elif names is not None and ci in names and names[ci]["이름"]:
            small_label[ci] = names[ci]["이름"]
            big_label[ci] = names[ci]["대분류"] or names[ci]["이름"]
        else:
            fallback = _label(rows, small_clusters[ci])
            small_label[ci] = fallback
            big_label[ci] = fallback

    big_ids = {name: i for i, name in enumerate(sorted(set(big_label.values())))}
    coords = _pca3(matrix)

    updates = []
    for ci, members in enumerate(small_clusters):
        for m in members:
            updates.append(
                {
                    "id": rows[m]["id"],
                    "cluster_small": ci,
                    "cluster_small_label": small_label[ci],
                    "cluster_big": big_ids[big_label[ci]],
                    "cluster_big_label": big_label[ci],
                    "px": float(coords[m, 0]),
                    "py": float(coords[m, 1]),
                    "pz": float(coords[m, 2]),
                }
            )
    db.apply_grouping(updates)

    return {
        "embedded": embedded_count,
        "metadata_generated": len(regenerated),
        "tags_merged": len(mapping),
        "named": len(changed),
        "total": len(rows),
        "clusters_small": len(small_clusters),
        "clusters_big": len(big_ids),
    }
