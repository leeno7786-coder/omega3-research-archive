"""The direct runtime diagnostic must not expose the retired embed command."""

from __future__ import annotations

import pytest


def test_runtime_direct_help_omits_embedding_command(capsys):
    from scripts import runtime_direct

    with pytest.raises(SystemExit):
        runtime_direct.main(["--help"])

    assert "embed" not in capsys.readouterr().out
