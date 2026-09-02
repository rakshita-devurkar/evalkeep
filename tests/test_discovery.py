"""Discovery end to end: storage, idempotency and cluster editing (guide 8F)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalsmith.analysis import Component, FailureType, Severity
from evalsmith.cli import app
from evalsmith.clusters import MemberRole
from evalsmith.commands.analyze_cmd import label_failure
from evalsmith.commands.detect_cmd import review_failure, run_detection
from evalsmith.commands.discover_cmd import (
    dismiss_cluster,
    list_clusters,
    merge_clusters,
    rename_cluster,
    restore_cluster,
    run_discovery,
    show_cluster,
    split_cluster,
)
from evalsmith.commands.ingest_cmd import ingest_traces
from evalsmith.config import Project
from evalsmith.errors import CommandError, ExitCode
from evalsmith.failures import FailureStatus, failure_id_for
from evalsmith.storage import TraceStore

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/refund-agent/traces.jsonl"

#: Two traces in one family, one clearly apart -- the shape the example dataset
#: was built to produce.
LABELS: dict[str, tuple[FailureType, Component, Severity, str]] = {
    "trace-1042": (
        FailureType.WRONG_TOOL_ARGUMENT,
        Component.TOOL_ARGUMENTS,
        Severity.HIGH,
        "Refunded the oldest order instead of the newest order.",
    ),
    "trace-1043": (
        FailureType.WRONG_TOOL_ARGUMENT,
        Component.TOOL_ARGUMENTS,
        Severity.CRITICAL,
        "Refunded an older order instead of the newest order.",
    ),
    "trace-1051": (
        FailureType.UNNECESSARY_ACTION,
        Component.PLANNING,
        Severity.CRITICAL,
        "Refunded every order on the account when the user asked for one.",
    ),
}


@pytest.fixture
def labelled(initialized_project: Path) -> Path:
    """The refund example, ingested, detected and labelled by hand."""
    ingest_traces(EXAMPLE, project_root=initialized_project)
    run_detection(project_root=initialized_project)
    for trace_id, (kind, component, severity, summary) in LABELS.items():
        label_failure(
            trace_id,
            failure_type=kind,
            component=component,
            severity=severity,
            summary=summary,
            project_root=initialized_project,
            labeler="alex",
        )
    return initialized_project


@pytest.fixture
def discovered(labelled: Path) -> Path:
    run_discovery(project_root=labelled)
    return labelled


def cluster_for(project: Path, trace_id: str) -> Any:
    return show_cluster(trace_id, project_root=project).cluster


class TestDiscovery:
    def test_groups_the_family_and_leaves_the_outlier_alone(self, labelled: Path) -> None:
        report = run_discovery(project_root=labelled)
        assert report.considered == 3
        assert report.clusters == 2
        assert report.largest == 2
        assert report.singletons == 1

    def test_stores_the_clustering(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        assert len(clusters) == 2
        assert sum(cluster.size for cluster in clusters) == 3

    def test_stores_the_parameters_that_produced_it(self, discovered: Path) -> None:
        with TraceStore.open(Project.load(discovered).database_path) as store:
            run = store.clusters.current_run()
        assert run is not None
        assert run.embedder == "hashing:512:0"
        assert run.dimensions == 512
        assert run.parameters["linkage"] == "average"
        assert run.parameters["threshold"] == 0.55
        assert run.parameters["seed"] == 0

    def test_selects_representatives(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1042")
        roles = {role for member in cluster.members for role in member.roles}
        assert MemberRole.CENTRAL in roles
        assert MemberRole.BOUNDARY in roles
        assert MemberRole.HIGH_SEVERITY in roles

    def test_the_worst_member_is_marked(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1042")
        worst = next(m for m in cluster.members if MemberRole.HIGH_SEVERITY in m.roles)
        assert worst.failure_id == failure_id_for("trace-1043")  # the critical one

    def test_dismissed_failures_are_not_grouped(self, labelled: Path) -> None:
        review_failure("trace-1051", FailureStatus.DISMISSED, project_root=labelled)
        report = run_discovery(project_root=labelled)
        assert report.considered == 2
        assert report.clusters == 1

    def test_unanalyzed_failures_are_reported_not_guessed_at(
        self, initialized_project: Path
    ) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        run_detection(project_root=initialized_project)
        label_failure(
            "trace-1042",
            failure_type=FailureType.WRONG_TOOL_ARGUMENT,
            component=Component.TOOL_ARGUMENTS,
            severity=Severity.HIGH,
            summary="Refunded the oldest order.",
            project_root=initialized_project,
        )
        report = run_discovery(project_root=initialized_project)
        assert report.unanalyzed == 2
        assert report.clusters == 1

    def test_nothing_analyzed_is_a_command_error(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        run_detection(project_root=initialized_project)
        with pytest.raises(CommandError, match="No analyzed failures"):
            run_discovery(project_root=initialized_project)

    def test_no_failures_is_a_command_error(self, initialized_project: Path) -> None:
        ingest_traces(EXAMPLE, project_root=initialized_project)
        with pytest.raises(CommandError, match="No failure candidates"):
            run_discovery(project_root=initialized_project)


class TestIdempotence:
    def test_a_second_pass_produces_the_same_clusters(self, discovered: Path) -> None:
        before = [c.cluster_id for c in list_clusters(project_root=discovered)]
        run_discovery(project_root=discovered)
        assert [c.cluster_id for c in list_clusters(project_root=discovered)] == before

    def test_the_second_pass_reuses_cached_vectors(self, discovered: Path) -> None:
        report = run_discovery(project_root=discovered)
        assert report.embedded == 0
        assert report.from_cache == 3

    def test_disabling_the_cache_re_embeds(self, discovered: Path) -> None:
        report = run_discovery(project_root=discovered, use_cache=False)
        assert report.embedded == 3


class TestEditsSurviveReclustering:
    def test_a_rename_is_kept(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1042")
        rename_cluster(cluster.cluster_id, "Refunds the wrong order", project_root=discovered)
        report = run_discovery(project_root=discovered)
        assert report.kept_labels >= 1
        assert cluster_for(discovered, "trace-1042").label == "Refunds the wrong order"

    def test_a_dismissal_is_kept(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1051")
        dismiss_cluster(cluster.cluster_id, project_root=discovered)
        run_discovery(project_root=discovered)
        assert cluster_for(discovered, "trace-1051").dismissed

    def test_reclustering_refuses_to_discard_edits(self, discovered: Path) -> None:
        """A merge changes membership, so the merged cluster cannot come back."""
        clusters = list_clusters(project_root=discovered)
        merge_clusters([clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered)
        with pytest.raises(CommandError, match="would discard reviewer edits"):
            run_discovery(project_root=discovered)

    def test_force_discards_them_explicitly(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        merge_clusters([clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered)
        report = run_discovery(project_root=discovered, force=True)
        assert report.discarded_edits == 1
        assert report.clusters == 2

    def test_an_unedited_clustering_is_replaced_without_complaint(self, discovered: Path) -> None:
        assert run_discovery(project_root=discovered).clusters == 2


class TestEditing:
    def test_rename_records_who_did_it(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1042")
        renamed = rename_cluster(
            cluster.cluster_id, "Wrong order refunded", project_root=discovered, reviewer="sam"
        )
        assert renamed.label == "Wrong order refunded"
        assert renamed.labelled_by == "sam"

    def test_an_empty_label_is_refused(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1042")
        with pytest.raises(CommandError, match="cannot be empty"):
            rename_cluster(cluster.cluster_id, "   ", project_root=discovered)

    def test_dismiss_keeps_the_cluster(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1051")
        dismiss_cluster(cluster.cluster_id, project_root=discovered)
        assert len(list_clusters(project_root=discovered)) == 2
        assert len(list_clusters(project_root=discovered, include_dismissed=False)) == 1

    def test_restore_undoes_a_dismissal(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1051")
        dismiss_cluster(cluster.cluster_id, project_root=discovered)
        restore_cluster(cluster.cluster_id, project_root=discovered)
        assert not cluster_for(discovered, "trace-1051").dismissed

    def test_merge_combines_membership(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        merged = merge_clusters(
            [clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered
        )
        assert merged.size == 3
        assert len(list_clusters(project_root=discovered)) == 1

    def test_merge_reselects_representatives(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        merged = merge_clusters(
            [clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered
        )
        roles = {role for member in merged.members for role in member.roles}
        assert roles == {
            MemberRole.CENTRAL,
            MemberRole.BOUNDARY,
            MemberRole.HIGH_SEVERITY,
        }

    def test_merge_keeps_a_reviewer_label(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        rename_cluster(clusters[0].cluster_id, "Wrong order", project_root=discovered)
        merged = merge_clusters(
            [clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered
        )
        assert merged.label == "Wrong order"

    def test_merge_needs_two_clusters(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        with pytest.raises(CommandError, match="at least two"):
            merge_clusters([clusters[0].cluster_id], project_root=discovered)

    def test_merge_refuses_a_repeated_cluster(self, discovered: Path) -> None:
        cluster = list_clusters(project_root=discovered)[0]
        with pytest.raises(CommandError, match="only be merged once"):
            merge_clusters([cluster.cluster_id, cluster.cluster_id], project_root=discovered)

    def test_split_moves_members_into_a_new_cluster(self, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        merged = merge_clusters(
            [clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered
        )
        remainder, extracted = split_cluster(
            merged.cluster_id, [failure_id_for("trace-1051")], project_root=discovered
        )
        assert extracted.size == 1
        assert remainder.size == 2
        assert len(list_clusters(project_root=discovered)) == 2

    def test_a_merge_then_split_round_trips_to_the_original_ids(self, discovered: Path) -> None:
        """Cluster identity is membership, so undoing an edit restores the IDs."""
        before = {c.cluster_id for c in list_clusters(project_root=discovered)}
        clusters = list_clusters(project_root=discovered)
        merged = merge_clusters(
            [clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered
        )
        split_cluster(merged.cluster_id, [failure_id_for("trace-1051")], project_root=discovered)
        assert {c.cluster_id for c in list_clusters(project_root=discovered)} == before

    def test_split_refuses_an_unknown_member(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1042")
        with pytest.raises(CommandError, match="Not in cluster"):
            split_cluster(cluster.cluster_id, ["fail-nope"], project_root=discovered)

    def test_split_refuses_to_empty_a_cluster(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1051")
        with pytest.raises(CommandError, match="every member"):
            split_cluster(cluster.cluster_id, cluster.failure_ids, project_root=discovered)

    def test_split_needs_something_to_move(self, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1042")
        with pytest.raises(CommandError, match="at least one failure"):
            split_cluster(cluster.cluster_id, [], project_root=discovered)

    def test_clusters_resolve_by_failure_or_trace_id(self, discovered: Path) -> None:
        by_trace = show_cluster("trace-1042", project_root=discovered).cluster
        by_failure = show_cluster(failure_id_for("trace-1042"), project_root=discovered).cluster
        by_id = show_cluster(by_trace.cluster_id, project_root=discovered).cluster
        assert by_trace.cluster_id == by_failure.cluster_id == by_id.cluster_id

    def test_an_unknown_identifier_is_a_command_error(self, discovered: Path) -> None:
        with pytest.raises(CommandError, match="No cluster matching"):
            show_cluster("nope", project_root=discovered)


class TestCli:
    def test_discover_reports_the_grouping(self, runner: CliRunner, labelled: Path) -> None:
        result = runner.invoke(app, ["discover", "-C", str(labelled)])
        assert result.exit_code == ExitCode.OK
        assert "clusters" in result.stdout
        assert "hashing:512:0" in result.stdout

    def test_discover_prints_the_parameters(self, runner: CliRunner, labelled: Path) -> None:
        result = runner.invoke(app, ["discover", "-C", str(labelled)])
        assert "threshold=0.55" in result.stdout
        assert "linkage=average" in result.stdout

    def test_clusters_list(self, runner: CliRunner, discovered: Path) -> None:
        result = runner.invoke(app, ["clusters", "list", "-C", str(discovered)])
        assert result.exit_code == ExitCode.OK
        assert "wrong_tool_argument" in result.stdout

    def test_clusters_list_when_empty(self, runner: CliRunner, labelled: Path) -> None:
        result = runner.invoke(app, ["clusters", "list", "-C", str(labelled)])
        assert "No clusters" in result.stdout

    def test_clusters_show_lists_members_and_roles(
        self, runner: CliRunner, discovered: Path
    ) -> None:
        result = runner.invoke(app, ["clusters", "show", "trace-1042", "-C", str(discovered)])
        assert result.exit_code == ExitCode.OK
        assert "central" in result.stdout
        assert "trace-1043" in result.stdout

    def test_rename_dismiss_and_restore(self, runner: CliRunner, discovered: Path) -> None:
        cluster = cluster_for(discovered, "trace-1051")
        renamed = runner.invoke(
            app, ["clusters", "rename", cluster.cluster_id, "Over-refunds", "-C", str(discovered)]
        )
        dismissed = runner.invoke(
            app, ["clusters", "dismiss", cluster.cluster_id, "-C", str(discovered)]
        )
        restored = runner.invoke(
            app, ["clusters", "restore", cluster.cluster_id, "-C", str(discovered)]
        )
        assert "renamed" in renamed.stdout
        assert "dismissed" in dismissed.stdout
        assert "restored" in restored.stdout

    def test_merge_and_split_from_the_cli(self, runner: CliRunner, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        merge = runner.invoke(
            app,
            [
                "clusters",
                "merge",
                clusters[0].cluster_id,
                clusters[1].cluster_id,
                "-C",
                str(discovered),
            ],
        )
        assert merge.exit_code == ExitCode.OK
        merged = list_clusters(project_root=discovered)[0]
        split = runner.invoke(
            app,
            [
                "clusters",
                "split",
                merged.cluster_id,
                "--failure",
                failure_id_for("trace-1051"),
                "-C",
                str(discovered),
            ],
        )
        assert split.exit_code == ExitCode.OK
        assert len(list_clusters(project_root=discovered)) == 2

    def test_reclustering_over_edits_exits_two(self, runner: CliRunner, discovered: Path) -> None:
        clusters = list_clusters(project_root=discovered)
        merge_clusters([clusters[0].cluster_id, clusters[1].cluster_id], project_root=discovered)
        result = runner.invoke(app, ["discover", "-C", str(discovered), "--skip-analysis"])
        assert result.exit_code == ExitCode.COMMAND_ERROR
