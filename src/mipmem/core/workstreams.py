"""Cross-host workstream auto-threading and utility micro-session compaction."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from mipmem.core.diagnostics import RotatingJsonLogger
from mipmem.core.salience import TIER_PRIMARY_WORK, SalienceResult, score_session_salience
from mipmem.core.sessions import SessionRecord
from mipmem.core.vault import atomic_write_vault_file
from mipmem.i18n import extract_anchor_tokens, get_lexicon

DEFAULT_FILE_OVERLAP_THRESHOLD = 0.20
DEFAULT_TITLE_OVERLAP_THRESHOLD = 0.34
COMPACTED_CACHE_REL_PATH = ".cache/merged_compacted_memory.md"


@dataclass(frozen=True)
class WorkstreamCluster:
    """A cross-host workstream grouping related coding or brainstorm sessions."""

    workstream_id: str
    label: str
    hosts: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    primary_session_ids: list[str] = field(default_factory=list)
    compacted_micro_ids: list[str] = field(default_factory=list)
    shared_files: list[str] = field(default_factory=list)
    latest_modified_time: str = ""
    top_salience_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize workstream cluster into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class WorkstreamResult:
    """Output of cross-host workstream threading and local cache compaction."""

    clusters: list[WorkstreamCluster]
    session_to_workstream: dict[str, str]
    compacted_markdown: str
    output_path: Optional[Path] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize workstream result into a JSON-compatible dictionary."""
        return {
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "session_to_workstream": dict(self.session_to_workstream),
            "compacted_markdown": self.compacted_markdown,
            "output_path": str(self.output_path) if self.output_path is not None else None,
        }


def _normalize_file_paths(record: SessionRecord) -> set[str]:
    """Return normalized POSIX-style file paths referenced by a session."""
    normalized: set[str] = set()
    for raw in (*record.edited_files, *record.touched_files):
        cleaned = raw.strip().replace("\\", "/").lstrip("./").lower()
        if cleaned:
            normalized.add(cleaned)
    return normalized


def _jaccard(left: set[str], right: set[str]) -> float:
    """Compute Jaccard similarity between two token/path sets."""
    if not left or not right:
        return 0.0
    union_len = len(left | right)
    if union_len == 0:
        return 0.0
    return len(left & right) / union_len


def _sessions_are_related(
    files_a: set[str],
    files_b: set[str],
    title_a: set[str],
    title_b: set[str],
    *,
    file_threshold: float,
    title_threshold: float,
) -> bool:
    """Decide whether two sessions belong to the same cross-host workstream."""
    if files_a and files_b and _jaccard(files_a, files_b) >= file_threshold:
        return True
    if title_a and title_b:
        shared_title = title_a & title_b
        union_title = title_a | title_b
        min_shared = 2 if len(union_title) > 2 else 1
        if len(shared_title) >= min_shared and _jaccard(title_a, title_b) >= title_threshold:
            return True
    return False


