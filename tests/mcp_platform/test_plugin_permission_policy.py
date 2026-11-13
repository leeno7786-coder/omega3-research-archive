"""The permission form's execution policy is refused at the lifecycle boundary with its own code.

An off-list policy used to pass validation as any string and fail later, inside connection
setup, as a bare enum error the HUD could only show raw.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_platform.plugin_authority import PluginExecutionPolicy
from tests.mcp_platform.test_plugin_lifecycle import _revision_lifecycle


def _service(starts: list[dict]):
    service = _revision_lifecycle()

    async def permissions(_plugin_id: str, _app_id: str | None):
        return {
            "app_id": "office.mail",
            "offered": {"provider_scopes": [], "capability_ids": []},
        }

    async def start(*_args, **kwargs):
        starts.append(kwargs)
        return SimpleNamespace(status="authorizing")

    service.permissions = permissions
    service.descriptor_for_plugin_app = lambda _plugin_id, _app_id: SimpleNamespace(
        declared_capabilities={}
    )
    service.operator_account_key = lambda: "operator"
    service._connection_service = SimpleNamespace(start=start)
    return service


def _settings(policy: str) -> dict[str, object]:
    return {
        "provider_scopes": [],
        "capability_ids": [],
        "execution_policy": policy,
        "source_constraints": {},
        "destination_constraints": {},
    }


@pytest.mark.anyio
async def test_an_off_list_execution_policy_is_refused_before_anything_starts() -> None:
    starts: list[dict] = []
    service = _service(starts)

    with pytest.raises(ValueError, match="^plugin_execution_policy_invalid$"):
        await service.set_permissions(
            "office.bundle",
            revision=service.revision,
            app_id="office.mail",
            settings=_settings("run_everything"),
        )

    assert starts == []


@pytest.mark.anyio
@pytest.mark.parametrize("policy", [policy.value for policy in PluginExecutionPolicy])
async def test_every_policy_the_form_offers_is_accepted(policy: str) -> None:
    starts: list[dict] = []
    service = _service(starts)

    await service.set_permissions(
        "office.bundle",
        revision=service.revision,
        app_id="office.mail",
        settings=_settings(policy),
    )

    assert [str(getattr(start["execution_policy"], "value", start["execution_policy"])) for start in starts] == [policy]
