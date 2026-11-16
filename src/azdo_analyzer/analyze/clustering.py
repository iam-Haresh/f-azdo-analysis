"""Pattern clustering (spec section 13.5): one GitLab template per cluster of similar pipelines."""

from __future__ import annotations

from collections import defaultdict

from ..config.models import Config
from ..model.entities import BuildPipeline, Cluster, Estate, ReleasePipeline, pipeline_steps


def fingerprint(pipeline: BuildPipeline | ReleasePipeline, ignore: set[str]) -> frozenset[str]:
    """Normalised task set: enabled steps, task groups expanded, glue tasks removed."""
    return frozenset(step.task_name for step in pipeline_steps(pipeline) if step.task_name and step.task_name.lower() not in ignore)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def cluster_estate(estate: Estate, config: Config) -> None:
    estate.clusters = []
    for pipeline in estate.pipelines():
        pipeline.cluster_id = ""
    if not config.clustering.enabled:
        return
    ignore = {name.lower() for name in config.clustering.ignore_tasks}
    threshold = config.clustering.similarity_threshold

    by_stack: dict[tuple[str, str], dict[frozenset[str], list[str]]] = defaultdict(lambda: defaultdict(list))
    pipelines = {p.id: p for p in estate.pipelines()}
    for pipeline in sorted(pipelines.values(), key=lambda p: p.id):
        if isinstance(pipeline, BuildPipeline) and pipeline.kind == "xaml":
            continue
        kind = "release" if isinstance(pipeline, ReleasePipeline) else "build"
        by_stack[(kind, pipeline.stack_signature)][fingerprint(pipeline, ignore)].append(pipeline.id)

    clusters: list[Cluster] = []
    for (kind, stack), groups in sorted(by_stack.items()):
        prints = sorted(groups, key=lambda fp: (-len(groups[fp]), sorted(fp)))
        parent = list(range(len(prints)))

        def find(i: int, parent: list[int] = parent) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(prints)):
            for j in range(i + 1, len(prints)):
                if prints[i] and prints[j] and jaccard(prints[i], prints[j]) >= threshold:
                    root_i, root_j = find(i), find(j)
                    if root_i != root_j:
                        parent[max(root_i, root_j)] = min(root_i, root_j)
        merged: dict[int, list[frozenset[str]]] = defaultdict(list)
        for i, fp in enumerate(prints):
            merged[find(i)].append(fp)
        for root in sorted(merged):
            member_prints = merged[root]
            members = sorted(m for fp in member_prints for m in groups[fp])
            if len(members) < config.clustering.min_cluster_size or not any(member_prints):
                continue
            common = frozenset.intersection(*member_prints)
            # Representative: first member of the most common task set.
            representative = sorted(groups[member_prints[0]])[0]
            deviations = {member: sorted(fp - common) for fp in member_prints for member in groups[fp] if fp - common}
            clusters.append(
                Cluster(
                    id="",
                    stack=f"{kind}: {stack}",
                    members=members,
                    representative=representative,
                    common_tasks=sorted(common),
                    deviations=dict(sorted(deviations.items())),
                )
            )
    clusters.sort(key=lambda c: (-len(c.members), c.stack, c.representative))
    for number, cluster in enumerate(clusters, start=1):
        cluster.id = f"C{number:03d}"
        for member in cluster.members:
            pipelines[member].cluster_id = cluster.id
    estate.clusters = clusters
