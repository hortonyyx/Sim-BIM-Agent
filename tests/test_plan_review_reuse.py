"""An observed drawing survives a wall-attribute edit, never a geometry edit."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from src.agent.runtime_roles.reader_delivery import require_observed_plan_trial
from src.agent.runtime_roles.trial import PlanTrial
from tests.test_plan_reading_delivery import setup


def journal(before, after, *, observed=True):
    def execution(sequence, receipt):
        return SimpleNamespace(sequence=sequence, payload=SimpleNamespace(
            event_type="tool_execution", tool_name="trial_plan_bim", outcome="succeeded",
            raw_result={"structuredContent": receipt}))
    events = [execution(1, before)]
    if observed:
        events.append(SimpleNamespace(sequence=2, payload=SimpleNamespace(
            event_type="adapter_request", images=[SimpleNamespace(sent=SimpleNamespace(sha256=row["sha256"]))
                                                  for row in before["returned_images"]])))
    events.append(execution(3, after))
    return SimpleNamespace(events=events, resolve=lambda value: value)


def two_trials(tmp_path, *, edit="metadata"):
    plan, trial = setup(tmp_path)
    before = asyncio.run(trial.run(plan))
    changed = copy.deepcopy(plan)
    changed["wall_metadata"][0]["basis"] = "Revised construction explanation"
    if edit == "geometry":
        # Subpixel input change can quantize to the exact same adopted drawing.
        changed["openings"][0]["p1"][1] += .001
    elif edit == "evidence":
        changed["unresolved"] = ["A new issue has not been reviewed."]
    after = asyncio.run(trial.run(changed))
    assert before["status"] == after["status"] == "passed"
    assert [row["sha256"] for row in before["returned_images"]] == [
        row["sha256"] for row in after["returned_images"]]
    return trial, before, after


def test_observed_wall_metadata_edit_reuses_proof_after_reload(tmp_path):
    trial, before, after = two_trials(tmp_path)
    restored = PlanTrial(None, image_name="plan.png", workspace=tmp_path,
                         receipt_directory=tmp_path / "trial_receipts", grid_step_m=.1)
    proof = require_observed_plan_trial(journal(before, after), restored, after)
    assert proof["observed_plan_sha256"] == before["plan_sha256"]
    assert proof["plan_sha256"] == after["plan_sha256"]
    assert trial.numeric_plan(after)["wall_metadata"][0]["basis"] == "Revised construction explanation"


@pytest.mark.parametrize("edit", ["metadata", "geometry", "evidence"])
def test_identical_images_do_not_replace_missing_or_stale_observation(tmp_path, edit):
    trial, before, after = two_trials(tmp_path, edit=edit)
    with pytest.raises(ValueError, match="have not reached a model request"):
        require_observed_plan_trial(journal(before, after, observed=edit != "metadata"), trial, after)


@pytest.mark.parametrize("target", ["old_input", "old_product", "old_view", "new_numeric", "new_view", "image"])
def test_reuse_rejects_tampered_proof_files(tmp_path, target):
    trial, before, after = two_trials(tmp_path)
    relative = {"old_input": before["input_plan_file"], "old_product": before["reading_product_file"],
                "old_view": before["returned_images"][0]["file"],
                "new_numeric": after["compiled_numeric_plan_file"],
                "new_view": after["returned_images"][0]["file"], "image": "images/plan.png"}[target]
    path = tmp_path / relative
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        require_observed_plan_trial(journal(before, after), trial, after)
