# 임베딩 생성 + 클러스터링 + 3D 투영 — 대시보드 그룹 뷰 재계산

import os
from collections import Counter

import httpx
import numpy as np

import db

EMBED_MODEL = "text-embedding-3-small"
T_SMALL = 0.62  # 소분류 병합 임계 (cosine)
T_BIG = 0.45  # 대분류 병합 임계


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


def _embed_input(row: dict) -> str:
    parts = [row["title"]]
    if row["tags"]:
        parts.append("태그: " + ", ".join(row["tags"]))
    parts.append(row["summary"] or row["snippet"] or "")
    return "\n".join(p for p in parts if p)[:6000]


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

    stale = db.rows_needing_embedding()
    if stale:
        vectors = _embed_texts([_embed_input(row) for row in stale])
        db.set_embeddings([(row["id"], vec) for row, vec in zip(stale, vectors)])

    rows = db.all_embedded()
    if not rows:
        return {"embedded": 0, "total": 0, "clusters_small": 0, "clusters_big": 0}

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
        "embedded": len(stale),
        "total": len(rows),
        "clusters_small": len(small_clusters),
        "clusters_big": len(big_of_small),
    }
