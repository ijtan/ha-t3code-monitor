"""Guard the translated Connect pairing placeholders."""

import ast
import json
from pathlib import Path


def test_pairing_progress_responses_supply_environment_placeholders() -> None:
    integration_path = Path(__file__).parents[1] / "custom_components" / "t3code"
    source = (integration_path / "config_flow.py").read_text()
    module = ast.parse(source)
    pairing_step = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_step_connect_pairing"
    )
    progress_calls = [
        node
        for node in ast.walk(pairing_step)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_show_progress"
    ]

    assert len(progress_calls) == 2
    for call in progress_calls:
        placeholders = next(
            keyword.value
            for keyword in call.keywords
            if keyword.arg == "description_placeholders"
        )
        assert isinstance(placeholders, ast.Call)
        assert isinstance(placeholders.func, ast.Name)
        assert placeholders.func.id == "_pairing_description_placeholders"

    strings = json.loads((integration_path / "strings.json").read_text())
    pairing_title = strings["config"]["step"]["connect_pairing"]["title"]
    assert "{" not in pairing_title
