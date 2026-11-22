"""The runner contract: the schemas in contracts/, their examples, and the
orchestrator's models in factory.contract must all agree."""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from referencing import Registry, Resource

from factory import contract

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
EXAMPLES = CONTRACTS / "examples"


def schema(name):
    return json.loads((CONTRACTS / f"{name}.schema.json").read_text())


def examples(kind, validity):
    return sorted((EXAMPLES / kind).glob(f"*.{validity}*.json"))


def load(path):
    return json.loads(path.read_text())


def registry():
    # The schemas $ref each other by file name, relative to their $id.
    resources = []
    for path in CONTRACTS.glob("*.schema.json"):
        doc = json.loads(path.read_text())
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def schema_accepts(name, data):
    validator = jsonschema.Draft202012Validator(schema(name), registry=registry())
    return not list(validator.iter_errors(data))


def ids(paths):
    return [p.name for p in paths]


TASKS = ["explore", "implement", "review", "auto-tickets", "smoke"]


@pytest.mark.parametrize("kind", ["dispatch", "result"])
@pytest.mark.parametrize("task", TASKS)
def test_every_task_has_a_valid_and_an_invalid_example(kind, task):
    for validity in ("valid", "invalid"):
        assert list((EXAMPLES / kind).glob(f"{task}.{validity}*.json")), (
            f"no {validity} {kind} example for {task}"
        )


def test_contract_names_every_task():
    assert list(contract.TASKS) == TASKS


DISPATCH_VALID = examples("dispatch", "valid")
DISPATCH_INVALID = examples("dispatch", "invalid")


@pytest.mark.parametrize("path", DISPATCH_VALID, ids=ids(DISPATCH_VALID))
def test_valid_dispatch_passes_schema_and_model(path):
    data = load(path)
    assert schema_accepts("dispatch", data)
    assert contract.parse_dispatch(data).task == data["task"]


@pytest.mark.parametrize("path", DISPATCH_INVALID, ids=ids(DISPATCH_INVALID))
def test_invalid_dispatch_fails_schema_and_model(path):
    data = load(path)
    assert not schema_accepts("dispatch", data)
    with pytest.raises(contract.ContractError):
        contract.parse_dispatch(data)


RESULT_VALID = examples("result", "valid")
RESULT_INVALID = examples("result", "invalid")


def task_of(path):
    # Examples are named <task>.<valid|invalid>[-why].json
    return path.name.split(".")[0]


@pytest.mark.parametrize("path", RESULT_VALID, ids=ids(RESULT_VALID))
def test_valid_result_passes_schema_and_model(path):
    data = load(path)
    assert schema_accepts("result", data)
    assert contract.parse_result(data, task=task_of(path)).task == task_of(path)


@pytest.mark.parametrize("path", RESULT_INVALID, ids=ids(RESULT_INVALID))
def test_invalid_result_fails_schema_and_model(path):
    data = load(path)
    assert not schema_accepts("result", data)
    with pytest.raises(contract.ContractError):
        contract.parse_result(data, task=task_of(path))


@pytest.mark.parametrize("path", RESULT_VALID, ids=ids(RESULT_VALID))
def test_a_result_for_the_wrong_task_fails(path):
    data = load(path)
    for other in TASKS:
        if other != task_of(path):
            with pytest.raises(contract.ContractError):
                contract.parse_result(data, task=other)


SCHEMAS = ["dispatch", "result", "enrolment"]


def object_schemas(node, where="#"):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield where, node
        for key, child in node.items():
            yield from object_schemas(child, f"{where}/{key}")
    elif isinstance(node, list):
        for i, child in enumerate(node):
            yield from object_schemas(child, f"{where}/{i}")


@pytest.mark.parametrize("name", SCHEMAS)
def test_every_object_forbids_additional_properties(name):
    objects = list(object_schemas(schema(name)))
    assert objects
    loose = [where for where, node in objects if node.get("additionalProperties") is not False]
    assert loose == []


def test_contract_version_is_one_integer_shared_by_both_contract_schemas():
    dispatch = schema("dispatch")
    assert dispatch["$defs"]["contract_version"]["const"] == 1
    assert schema("result")["properties"]["contract_version"] == {
        "$ref": "dispatch.schema.json#/$defs/contract_version"
    }
    for task in TASKS:
        assert dispatch["$defs"][task]["properties"]["contract_version"] == {
            "$ref": "#/$defs/contract_version"
        }
    assert contract.CONTRACT_VERSION == 1


def test_models_know_the_same_failure_reasons_as_the_schema():
    assert list(contract.FAILURES) == schema("result")["$defs"]["failure"]["enum"]


def test_result_schema_knows_every_task():
    assert schema("result")["properties"]["task"]["enum"] == TASKS


ENROLMENT_VALID = sorted((EXAMPLES / "enrolment").glob("*.valid*.yml"))
ENROLMENT_INVALID = sorted((EXAMPLES / "enrolment").glob("*.invalid*.yml"))


def test_there_are_valid_and_invalid_enrolment_examples():
    assert ENROLMENT_VALID and ENROLMENT_INVALID


@pytest.mark.parametrize("path", ENROLMENT_VALID, ids=ids(ENROLMENT_VALID))
def test_valid_enrolment_passes_schema_and_model(path):
    text = path.read_text()
    assert schema_accepts("enrolment", yaml.safe_load(text))
    contract.parse_enrolment(text)


@pytest.mark.parametrize("path", ENROLMENT_INVALID, ids=ids(ENROLMENT_INVALID))
def test_invalid_enrolment_fails_schema_and_model(path):
    text = path.read_text()
    assert not schema_accepts("enrolment", yaml.safe_load(text))
    with pytest.raises(contract.ContractError):
        contract.parse_enrolment(text)


def test_an_enrolment_without_tasks_or_enabled_enables_everything():
    enrolment = contract.parse_enrolment("setup: uv sync\ncheck: pytest\n")
    assert enrolment.enabled is True
    assert enrolment.enabled_tasks() == set(TASKS)


def test_an_enrolment_lists_the_tasks_it_enables_and_smoke_is_always_on():
    enrolment = contract.parse_enrolment(
        "setup: uv sync\ncheck: pytest\ntasks: [explore, review]\n"
    )
    assert enrolment.enabled_tasks() == {"explore", "review", "smoke"}


def test_an_enrolment_overrides_timeouts_and_models_per_task():
    enrolment = contract.parse_enrolment(
        "setup: uv sync\ncheck: pytest\n"
        "timeouts:\n  implement: 55\n"
        "models:\n  explore: claude-haiku-4-5\n"
    )
    assert enrolment.timeouts == {"implement": 55}
    assert enrolment.models == {"explore": "claude-haiku-4-5"}


def test_an_enrolment_that_is_not_yaml_is_a_contract_error():
    with pytest.raises(contract.ContractError):
        contract.parse_enrolment("setup: [unclosed\n")
