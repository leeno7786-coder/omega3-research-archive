"""Schema-pinned adapter for the current MCP Tasks extension."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import warnings
from typing import Any, Literal

import jsonschema
from mcp import types
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.warnings import TypedDictExtraConfigWarning

# mcp-types declares RequestParams._meta as an OPEN TypedDict, because _meta is
# the MCP spec's deliberate extension point. Omega's request models below close
# themselves with extra="forbid", so Pydantic warns once per model that it
# cannot apply that setting to the open member and will ignore it. That is the
# correct outcome -- an extensible _meta is what the spec asks for, and nothing
# here depends on Pydantic to reject unknown keys inside it; every field Omega
# reads is validated against an exact set of its own. The notice is silenced at
# the one place it originates rather than left to train the operator to ignore
# warnings on every boot. tests/mcp_platform/fake_stdio_server.py and pytest.ini
# already suppress the identical message.
warnings.filterwarnings(
    "ignore",
    message=(
        r"TypedDict class 'RequestParamsMeta' allows extra items, but 'extra' "
        r"configuration is set to `'forbid'`\."
    ),
    category=TypedDictExtraConfigWarning,
)

from core.paths import OMEGA_ROOT
from mcp_platform.canonical import canonical_bytes
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION

TASKS_EXTENSION_IDENTIFIER = "io.modelcontextprotocol/tasks"
TASKS_SCHEMA_SHA256 = "b17cb4a2534379c214b17770bd5d3d54f69fde16a953bfb542c58235a61274bb"
_TASKS_SCHEMA_PATH = (
    OMEGA_ROOT / "schema" / "mcp" / "extensions" / "io.modelcontextprotocol.tasks.schema.json"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:/-]{0,255}\Z")
_TASK_STATES = frozenset({"working", "input_required", "completed", "failed", "cancelled"})
_TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})


def _load_schema() -> dict[str, Any]:
    schema_bytes = _TASKS_SCHEMA_PATH.read_bytes()
    if hashlib.sha256(schema_bytes).hexdigest() != TASKS_SCHEMA_SHA256:
        raise RuntimeError("the pinned MCP Tasks schema digest does not match")
    parsed = json.loads(schema_bytes)
    if not isinstance(parsed, dict):
        raise RuntimeError("the pinned MCP Tasks schema is not an object")
    jsonschema.Draft202012Validator.check_schema(parsed)
    return parsed


_TASKS_SCHEMA = _load_schema()


def _validator(definition: str) -> jsonschema.Draft202012Validator:
    schema = {
        "$schema": _TASKS_SCHEMA["$schema"],
        "$defs": _TASKS_SCHEMA["$defs"],
        "$ref": f"#/$defs/{definition}",
    }
    return jsonschema.Draft202012Validator(schema)


_WIRE_VALIDATORS = {
    "tasks/get": (_validator("GetTaskRequest"), _validator("GetTaskResult")),
    "tasks/update": (_validator("UpdateTaskRequest"), _validator("UpdateTaskResult")),
    "tasks/cancel": (_validator("CancelTaskRequest"), _validator("CancelTaskResult")),
}
_CREATE_RESULT_VALIDATOR = _validator("CreateTaskResult")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} is invalid")
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _finite_nonnegative(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} is invalid")
    return float(value)


class _TaskIdParams(types.RequestParams):
    model_config = ConfigDict(extra="forbid")

    task_id: str

    @field_validator("task_id")
    @classmethod
    def _valid_task_id(cls, value: str) -> str:
        return _identifier(value, field="task_id")


class GetTaskParams(_TaskIdParams):
    pass


class GetTaskRequest(types.Request[GetTaskParams, Literal["tasks/get"]]):
    model_config = ConfigDict(extra="forbid")
    name_param = "taskId"

    method: Literal["tasks/get"] = "tasks/get"
    params: GetTaskParams


class UpdateTaskParams(_TaskIdParams):
    input_responses: dict[str, Any]

    @field_validator("input_responses")
    @classmethod
    def _valid_input_responses(cls, value: dict[str, Any]) -> dict[str, Any]:
        canonical_bytes(value)
        return value


class UpdateTaskRequest(types.Request[UpdateTaskParams, Literal["tasks/update"]]):
    model_config = ConfigDict(extra="forbid")
    name_param = "taskId"

    method: Literal["tasks/update"] = "tasks/update"
    params: UpdateTaskParams


class CancelTaskParams(_TaskIdParams):
    pass


class CancelTaskRequest(types.Request[CancelTaskParams, Literal["tasks/cancel"]]):
    model_config = ConfigDict(extra="forbid")
    name_param = "taskId"

    method: Literal["tasks/cancel"] = "tasks/cancel"
    params: CancelTaskParams


class TaskElicitationParams(BaseModel):
    """Closed form-mode elicitation embedded in a Tasks input request."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: Literal["form"] = "form"
    message: str
    requested_schema: dict[str, Any] = Field(alias="requestedSchema")

    @field_validator("message")
    @classmethod
    def _valid_message(cls, value: str) -> str:
        if (
            type(value) is not str
            or not value
            or unicodedata.normalize("NFC", value) != value
            or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
        ):
            raise ValueError("task input request message is invalid")
        return value

    @field_validator("requested_schema")
    @classmethod
    def _valid_requested_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        canonical_bytes(value)
        return value


