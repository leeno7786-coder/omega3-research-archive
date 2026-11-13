"""RED-first: the shared model-JSON extractor must SALVAGE a near-valid object
whose only defect is a mismatched/missing bracket — the exact failure that pegged
the GPU on the first live 10-episode window reflection (gemma closed a
``[``-opened array with ``}``). It must NOT over-repair genuine garbage into a
dict (no silent fabrication), and well-formed output must parse untouched via the
strict path.
"""

from omega_loop._json_extract import extract_json_object

# The literal shape gemma emitted on the incident window: pressure_feedback.
# contributors_to_create is an array opened with "[" and closed with "}".
_INCIDENT = """```json
{
  "trajectory": {"decision": "establish", "target_domain": "Machine Learning"},
  "pressure_feedback": {
    "contributors_to_create": [
      {
        "type": "operator_priority",
        "question": "Define the foundational concepts of ETL/ELT in the context of modern data pipelines."
      }
    }
  }
}
```"""


def test_repairs_mismatched_array_closer():
    out = extract_json_object(_INCIDENT)
    assert out is not None, "the one-bracket defect must be repaired, not rejected"
    assert out["trajectory"]["decision"] == "establish"
    created = out["pressure_feedback"]["contributors_to_create"]
    assert isinstance(created, list) and len(created) == 1
    assert created[0]["type"] == "operator_priority"


def test_repairs_truncated_trailing_closers():
    # Prefill-truncated output: missing the final closers entirely.
    truncated = '{"a": {"b": [1, 2, 3'
    out = extract_json_object(truncated)
    assert out == {"a": {"b": [1, 2, 3]}}


def test_well_formed_is_untouched():
    out = extract_json_object('{"decision": "idle", "n": 7}')
    assert out == {"decision": "idle", "n": 7}


def test_braces_inside_strings_are_not_treated_as_structure():
    # A string value containing brackets must not confuse the repair scanner.
    payload = '{"q": "what is a list [a, b] or dict {x: 1}?", "ok": true}'
    out = extract_json_object(payload)
    assert out == {"q": "what is a list [a, b] or dict {x: 1}?", "ok": True}


def test_accepts_python_literal_dict_shape():
    payload = """<think>done</think>
```json
{'response_text': 'Machine learning uses data.', 'pressure_params': {},}
```"""
    out = extract_json_object(payload)
    assert out == {
        "response_text": "Machine learning uses data.",
        "pressure_params": {},
    }


def test_does_not_fabricate_from_garbage():
    assert extract_json_object("just some prose, no json here") is None
    assert extract_json_object("") is None
    assert extract_json_object(None) is None
    # Brackets present but the content is not JSON — must stay None, not invent.
    assert extract_json_object("{ this is not : valid json at all ]") is None


def test_pathological_input_fails_closed_never_raises():
    """extract_json_object must NEVER raise — a deeply-nested payload that trips
    RecursionError in json.loads must fail closed to None (the per-episode path
    calls this too; a raise here would crash cognition).

    Depth must exceed EVERY platform's C-recursion ceiling: CPython 3.13 allows
    ~4k frames on Windows but ~10k on Linux, so the old 6000 bomb parsed
    successfully on Linux instead of exercising the fail-closed path."""
    bomb = '{"a":' * 100_000 + "1" + "}" * 100_000
    assert extract_json_object(bomb) is None
