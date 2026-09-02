"""Targets: reachable agents, described without their credentials (guide 8I)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evalkeep.commands.target_cmd import add_target, list_targets, remove_target, show_target
from evalkeep.errors import CommandError
from evalkeep.targets import (
    Target,
    TargetKind,
    find_secrets,
    get_target,
    load_targets,
    referenced_environment,
    save_targets,
)


def http_target(**overrides: object) -> Target:
    payload: dict[str, object] = {
        "target_id": "candidate",
        "kind": TargetKind.HTTP,
        "url": "https://agent.example.com/chat",
        "body": {"message": "{{input}}"},
    }
    payload.update(overrides)
    return Target(**payload)  # type: ignore[arg-type]


class TestSecretEnforcement:
    def test_a_clean_target_has_no_secrets(self) -> None:
        assert find_secrets(http_target()) == []

    def test_an_env_reference_is_allowed(self) -> None:
        target = http_target(headers={"Authorization": "${AGENT_TOKEN}"})
        assert find_secrets(target) == []

    def test_a_literal_token_is_caught(self) -> None:
        target = http_target(headers={"Authorization": "Bearer sk-live0123456789abcdefghijklmn"})
        problems = find_secrets(target)
        assert problems and "looks like a credential" in problems[0]

    def test_a_literal_under_a_credential_key_is_caught(self) -> None:
        target = http_target(headers={"api_key": "not-obviously-a-token"})
        problems = find_secrets(target)
        assert problems and "ENV_VAR" in problems[0]

    def test_secrets_nested_in_the_body_are_caught(self) -> None:
        target = http_target(body={"message": "{{input}}", "auth": {"password": "hunter2"}})
        assert find_secrets(target)

    def test_adding_a_target_with_a_secret_is_refused(self, initialized_project: Path) -> None:
        with pytest.raises(CommandError, match="looks like a credential"):
            add_target(
                "candidate",
                TargetKind.HTTP,
                project_root=initialized_project,
                url="https://agent.example.com/chat",
                body={"message": "{{input}}"},
                headers={"Authorization": "Bearer sk-live0123456789abcdefghijklmn"},
            )

    def test_nothing_is_written_when_a_secret_is_refused(self, initialized_project: Path) -> None:
        with pytest.raises(CommandError):
            add_target(
                "candidate",
                TargetKind.HTTP,
                project_root=initialized_project,
                url="https://x.example.com",
                body={"m": "{{input}}"},
                headers={"api_key": "literal-secret-value"},
            )
        assert not (initialized_project / "targets.yaml").exists()


class TestEnvironment:
    def test_referenced_variables_are_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_TOKEN", "x")
        monkeypatch.delenv("OTHER_TOKEN", raising=False)
        target = http_target(
            headers={"Authorization": "${AGENT_TOKEN}", "X-Extra": "${OTHER_TOKEN}"}
        )
        assert referenced_environment(target) == {"AGENT_TOKEN": True, "OTHER_TOKEN": False}

    def test_a_target_without_references_needs_nothing(self) -> None:
        assert referenced_environment(http_target()) == {}


class TestShapeValidation:
    def test_http_needs_a_url(self) -> None:
        target = Target(target_id="t", kind=TargetKind.HTTP, body={"m": "x"})
        with pytest.raises(CommandError, match="needs a url"):
            target.validate_shape()

    def test_http_needs_a_body(self) -> None:
        target = Target(target_id="t", kind=TargetKind.HTTP, url="https://x")
        with pytest.raises(CommandError, match="needs a body"):
            target.validate_shape()

    def test_a_script_target_needs_a_path(self) -> None:
        target = Target(target_id="t", kind=TargetKind.PYTHON)
        with pytest.raises(CommandError, match="needs a path"):
            target.validate_shape()

    def test_a_model_target_needs_a_provider(self) -> None:
        target = Target(target_id="t", kind=TargetKind.MODEL)
        with pytest.raises(CommandError, match="needs a provider"):
            target.validate_shape()

    def test_a_complete_target_validates(self) -> None:
        http_target().validate_shape()


class TestPersistence:
    def test_targets_round_trip(self, tmp_path: Path) -> None:
        from evalkeep.targets import TargetFile

        original = TargetFile(targets={"candidate": http_target()})
        save_targets(tmp_path, original)
        loaded = load_targets(tmp_path)
        assert loaded.targets["candidate"].url == "https://agent.example.com/chat"
        assert loaded.targets["candidate"].target_id == "candidate"

    def test_a_missing_file_is_an_empty_set(self, tmp_path: Path) -> None:
        assert load_targets(tmp_path).targets == {}

    def test_malformed_yaml_is_a_command_error(self, tmp_path: Path) -> None:
        (tmp_path / "targets.yaml").write_text("targets: [unclosed\n")
        with pytest.raises(CommandError, match="Could not parse"):
            load_targets(tmp_path)

    def test_an_invalid_target_is_a_command_error(self, tmp_path: Path) -> None:
        (tmp_path / "targets.yaml").write_text("targets:\n  a:\n    kind: telepathy\n")
        with pytest.raises(CommandError, match="Invalid target configuration"):
            load_targets(tmp_path)

    def test_an_unknown_target_lists_the_known_ones(self, tmp_path: Path) -> None:
        from evalkeep.targets import TargetFile

        save_targets(tmp_path, TargetFile(targets={"candidate": http_target()}))
        with pytest.raises(CommandError) as raised:
            get_target(tmp_path, "baseline")
        assert raised.value.hint is not None
        assert "candidate" in raised.value.hint


class TestCommands:
    def test_adding_and_listing(self, initialized_project: Path) -> None:
        add_target(
            "baseline",
            TargetKind.PYTHON,
            project_root=initialized_project,
            path="agents/baseline.py",
            function="call_api",
        )
        targets = list_targets(project_root=initialized_project)
        assert [t.target_id for t in targets] == ["baseline"]

    def test_a_duplicate_needs_replace(self, initialized_project: Path) -> None:
        add_target("baseline", TargetKind.PYTHON, project_root=initialized_project, path="a.py")
        with pytest.raises(CommandError, match="already exists"):
            add_target("baseline", TargetKind.PYTHON, project_root=initialized_project, path="b.py")
        replaced = add_target(
            "baseline",
            TargetKind.PYTHON,
            project_root=initialized_project,
            path="b.py",
            replace=True,
        )
        assert replaced.path == "b.py"

    def test_showing_reports_the_environment(
        self, initialized_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENT_TOKEN", raising=False)
        add_target(
            "candidate",
            TargetKind.HTTP,
            project_root=initialized_project,
            url="https://x.example.com",
            body={"m": "{{input}}"},
            headers={"Authorization": "${AGENT_TOKEN}"},
        )
        _, environment = show_target("candidate", project_root=initialized_project)
        assert environment == {"AGENT_TOKEN": False}

    def test_removing(self, initialized_project: Path) -> None:
        add_target("baseline", TargetKind.PYTHON, project_root=initialized_project, path="a.py")
        remove_target("baseline", project_root=initialized_project)
        assert list_targets(project_root=initialized_project) == []

    def test_removing_something_absent_is_a_command_error(self, initialized_project: Path) -> None:
        with pytest.raises(CommandError, match="No target named"):
            remove_target("nope", project_root=initialized_project)

    def test_an_unnamed_target_is_refused(self, initialized_project: Path) -> None:
        with pytest.raises(CommandError, match="needs a name"):
            add_target("  ", TargetKind.PYTHON, project_root=initialized_project, path="a.py")
