# 임베딩 생성 + 클러스터링 + 3D 투영 — 대시보드 그룹 뷰 재계산

import os
from collections import Counter

import httpx
import numpy as np

import db

EMBED_MODEL = "text-embedding-3-small"
T_SMALL = 0.62  # 소분류 병합 임계 (cosine)
T_BIG = 0.45  # 대분류 병합 임계
MIN_CHARS = 30  # 이보다 짧은 메시지("고마워" 등)는 대표 벡터 계산에서 제외


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


def _update_conversation_vector(row: dict, vectors: np.ndarray | None, new_count: int) -> int:
    """새 메시지 벡터들을 기존 이동 평균에 증분 병합. 임베딩된 벡터 수를 반환."""
    old_count = row["embedded_vector_count"]
    total_messages = len(row["messages"])
    if vectors is None or len(vectors) == 0:
        # 새 메시지가 전부 필터에 걸림 — 카운터만 최신화
        db.set_embedding_state(row["id"], row["embedding"], total_messages, old_count)
        return 0
    new_sum = vectors.sum(axis=0)
    if row["embedding"] is not None and old_count > 0:
        mean = (np.array(row["embedding"], dtype=np.float32) * old_count + new_sum) / (
            old_count + len(vectors)
        )
    else:
        mean = new_sum / len(vectors)
    db.set_embedding_state(
        row["id"], [float(x) for x in mean], total_messages, old_count + len(vectors)
    )
    return len(vectors)


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

    # 대화별로 "아직 반영 안 된 새 메시지"만 골라 한 번에 임베딩 후 증분 평균에 병합
    stale = db.rows_needing_embedding()
    batches: list[tuple[dict, list[str]]] = []
    for row in stale:
        new_messages = row["messages"][row["embedded_message_count"] :]
        texts = [
            m["content"][:6000]
            for m in new_messages
            if len(m.get("content", "").strip()) >= MIN_CHARS
        ]
        batches.append((row, texts))

    all_texts = [t for _, texts in batches for t in texts]
    all_vectors = _embed_texts(all_texts) if all_texts else []

    embedded_count = 0
    position = 0
    for row, texts in batches:
        vectors = (
            np.array(all_vectors[position : position + len(texts)], dtype=np.float32)
            if texts
            else None
        )
        position += len(texts)
        embedded_count += _update_conversation_vector(row, vectors, len(texts))

    rows = db.all_embedded()
    if not rows:
        return {"embedded": embedded_count, "total": 0, "clusters_small": 0, "clusters_big": 0}

    matrix = np.array([row["embedding"] for row in rows], dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9

    small_clusters = _agglomerate(matrix, T_SMALL)
    small_centroids = np.array([matrix[m].mean(axis=0) for m in small_clusters])
    big_of_small = _agglomerate(small_centroids, T_BIG)
    coords = _pca3(matrix)

    updates = []
    for big_id, small_ids in enumerate(big_of_small):
        big_members = [m for s in small_ids for m in small_clusters[s]]
        big_label = _label(rows, big_members)
        for s in small_ids:
            members = small_clusters[s]
            small_label = _label(rows, members)
            for m in members:
                updates.append(
                    {
                        "id": rows[m]["id"],
                        "cluster_small": s,
                        "cluster_small_label": small_label,
                        "cluster_big": big_id,
                        "cluster_big_label": big_label,
                        "px": float(coords[m, 0]),
                        "py": float(coords[m, 1]),
                        "pz": float(coords[m, 2]),
                    }
                )
    db.apply_grouping(updates)

    return {
        "embedded": embedded_count,
        "total": len(rows),
        "clusters_small": len(small_clusters),
        "clusters_big": len(big_of_small),
    }
