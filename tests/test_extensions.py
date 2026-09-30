"""Step extensions: plan building, cleanup order and prompt notes."""

import sys
import types

import pytest

import opendot.extensions as ext_module
from opendot.extensions import (
    ExtensionError,
    StepContext,
    StepExtensions,
    load_step_extensions,
    with_prompt_notes,
)
from opendot.models import GatewayCallStatus, GatewayMode, Step, StepPlan


class Recorder:
    def __init__(self, name, calls, fail_before=False, fail_after=False):
        self.name = name
        self.calls = calls
        self.fail_before = fail_before
        self.fail_after = fail_after

    def before_step(self, ctx, plan):
        self.calls.append(("before", self.name))
        if self.fail_before:
            raise ExtensionError("cannot prepare")
        plan.prompt_notes.append(f"note from {self.name}")

    def after_step(self, ctx, plan, attempt):
        self.calls.append(("after", self.name, attempt.id if attempt else None))
        if self.fail_after:
            raise RuntimeError("cleanup broke")


def _ctx(store, config, step=Step.WORK):
    task = store.create_task(text="t", requester="alice", channel="cli", conversation="local")
    return StepContext(task=task, step=step, store=store, config=config, backend_kind="fake")


def test_no_extensions_means_no_plan(store, config):
    assert StepExtensions([]).begin(_ctx(store, config)) is None


def test_only_work_steps_get_a_plan(store, config):
    calls = []
    exts = StepExtensions([Recorder("a", calls)])
    assert exts.begin(_ctx(store, config, Step.REVIEW)) is None
    assert calls == []


def test_begin_and_finish_order(store, config):
    calls = []
    exts = StepExtensions([Recorder("a", calls), Recorder("b", calls, fail_after=True)])
    ctx = _ctx(store, config)
    plan = exts.begin(ctx)
    assert isinstance(plan, StepPlan)
    assert plan.run_dir == config.runs_dir / f"task-{ctx.task.id}" / plan.step_token
    assert plan.run_dir.is_dir()
    assert plan.run_dir.stat().st_mode & 0o777 == 0o700
    assert plan.prompt_notes == ["note from a", "note from b"]

    store.log_gateway_call(
        ctx.task.id,
        plan.step_token,
        server="factiq",
        tool="search_series",
        mode=GatewayMode.READ,
        arguments={},
        status=GatewayCallStatus.OK,
    )
    attempt = store.start_attempt(ctx.task.id, Step.WORK, "fake", run_path=plan.run_dir)
    exts.finish(ctx, plan, attempt)
    assert calls == [
        ("before", "a"),
        ("before", "b"),
        ("after", "b", attempt.id),
        ("after", "a", attempt.id),
    ]
    assert store.list_gateway_calls(task_id=ctx.task.id)[0].attempt_id == attempt.id
    assert [e.kind for e in store.list_events(task_id=ctx.task.id)] == ["extension.cleanup_failed"]


def test_failed_before_step_cleans_up_started_ones(store, config):
    calls = []
    exts = StepExtensions(
        [Recorder("a", calls), Recorder("b", calls, fail_before=True), Recorder("c", calls)]
    )
    with pytest.raises(ExtensionError):
        exts.begin(_ctx(store, config))
    assert calls == [("before", "a"), ("before", "b"), ("after", "a", None)]


def test_prompt_notes():
    assert with_prompt_notes("do it", None) == "do it"
    plan = StepPlan(step_token="x", run_dir=None, prompt_notes=["one", " two "])
    text = with_prompt_notes("do it\n", plan)
    assert text == "do it\n\n## Tools and folders for this step\n\none\n\ntwo\n"


def test_loader_skips_missing_and_keeps_order(config, monkeypatch):
    module = types.ModuleType("opendot_test_ext")
    module.step_extensions = lambda cfg: [Recorder("x", [])]
    monkeypatch.setitem(sys.modules, "opendot_test_ext", module)
    monkeypatch.setattr(
        ext_module, "FEATURE_STEP_MODULES", ("opendot_missing_ext", "opendot_test_ext")
    )
    assert [e.name for e in load_step_extensions(config)] == ["x"]


def test_default_loader_runs_before_features_exist(config):
    assert isinstance(load_step_extensions(config), list)


def test_feature_cli_and_doctor_hooks(config, monkeypatch):
    import argparse

    module = types.ModuleType("opendot_test_cli")
    module.register_cli = lambda sub: sub.add_parser("widgets")
    module.doctor_checks = lambda cfg: [("widgets", True, "ok")]
    bare = types.ModuleType("opendot_test_bare")
    monkeypatch.setitem(sys.modules, "opendot_test_cli", module)
    monkeypatch.setitem(sys.modules, "opendot_test_bare", bare)
    monkeypatch.setattr(
        ext_module,
        "FEATURE_CLI_MODULES",
        ("opendot_missing_cli", "opendot_test_bare", "opendot_test_cli"),
    )
    parser = argparse.ArgumentParser()
    ext_module.register_feature_cli(parser.add_subparsers(dest="cmd"))
    assert parser.parse_args(["widgets"]).cmd == "widgets"
    assert ext_module.doctor_checks(config) == [("widgets", True, "ok")]


def test_default_cli_hooks_run_before_features_exist(config):
    import argparse

    ext_module.register_feature_cli(argparse.ArgumentParser().add_subparsers())
    assert isinstance(ext_module.doctor_checks(config), list)