def _render_compacted_markdown(
    clusters: Sequence[WorkstreamCluster],
    records_by_id: dict[str, SessionRecord],
    salience_by_id: dict[str, SalienceResult],
) -> str:
    """Render deterministic local compacted memory digest across workstreams."""
    lines: list[str] = [
        "# Merged Compacted Workstreams",
        "",
        f"> Workstreams: {len(clusters)} | Sessions: {len(records_by_id)}",
        "",
    ]

    for cluster in clusters:
        host_list = ", ".join(cluster.hosts)
        lines.append(
            f"## [{cluster.workstream_id}] {cluster.label} "
            f"(hosts: {host_list}; top_score: {cluster.top_salience_score:.1f})"
        )
        if cluster.shared_files:
            lines.append(f"- Shared files: {', '.join(cluster.shared_files[:6])}")

        for cid in cluster.primary_session_ids:
            rec = records_by_id[cid]
            sal = salience_by_id[cid]
            ts = rec.last_modified_time or "unknown-time"
            lines.append(
                f"- PRIMARY `{cid}` ({rec.host_id}/{rec.surface}, {ts}, "
                f"score={sal.score:.1f}): {rec.title}"
            )

        if cluster.compacted_micro_ids:
            micro_list = ", ".join(f"`{cid}`" for cid in cluster.compacted_micro_ids[:8])
            extra = (
                f" (+{len(cluster.compacted_micro_ids) - 8} more)"
                if len(cluster.compacted_micro_ids) > 8
                else ""
            )
            lines.append(
                f"- Compacted {len(cluster.compacted_micro_ids)} utility micro-session(s): "
                f"{micro_list}{extra}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# @para-doc [#csa-workstreams-auto-thread]
def thread_workstreams(
    sessions: Sequence[SessionRecord],
    *,
    locale: str = "en",
    file_overlap_threshold: float = DEFAULT_FILE_OVERLAP_THRESHOLD,
    title_overlap_threshold: float = DEFAULT_TITLE_OVERLAP_THRESHOLD,
    vault_root: Optional[Union[str, Path]] = None,
    home_dir: Optional[Path] = None,
    logger: Optional[RotatingJsonLogger] = None,
) -> WorkstreamResult:
    """Group cross-host sessions into deterministic workstreams and compact micro-sessions."""
    lexicon = get_lexicon(locale)

    ordered = sorted(
        sessions,
        key=lambda rec: (rec.last_modified_time, rec.conversation_id),
        reverse=True,
    )
    count = len(ordered)
    if count == 0:
        empty_md = "# Merged Compacted Workstreams\n\n> Workstreams: 0 | Sessions: 0\n"
        out_path: Optional[Path] = None
        if vault_root is not None:
            out_path = atomic_write_vault_file(
                vault_root,
                COMPACTED_CACHE_REL_PATH,
                empty_md,
                home_dir=home_dir,
                logger=logger,
            )
        return WorkstreamResult(
            clusters=[],
            session_to_workstream={},
            compacted_markdown=empty_md,
            output_path=out_path,
        )

    records_by_id: dict[str, SessionRecord] = {}
    salience_by_id: dict[str, SalienceResult] = {}
    file_sets: list[set[str]] = []
    title_sets: list[set[str]] = []

    for rec in ordered:
        records_by_id[rec.conversation_id] = rec
        salience_by_id[rec.conversation_id] = score_session_salience(rec, locale=locale)
        file_sets.append(_normalize_file_paths(rec))
        title_sets.append(set(extract_anchor_tokens(rec.title, lexicon)))

    parent = list(range(count))

    def _find(idx: int) -> int:
        while parent[idx] != idx:
            parent[idx] = parent[parent[idx]]
            idx = parent[idx]
        return idx

    def _union(left: int, right: int) -> None:
        root_l = _find(left)
        root_r = _find(right)
        if root_l != root_r:
            if root_l < root_r:
                parent[root_r] = root_l
            else:
                parent[root_l] = root_r

    for idx_a in range(count):
        for idx_b in range(idx_a + 1, count):
            if _sessions_are_related(
                file_sets[idx_a],
                file_sets[idx_b],
                title_sets[idx_a],
                title_sets[idx_b],
                file_threshold=file_overlap_threshold,
                title_threshold=title_overlap_threshold,
            ):
                _union(idx_a, idx_b)

    groups: dict[int, list[SessionRecord]] = {}
    for idx, rec in enumerate(ordered):
        root = _find(idx)
        groups.setdefault(root, []).append(rec)

    clusters: list[WorkstreamCluster] = []
    session_to_workstream: dict[str, str] = {}

    for group_records in groups.values():
        group_sorted = sorted(
            group_records,
            key=lambda rec: (rec.last_modified_time, rec.conversation_id),
            reverse=True,
        )
        canonical_ids = sorted(rec.conversation_id for rec in group_sorted)
        digest = hashlib.sha256("|".join(canonical_ids).encode("utf-8")).hexdigest()[:8]
        workstream_id = f"ws-{digest}"

        hosts = sorted({rec.host_id for rec in group_sorted if rec.host_id})
        session_ids = [rec.conversation_id for rec in group_sorted]
        primary_ids: list[str] = []
        micro_ids: list[str] = []

        best_record = group_sorted[0]
        best_score = -1.0
        for rec in group_sorted:
            sal = salience_by_id[rec.conversation_id]
            if sal.tier == TIER_PRIMARY_WORK:
                primary_ids.append(rec.conversation_id)
            else:
                micro_ids.append(rec.conversation_id)
            if sal.score > best_score:
                best_score = sal.score
                best_record = rec
            session_to_workstream[rec.conversation_id] = workstream_id

        file_counter: Counter[str] = Counter()
        for rec in group_sorted:
            for path in _normalize_file_paths(rec):
                file_counter[path] += 1
        min_occurrences = 2 if len(group_sorted) > 1 else 1
        shared_files = sorted(
            path for path, freq in file_counter.items() if freq >= min_occurrences
        )

        latest_ts = max((rec.last_modified_time for rec in group_sorted), default="")
        clusters.append(
            WorkstreamCluster(
                workstream_id=workstream_id,
                label=best_record.title or best_record.conversation_id,
                hosts=hosts,
                session_ids=session_ids,
                primary_session_ids=primary_ids,
                compacted_micro_ids=micro_ids,
                shared_files=shared_files,
                latest_modified_time=latest_ts,
                top_salience_score=round(max(0.0, best_score), 2),
            )
        )

    clusters.sort(
        key=lambda cl: (cl.top_salience_score, cl.latest_modified_time, cl.workstream_id),
        reverse=True,
    )

    compacted_markdown = _render_compacted_markdown(clusters, records_by_id, salience_by_id)
    output_path: Optional[Path] = None
    if vault_root is not None:
        output_path = atomic_write_vault_file(
            vault_root,
            COMPACTED_CACHE_REL_PATH,
            compacted_markdown,
            home_dir=home_dir,
            logger=logger,
        )

    return WorkstreamResult(
        clusters=clusters,
        session_to_workstream=session_to_workstream,
        compacted_markdown=compacted_markdown,
        output_path=output_path,
    )