class TaskInputRequest(BaseModel):
    """The exact supported server request embedded by the Tasks extension."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["elicitation/create"]
    params: TaskElicitationParams


class TaskInputResponse(BaseModel):
    """A closed elicitation result supplied to ``tasks/update``."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "decline", "cancel"]
    content: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _valid_action_content(self) -> TaskInputResponse:
        if (self.action == "accept") != (self.content is not None):
            raise ValueError("task input response payload is invalid")
        if self.content is not None:
            canonical_bytes(self.content)
        return self


class _TaskSummary(types.Result):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["working", "input_required", "completed", "failed", "cancelled"]
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: float | None
    poll_interval_ms: float | None = None

    @field_validator("task_id")
    @classmethod
    def _valid_task_id(cls, value: str) -> str:
        return _identifier(value, field="task_id")

    @field_validator("status_message", "created_at", "last_updated_at")
    @classmethod
    def _bounded_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            type(value) is not str
            or unicodedata.normalize("NFC", value) != value
            or len(value) > 4_096
            or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
        ):
            raise ValueError("task text is invalid")
        return value

    @field_validator("ttl_ms", "poll_interval_ms")
    @classmethod
    def _valid_duration(cls, value: float | None, info: Any) -> float | None:
        return _finite_nonnegative(value, field=info.field_name)


class CreateTaskResult(_TaskSummary):
    result_type: Literal["task"]


class GetTaskResult(_TaskSummary):
    input_requests: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    result_type: Literal["complete"]

    @model_validator(mode="after")
    def _valid_state_payload(self) -> GetTaskResult:
        fields = {
            "input_required": self.input_requests is not None,
            "completed": self.result is not None,
            "failed": self.error is not None,
        }
        for name, present in fields.items():
            if present != (self.status == name):
                raise ValueError("task state payload is invalid")
        for value in (self.input_requests, self.result, self.error):
            if value is not None:
                canonical_bytes(value)
        return self


class UpdateTaskResult(types.Result):
    model_config = ConfigDict(extra="forbid")

    result_type: Literal["complete"]


class CancelTaskResult(types.Result):
    model_config = ConfigDict(extra="forbid")

    result_type: Literal["complete"]


async def _claimed_task_requires_bound_handling(
    _result: CreateTaskResult,
    _context: ClaimContext,
) -> types.CallToolResult:
    raise RuntimeError("claimed Tasks results require Omega's bound Task handler")


class TasksClientExtension(ClientExtension):
    """Declare and parse the exact schema-pinned Tasks result claim."""

    identifier = TASKS_EXTENSION_IDENTIFIER

    def settings(self) -> dict[str, Any]:
        return {}

    def claims(self) -> tuple[ResultClaim[CreateTaskResult], ...]:
        return (
            ResultClaim(
                result_type="task",
                model=CreateTaskResult,
                resolve=_claimed_task_requires_bound_handling,
                protocol_versions=frozenset({MCP_PROTOCOL_VERSION}),
            ),
        )


def validate_create_task_result(result: CreateTaskResult) -> None:
    document = result.model_dump(by_alias=True, exclude_none=True)
    document.pop("resultType", None)
    document.pop("_meta", None)
    _CREATE_RESULT_VALIDATOR.validate(document)


def validate_task_request(request: types.Request[Any, Any]) -> None:
    validators = _WIRE_VALIDATORS.get(request.method)
    if validators is None:
        raise ValueError("unsupported Tasks request method")
    document = request.model_dump(by_alias=True, exclude_none=True)
    document["jsonrpc"] = "2.0"
    document["id"] = "omega-task-request"
    validators[0].validate(document)


def validate_task_result(method: str, result: types.Result) -> None:
    validators = _WIRE_VALIDATORS.get(method)
    if validators is None:
        raise ValueError("unsupported Tasks result method")
    document = result.model_dump(by_alias=True, exclude_none=True)
    document.pop("resultType", None)
    document.pop("_meta", None)
    validators[1].validate(document)


def normalized_task(result: GetTaskResult) -> dict[str, object]:
    document = result.model_dump(
        by_alias=False,
        exclude_none=True,
        exclude={"meta", "result_type"},
    )
    canonical_bytes(document)
    return document


def parse_task_input_request(value: Any) -> TaskInputRequest:
    return TaskInputRequest.model_validate(value)


def parse_task_input_response(value: Any) -> TaskInputResponse:
    return TaskInputResponse.model_validate(value)


def is_terminal_task_status(status: str) -> bool:
    if status not in _TASK_STATES:
        raise ValueError("task status is invalid")
    return status in _TERMINAL_TASK_STATES


__all__ = [
    "TASKS_EXTENSION_IDENTIFIER",
    "TASKS_SCHEMA_SHA256",
    "CancelTaskParams",
    "CancelTaskRequest",
    "CancelTaskResult",
    "CreateTaskResult",
    "GetTaskParams",
    "GetTaskRequest",
    "GetTaskResult",
    "TasksClientExtension",
    "TaskElicitationParams",
    "TaskInputRequest",
    "TaskInputResponse",
    "UpdateTaskParams",
    "UpdateTaskRequest",
    "UpdateTaskResult",
    "is_terminal_task_status",
    "normalized_task",
    "parse_task_input_request",
    "parse_task_input_response",
    "validate_create_task_result",
    "validate_task_request",
    "validate_task_result",
]
