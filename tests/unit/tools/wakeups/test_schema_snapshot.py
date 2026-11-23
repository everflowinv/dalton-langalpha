"""The schema ``schedule_wakeup`` shows the model is prompt surface: every
field name, description and limit in it is read at call time. It is stored
as a snapshot so a change is a decision, not a side effect.

To change it on purpose, regenerate ``schema_snapshot.json`` from the two
expressions below and review the diff like a prompt edit.
"""

import json
from pathlib import Path

from langchain_core.utils.function_calling import convert_to_openai_tool

from src.tools.wakeups.models import ScheduleWakeupArgs
from src.tools.wakeups.tool import build_schedule_wakeup_tool
from src.tools.wakeups.wording import (  # DALTON-HOOK(hide-trading)
    SCHEDULE_WAKEUP_DOC_NO_TRADING,
    TRIGGER_MODEL_DOC_NO_TRADING,
)

SNAPSHOT = json.loads((Path(__file__).parent / "schema_snapshot.json").read_text())


def test_the_args_model_schema_is_the_snapshot():
    assert ScheduleWakeupArgs.model_json_schema() == SNAPSHOT["ScheduleWakeupArgs"]


def test_the_tool_as_the_model_sees_it_is_the_snapshot():
    # name, description and parameters, after the tool node's injected
    # arguments (state, tool_call_id) are left out.
    # DALTON-HOOK(hide-trading): upstream snapshot holds the full trading variant;
    # default build_schedule_wakeup_tool() hides order / watch_elsewhere below.
    seen_full = convert_to_openai_tool(build_schedule_wakeup_tool(include_orders=True))
    assert seen_full == SNAPSHOT["as_the_model_sees_it"]
    assert set(seen_full["function"]["parameters"]["properties"]) == {
        "triggers",
        "cancel",
        "quiet",
    }

    seen = convert_to_openai_tool(build_schedule_wakeup_tool())
    assert seen["function"]["name"] == "schedule_wakeup"
    assert seen["function"]["description"] == SCHEDULE_WAKEUP_DOC_NO_TRADING
    assert "order" not in seen["function"]["description"].lower()
    trigger_item = seen["function"]["parameters"]["properties"]["triggers"]["anyOf"][0]["items"]
    assert trigger_item["description"] == TRIGGER_MODEL_DOC_NO_TRADING
    assert "order" not in trigger_item["properties"]
    assert "brokerage" not in json.dumps(seen).lower()
    assert build_schedule_wakeup_tool(watch_elsewhere=True).watch_elsewhere is False
