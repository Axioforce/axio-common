"""Required model outputs per device type, and what a job produced (#1296)."""
import json

from axio_common.models import job_outputs, job_recipe, required_outputs


def test_plates_need_force_and_moment():
    assert required_outputs("07") == ("force", "moment")
    assert required_outputs(None) == ("force", "moment")


def test_dev_kit_needs_force_cop_field():
    assert required_outputs("18") == ("force", "cop", "field")


def test_set_recipe_job_produces_all_three():
    # As stored: Job.config is a JSON string, and a set job's model_type is 'force'.
    cfg = json.dumps({"DEVICE_ID": "18-c2c4a1ed", "DEVKIT_RECIPE": "set_xf_v1"})
    assert job_outputs("force", cfg) == {"force", "cop", "field"}


def test_single_model_recipes():
    assert job_outputs("force", {"DEVKIT_RECIPE": "force_xf_v1"}) == {"force"}
    assert job_outputs("force", {"DEVKIT_RECIPE": "cop_xf_v1"}) == {"cop"}
    assert job_outputs("force", {"DEVKIT_RECIPE": "field_xf_v1"}) == {"field"}


def test_no_recipe_falls_back_to_model_type():
    assert job_outputs("moment", json.dumps({"OUTPUT_TYPE": "moment vector"})) == {"moment"}
    assert job_outputs("force", None) == {"force"}
    assert job_outputs("force", "not json") == {"force"}


def test_unknown_recipe_is_unknown_not_force():
    assert job_outputs("force", {"DEVKIT_RECIPE": "set_xf_v9"}) is None
    assert job_recipe({"DEVKIT_RECIPE": "set_xf_v9"}) == "set_xf_v9"
