"""Which models a device type needs, and which models a training job produced.

A force plate ships with a force model and a moment model, each its own job
(`Job.model_type` 'force' | 'moment'). A dev kit (type 18) ships with a force,
a COP and a continuous-field model, and one set-recipe job trains all three
(`DEVKIT_RECIPE` 'set_xf_v1', #1213/#1214): the job is recorded as
`model_type` 'force', so `model_type` alone does not say what a job produced
(work item #1296). `job_outputs` reads the job's config for that.

Pure: no DB, no bucket. axio-server's lifecycle stage, the dev-kit calibration
record and the metrics export all answer "is this unit's model set complete"
from these two functions, so they agree.
"""
from __future__ import annotations

import json
from typing import FrozenSet, Optional, Tuple, Union

from .devkit import DEVKIT_TYPE_ID

OUTPUT_FORCE = "force"
OUTPUT_MOMENT = "moment"
OUTPUT_COP = "cop"
OUTPUT_FIELD = "field"

#: A plate's model set; also the answer for any type not listed below.
DEFAULT_REQUIRED_OUTPUTS: Tuple[str, ...] = (OUTPUT_FORCE, OUTPUT_MOMENT)

#: Device types whose model set differs from a plate's, in display order.
REQUIRED_OUTPUTS_BY_TYPE = {
    DEVKIT_TYPE_ID: (OUTPUT_FORCE, OUTPUT_COP, OUTPUT_FIELD),
}

#: What each dev-kit recipe trains. Mirrors the Neuralizer's RECIPES
#: (experiments/devkit/small_models/devkit_job.py, `parts()`). A recipe name is
#: never edited once a model has shipped with it, so a new recipe is a new line
#: here; until it is added, a job that ran it produced unknown outputs.
DEVKIT_RECIPE_OUTPUTS = {
    "force_xf_v1": (OUTPUT_FORCE,),
    "cop_xf_v1": (OUTPUT_COP,),
    "field_xf_v1": (OUTPUT_FIELD,),
    "set_xf_v1": (OUTPUT_FORCE, OUTPUT_COP, OUTPUT_FIELD),
}


def required_outputs(type_id: Optional[str]) -> Tuple[str, ...]:
    """The models a unit of this device type needs before it can be tested."""
    return REQUIRED_OUTPUTS_BY_TYPE.get((type_id or "").lower(), DEFAULT_REQUIRED_OUTPUTS)


def job_recipe(config: Union[str, dict, None]) -> Optional[str]:
    """The job config's DEVKIT_RECIPE, else None. Accepts `Job.config` as
    stored (a JSON string) or already parsed; unreadable config is None."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return None
    if not isinstance(config, dict):
        return None
    name = config.get("DEVKIT_RECIPE")
    return str(name) if name else None


def job_outputs(model_type: Optional[str], config: Union[str, dict, None] = None
                ) -> Optional[FrozenSet[str]]:
    """The models a job produces: its recipe's outputs when the config names a
    DEVKIT_RECIPE, else its `model_type`. None when the recipe is not in
    DEVKIT_RECIPE_OUTPUTS: unknown, never guessed."""
    recipe = job_recipe(config)
    if recipe is not None:
        outputs = DEVKIT_RECIPE_OUTPUTS.get(recipe)
        return frozenset(outputs) if outputs is not None else None
    return frozenset({model_type}) if model_type else frozenset()

