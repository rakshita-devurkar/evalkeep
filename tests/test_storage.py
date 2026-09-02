"""Storage: migrations, transactions, event order and duplicate safety (guide 8C)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evalkeep.errors import CommandError
from evalkeep.hashing import canonical_content, content_hash
from evalkeep.redaction import RedactionRule, RedactionSummary
from evalkeep.storage import LATEST_VERSION, MIGRATIONS, StoreResult, TraceStore, apply_migrations
from evalkeep.trace import NormalizedTrace


def make_trace(trace_id: str = "trace-1", **overrides: Any) -> NormalizedTrace:
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "input": {"text": "Refund my latest order."},
        "outcome": {"status": "failure"},
    }
    payload.update(overrides)
    return NormalizedTrace.model_validate(payload)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore.open(tmp_path / "db" / "database.db") as opened:
        yield opened


class TestMigrations:
    def test_a_new_database_reaches_the_latest_version(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        applied = apply_migrations(connection)
        assert [m.version for m in applied] == [m.version for m in MIGRATIONS]
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        assert row[0] == LATEST_VERSION

    def test_applying_twice_is_a_no_op(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        assert apply_migrations(connection) == []

    def test_creates_the_documented_tables(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"traces", "events", "schema_migrations"} <= names

    def test_a_newer_database_is_refused(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?)",
            (LATEST_VERSION + 5, "from the future", "2030-01-01T00:00:00Z"),
        )
        connection.commit()
        with pytest.raises(CommandError, match="only understands"):
            apply_migrations(connection)

    def test_a_failing_migration_leaves_no_partial_state(self, tmp_path: Path) -> None:
        """The version row and the schema change commit together, or not at all."""
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        connection.execute("CREATE TABLE traces (blocking TEXT)")
        connection.commit()
        with pytest.raises(sqlite3.Error):
            apply_migrations(connection)
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        assert row[0] is None


class TestForeignKeys:
    def test_events_are_removed_with_their_trace(self, store: TraceStore) -> None:
        store.add(_trace_with_events())
        assert store.event_count() == 2
        store._connection.execute("DELETE FROM traces WHERE trace_id = ?", ("trace-1",))
        store._connection.commit()
        assert store.event_count() == 0

    def test_an_orphan_event_is_rejected(self, store: TraceStore) -> None:
        with pytest.raises(sqlite3.IntegrityError), store._connection:
            store._connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("no-such-trace", 0, "e1", "message", None, None, None, "{}"),
            )


class TestStoring:
    def test_a_new_trace_is_stored(self, store: TraceStore) -> None:
        outcome = store.add(make_trace())
        assert outcome.result is StoreResult.STORED
        assert outcome.written
        assert store.count() == 1

    def test_the_stored_trace_round_trips(self, store: TraceStore) -> None:
        original = _trace_with_events()
        store.add(original)
        stored = store.get("trace-1")
        assert stored is not None
        assert stored.trace == original

    def test_events_come_back_in_recorded_order(self, store: TraceStore) -> None:
        store.add(_trace_with_events())
        rows = store._connection.execute(
            "SELECT position, event_id FROM events WHERE trace_id = ? ORDER BY position",
            ("trace-1",),
        ).fetchall()
        assert [row["event_id"] for row in rows] == ["e1", "e2"]
        stored = store.get("trace-1")
        assert stored is not None
        assert [event.event_id for event in stored.trace.events] == ["e1", "e2"]

    def test_tool_events_are_indexed_for_detection(self, store: TraceStore) -> None:
        store.add(_trace_with_events())
        rows = store._connection.execute(
            "SELECT trace_id, type FROM events WHERE tool = ? ORDER BY position",
            ("refund_order",),
        ).fetchall()
        assert [row["type"] for row in rows] == ["tool_call", "tool_result"]
        assert {row["trace_id"] for row in rows} == {"trace-1"}

    def test_the_redaction_summary_is_kept_with_the_trace(self, store: TraceStore) -> None:
        summary = RedactionSummary()
        summary.record(RedactionRule.EMAIL, 2)
        store.add(make_trace(), redaction=summary)
        stored = store.get("trace-1")
        assert stored is not None
        assert stored.redactions == 2
        assert stored.redaction_summary == {"email": 2}

    def test_an_unknown_trace_id_reads_as_none(self, store: TraceStore) -> None:
        assert store.get("nope") is None


class TestNeverOverwrite:
    def test_the_same_trace_twice_is_a_no_op(self, store: TraceStore) -> None:
        store.add(make_trace())
        outcome = store.add(make_trace())
        assert outcome.result is StoreResult.ALREADY_STORED
        assert store.count() == 1

    def test_the_same_id_with_different_content_is_refused(self, store: TraceStore) -> None:
        store.add(make_trace())
        outcome = store.add(make_trace(input={"text": "Something else entirely."}))
        assert outcome.result is StoreResult.ID_CONFLICT
        assert not outcome.written

    def test_a_refused_conflict_leaves_the_stored_trace_intact(self, store: TraceStore) -> None:
        store.add(make_trace())
        store.add(make_trace(input={"text": "Something else entirely."}))
        stored = store.get("trace-1")
        assert stored is not None
        assert stored.trace.input.text == "Refund my latest order."

    def test_the_same_content_under_a_new_id_is_reported(self, store: TraceStore) -> None:
        store.add(make_trace("trace-1"))
        outcome = store.add(make_trace("trace-2"))
        assert outcome.result is StoreResult.CONTENT_DUPLICATE
        assert outcome.existing_trace_id == "trace-1"
        assert store.count() == 1

    def test_classify_writes_nothing(self, store: TraceStore) -> None:
        assert store.classify(make_trace()).result is StoreResult.STORED
        assert store.count() == 0


class TestListing:
    def test_lists_what_was_stored(self, store: TraceStore) -> None:
        store.add(_trace_with_events())
        store.add(make_trace("trace-2", input={"text": "Where is my order?"}))
        summaries = store.list()
        assert [s.trace_id for s in summaries] == ["trace-1", "trace-2"]
        assert summaries[0].events == 2

    def test_filters_by_status(self, store: TraceStore) -> None:
        store.add(make_trace("trace-1"))
        store.add(make_trace("trace-2", input={"text": "ok"}, outcome={"status": "success"}))
        assert [s.trace_id for s in store.list(status="success")] == ["trace-2"]
        assert store.count(status="failure") == 1

    def test_paginates(self, store: TraceStore) -> None:
        for index in range(5):
            store.add(make_trace(f"trace-{index}", input={"text": f"question {index}"}))
        page = store.list(limit=2, offset=2)
        assert len(page) == 2
        assert store.count() == 5

    def test_an_empty_store_lists_nothing(self, store: TraceStore) -> None:
        assert store.list() == []


class TestOpening:
    def test_creates_the_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "deeply" / "nested" / "database.db"
        with TraceStore.open(path):
            pass
        assert path.is_file()

    def test_an_unusable_path_is_a_command_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        with (
            pytest.raises(CommandError, match="Could not open the database"),
            TraceStore.open(blocker / "database.db"),
        ):
            pass


class TestContentHash:
    def test_is_stable_across_calls(self) -> None:
        assert content_hash(make_trace()) == content_hash(make_trace())

    def test_ignores_the_trace_id(self) -> None:
        assert content_hash(make_trace("trace-1")) == content_hash(make_trace("trace-2"))

    def test_ignores_metadata_and_timestamps(self) -> None:
        bare = _trace_with_events()
        annotated = _trace_with_events(
            metadata={"source": "elsewhere", "recorded_at": "2027-01-01T00:00:00Z"}
        )
        assert content_hash(bare) == content_hash(annotated)

    def test_ignores_event_and_call_identifiers(self) -> None:
        renamed = _trace_with_events()
        payload = renamed.model_dump(mode="json")
        payload["events"][0]["event_id"] = "renamed"
        payload["events"][0]["call_id"] = "renamed-call"
        payload["events"][1]["call_id"] = "renamed-call"
        assert content_hash(NormalizedTrace.model_validate(payload)) == content_hash(renamed)

    def test_notices_a_different_question(self) -> None:
        assert content_hash(make_trace()) != content_hash(
            make_trace(input={"text": "Something else."})
        )

    def test_notices_a_different_tool_argument(self) -> None:
        one = _trace_with_events()
        payload = one.model_dump(mode="json")
        payload["events"][0]["arguments"] = {"order_id": "order-Z"}
        assert content_hash(NormalizedTrace.model_validate(payload)) != content_hash(one)

    def test_notices_a_different_outcome(self) -> None:
        assert content_hash(make_trace()) != content_hash(make_trace(outcome={"status": "success"}))

    def test_notices_reordered_events(self) -> None:
        one = _trace_with_events()
        payload = one.model_dump(mode="json")
        payload["events"] = [
            {"event_id": "e1", "type": "message", "role": "user", "content": "a"},
            {"event_id": "e2", "type": "message", "role": "assistant", "content": "b"},
        ]
        reordered = dict(payload)
        reordered["events"] = list(reversed(payload["events"]))
        assert content_hash(NormalizedTrace.model_validate(payload)) != content_hash(
            NormalizedTrace.model_validate(reordered)
        )

    def test_is_algorithm_labelled(self) -> None:
        assert content_hash(make_trace()).startswith("sha256:")

    def test_the_canonical_form_excludes_identity(self) -> None:
        canonical = canonical_content(_trace_with_events())
        assert set(canonical) == {"input", "output", "outcome", "events"}
        assert all("event_id" not in event for event in canonical["events"])


def _trace_with_events(**overrides: Any) -> NormalizedTrace:
    return make_trace(
        events=[
            {
                "event_id": "e1",
                "type": "tool_call",
                "tool": "refund_order",
                "call_id": "c1",
                "arguments": {"order_id": "order-A"},
                "timestamp": "2026-08-14T09:12:06Z",
            },
            {
                "event_id": "e2",
                "type": "tool_result",
                "tool": "refund_order",
                "call_id": "c1",
                "result": {"status": "refunded"},
                "timestamp": "2026-08-14T09:12:07Z",
            },
        ],
        **overrides,
    )


class TestFailureStorage:
    def _failure(self, trace_id: str = "trace-1") -> Any:
        from evalkeep.detectors import Signal, SignalKind
        from evalkeep.failures import Failure

        return Failure.from_signals(
            trace_id,
            [
                Signal(
                    detector="explicit_status",
                    kind=SignalKind.EXPLICIT_STATUS,
                    source="outcome.status",
                    summary="marked failed",
                    evidence={"status": "failure"},
                )
            ],
        )

    def test_migration_two_creates_the_failure_tables(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"failures", "failure_signals"} <= names

    def test_a_failure_round_trips_with_its_signals(self, store: TraceStore) -> None:
        store.add(make_trace())
        store.failures.save(self._failure())
        loaded = store.failures.get_by_trace("trace-1")
        assert loaded is not None
        assert loaded.signals[0].evidence == {"status": "failure"}
        assert loaded.signals[0].source == "outcome.status"

    def test_saving_twice_replaces_signals_rather_than_appending(self, store: TraceStore) -> None:
        store.add(make_trace())
        failure = self._failure()
        store.failures.save(failure)
        store.failures.save(failure)
        loaded = store.failures.get(failure.failure_id)
        assert loaded is not None
        assert len(loaded.signals) == 1

    def test_one_failure_per_trace_is_enforced_by_the_schema(self, store: TraceStore) -> None:
        from evalkeep.failures import Failure

        store.add(make_trace())
        store.failures.save(self._failure())
        second = Failure.from_signals("trace-1", [])
        second.failure_id = "fail-different"
        with pytest.raises(sqlite3.IntegrityError):
            store.failures.save(second)

    def test_a_failure_cannot_outlive_its_trace(self, store: TraceStore) -> None:
        store.add(make_trace())
        store.failures.save(self._failure())
        store._connection.execute("DELETE FROM traces WHERE trace_id = ?", ("trace-1",))
        store._connection.commit()
        assert store.failures.count() == 0
        assert store._connection.execute("SELECT COUNT(*) FROM failure_signals").fetchone()[0] == 0

    def test_a_failure_needs_a_stored_trace(self, store: TraceStore) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.failures.save(self._failure("no-such-trace"))

    def test_deleting_a_failure_removes_its_signals(self, store: TraceStore) -> None:
        store.add(make_trace())
        failure = self._failure()
        store.failures.save(failure)
        store.failures.delete(failure.failure_id)
        assert store.failures.get(failure.failure_id) is None
        assert store._connection.execute("SELECT COUNT(*) FROM failure_signals").fetchone()[0] == 0

    def test_iterating_traces_streams_them_in_order(self, store: TraceStore) -> None:
        for index in range(3):
            store.add(make_trace(f"trace-{index}", input={"text": f"question {index}"}))
        assert [t.trace_id for t in store.iter_traces()] == ["trace-0", "trace-1", "trace-2"]


class TestClusterStorage:
    def _cluster(self, failure_ids: list[str]) -> Any:
        from evalkeep.clusters import Cluster, ClusterMember, MemberRole

        members = [
            ClusterMember(failure_id=fid, distance=0.1 * index)
            for index, fid in enumerate(failure_ids)
        ]
        members[0].roles.append(MemberRole.CENTRAL)
        return Cluster.build(label="wrong_tool_argument in tool_arguments", members=members)

    def _run(self) -> Any:
        from evalkeep.clusters import ClusteringRun

        return ClusteringRun(
            run_id="run-1",
            embedder="hashing:512:0",
            dimensions=512,
            parameters={"threshold": 0.55, "seed": 0},
            failures=2,
        )

    def _seed(self, store: TraceStore, count: int = 2) -> list[str]:
        from evalkeep.detectors import Signal, SignalKind
        from evalkeep.failures import Failure

        failure_ids: list[str] = []
        for index in range(count):
            trace_id = f"trace-{index}"
            store.add(make_trace(trace_id, input={"text": f"question {index}"}))
            failure = Failure.from_signals(
                trace_id,
                [
                    Signal(
                        detector="explicit_status",
                        kind=SignalKind.EXPLICIT_STATUS,
                        source="outcome.status",
                        summary="marked failed",
                    )
                ],
            )
            store.failures.save(failure)
            failure_ids.append(failure.failure_id)
        return failure_ids

    def test_migration_four_creates_the_cluster_tables(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"clustering_runs", "clusters", "cluster_members"} <= names

    def test_a_run_round_trips_with_its_parameters(self, store: TraceStore) -> None:
        failure_ids = self._seed(store)
        store.clusters.replace_run(self._run(), [self._cluster(failure_ids)])
        run = store.clusters.current_run()
        assert run is not None
        assert run.embedder == "hashing:512:0"
        assert run.parameters == {"threshold": 0.55, "seed": 0}

    def test_members_and_roles_round_trip(self, store: TraceStore) -> None:
        from evalkeep.clusters import MemberRole

        failure_ids = self._seed(store)
        cluster = self._cluster(failure_ids)
        store.clusters.replace_run(self._run(), [cluster])
        loaded = store.clusters.get(cluster.cluster_id)
        assert loaded is not None
        assert loaded.failure_ids == failure_ids
        assert MemberRole.CENTRAL in loaded.members[0].roles

    def test_a_new_run_replaces_the_previous_clustering(self, store: TraceStore) -> None:
        failure_ids = self._seed(store)
        first = self._cluster(failure_ids)
        store.clusters.replace_run(self._run(), [first])
        second = self._cluster(failure_ids[:1])
        store.clusters.replace_run(self._run(), [second])
        assert store.clusters.count() == 1
        assert store.clusters.get(first.cluster_id) is None

    def test_a_cluster_cannot_outlive_its_failures(self, store: TraceStore) -> None:
        failure_ids = self._seed(store)
        store.clusters.replace_run(self._run(), [self._cluster(failure_ids)])
        store._connection.execute("DELETE FROM traces")
        store._connection.commit()
        assert store._connection.execute("SELECT COUNT(*) FROM cluster_members").fetchone()[0] == 0

    def test_a_member_must_reference_a_real_failure(self, store: TraceStore) -> None:
        self._seed(store, count=1)
        with pytest.raises(sqlite3.IntegrityError):
            store.clusters.replace_run(self._run(), [self._cluster(["fail-nope"])])

    def test_clusters_are_found_by_member(self, store: TraceStore) -> None:
        failure_ids = self._seed(store)
        cluster = self._cluster(failure_ids)
        store.clusters.replace_run(self._run(), [cluster])
        found = store.clusters.find_by_failure(failure_ids[1])
        assert found is not None and found.cluster_id == cluster.cluster_id

    def test_an_empty_store_has_no_run(self, store: TraceStore) -> None:
        assert store.clusters.current_run() is None
        assert store.clusters.list() == []


class TestRegressionTestStorage:
    def _test(self, failure_id: str) -> Any:
        from evalkeep.regression import (
            CaseInput,
            Expectation,
            ExpectationType,
            Fixture,
            Provenance,
            RegressionTest,
        )

        return RegressionTest(
            test_id="refund_my_latest_order_abc12345",
            failure_id=failure_id,
            input=CaseInput(text="Refund my latest order."),
            fixtures=[Fixture(tool="refund_order", arguments={"order_id": "order-A"})],
            expectations=[
                Expectation(
                    type=ExpectationType.TOOL_ARGUMENT_NOT_EQUALS,
                    tool="refund_order",
                    path="order_id",
                    value="order-A",
                )
            ],
            warnings=["needs a positive expectation"],
            provenance=Provenance(
                trace_id="trace-1",
                failure_id=failure_id,
                content_hash="sha256:abc",
                cluster_id="cl-gone",
            ),
        )

    def _seed(self, store: TraceStore) -> str:
        from evalkeep.failures import Failure

        store.add(make_trace("trace-1"))
        failure = Failure.from_signals("trace-1", [])
        store.failures.save(failure)
        return failure.failure_id

    def test_migration_five_creates_the_table(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "regression_tests" in names

    def test_a_test_round_trips(self, store: TraceStore) -> None:
        from evalkeep.regression import ExpectationType

        failure_id = self._seed(store)
        store.tests.save(self._test(failure_id))
        loaded = store.tests.get_by_failure(failure_id)
        assert loaded is not None
        assert loaded.input.text == "Refund my latest order."
        assert loaded.expectations[0].type is ExpectationType.TOOL_ARGUMENT_NOT_EQUALS
        assert loaded.fixtures[0].arguments == {"order_id": "order-A"}
        assert loaded.warnings == ["needs a positive expectation"]
        assert loaded.provenance.content_hash == "sha256:abc"

    def test_one_test_per_failure_is_enforced(self, store: TraceStore) -> None:
        failure_id = self._seed(store)
        store.tests.save(self._test(failure_id))
        second = self._test(failure_id)
        second.test_id = "another_id_00000000"
        with pytest.raises(sqlite3.IntegrityError):
            store.tests.save(second)

    def test_saving_twice_updates_in_place(self, store: TraceStore) -> None:
        from evalkeep.regression import ReviewStatus

        failure_id = self._seed(store)
        test = self._test(failure_id)
        store.tests.save(test)
        test.status = ReviewStatus.APPROVED
        store.tests.save(test)
        loaded = store.tests.get(test.test_id)
        assert loaded is not None and loaded.status is ReviewStatus.APPROVED
        assert store.tests.count() == 1

    def test_a_test_cannot_outlive_its_failure(self, store: TraceStore) -> None:
        failure_id = self._seed(store)
        store.tests.save(self._test(failure_id))
        store._connection.execute("DELETE FROM traces")
        store._connection.commit()
        assert store.tests.count() == 0

    def test_a_test_survives_the_cluster_that_suggested_it(self, store: TraceStore) -> None:
        """cluster_id is a plain column: clusters are rebuilt, tests are not."""
        failure_id = self._seed(store)
        store.tests.save(self._test(failure_id))
        loaded = store.tests.get_by_failure(failure_id)
        assert loaded is not None
        assert loaded.provenance.cluster_id == "cl-gone"
        assert store.clusters.count() == 0

    def test_filtering_by_status(self, store: TraceStore) -> None:
        from evalkeep.regression import ReviewStatus

        failure_id = self._seed(store)
        store.tests.save(self._test(failure_id))
        assert store.tests.count(status=ReviewStatus.DRAFT) == 1
        assert store.tests.count(status=ReviewStatus.APPROVED) == 0
        assert store.tests.counts_by_status() == {ReviewStatus.DRAFT: 1}

    def test_listing_and_iterating(self, store: TraceStore) -> None:
        failure_id = self._seed(store)
        store.tests.save(self._test(failure_id))
        assert [t.failure_id for t in store.tests.list()] == [failure_id]
        assert [t.failure_id for t in store.tests.iter_all()] == [failure_id]

    def test_an_unknown_test_reads_as_none(self, store: TraceStore) -> None:
        assert store.tests.get("nope") is None
        assert store.tests.get_by_failure("nope") is None

    def test_deleting_a_test(self, store: TraceStore) -> None:
        failure_id = self._seed(store)
        test = self._test(failure_id)
        store.tests.save(test)
        store.tests.delete(test.test_id)
        assert store.tests.get(test.test_id) is None


class TestRunStorage:
    def _run_and_results(self) -> Any:
        from evalkeep.runs import CaseResult, ErrorKind, EvaluationRun, Outcome

        run = EvaluationRun(
            run_id="run-1",
            target_id="baseline",
            suite_hash="sha256:abc",
            tests=3,
            runner="promptfoo:3",
            environment={"python": "3.11.16"},
        )
        results = [
            CaseResult(test_id="t1", outcome=Outcome.PASS, latency_ms=10),
            CaseResult(
                test_id="t2",
                outcome=Outcome.FAIL,
                failed_assertions=["refunded order-A"],
                observation='{"text": "..."}',
            ),
            CaseResult(
                test_id="t3",
                outcome=Outcome.ERROR,
                error_kind=ErrorKind.TIMEOUT,
                error="timed out",
            ),
        ]
        return run, results

    def test_migration_seven_creates_the_run_tables(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(tmp_path / "db.sqlite")
        apply_migrations(connection)
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"evaluation_runs", "test_results"} <= names

    def test_a_run_round_trips_with_its_results(self, store: TraceStore) -> None:
        from evalkeep.runs import ErrorKind, Outcome

        run, results = self._run_and_results()
        store.runs.save(run, results)

        loaded = store.runs.get("run-1")
        assert loaded is not None
        assert loaded.suite_hash == "sha256:abc"
        assert loaded.runner == "promptfoo:3"
        assert loaded.environment == {"python": "3.11.16"}

        stored = store.runs.results("run-1")
        assert [r.outcome for r in stored] == [Outcome.PASS, Outcome.FAIL, Outcome.ERROR]
        assert stored[1].failed_assertions == ["refunded order-A"]
        assert stored[2].error_kind is ErrorKind.TIMEOUT

    def test_counts_separate_errors_from_failures(self, store: TraceStore) -> None:
        from evalkeep.runs import Outcome

        run, results = self._run_and_results()
        store.runs.save(run, results)
        assert store.runs.counts("run-1") == {
            Outcome.PASS: 1,
            Outcome.FAIL: 1,
            Outcome.ERROR: 1,
        }

    def test_saving_twice_replaces_the_results(self, store: TraceStore) -> None:
        run, results = self._run_and_results()
        store.runs.save(run, results)
        store.runs.save(run, results[:1])
        assert len(store.runs.results("run-1")) == 1

    def test_the_latest_run_for_a_target(self, store: TraceStore) -> None:
        from datetime import UTC, datetime, timedelta

        run, results = self._run_and_results()
        store.runs.save(run, results)
        later = self._run_and_results()[0]
        later.run_id = "run-2"
        later.started_at = datetime.now(UTC) + timedelta(seconds=5)
        store.runs.save(later, [])
        latest = store.runs.latest("baseline")
        assert latest is not None and latest.run_id == "run-2"

    def test_results_die_with_their_run(self, store: TraceStore) -> None:
        run, results = self._run_and_results()
        store.runs.save(run, results)
        store._connection.execute("DELETE FROM evaluation_runs WHERE run_id = ?", ("run-1",))
        store._connection.commit()
        assert store.runs.results("run-1") == []

    def test_listing_recent_runs(self, store: TraceStore) -> None:
        run, results = self._run_and_results()
        store.runs.save(run, results)
        assert [r.run_id for r in store.runs.recent()] == ["run-1"]

    def test_an_unknown_run_reads_as_none(self, store: TraceStore) -> None:
        assert store.runs.get("nope") is None
        assert store.runs.latest("nope") is None
