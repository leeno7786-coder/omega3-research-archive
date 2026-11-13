import scripts.boot_gate as bg


def test_identity_recall_returns_creator_anchor_not_base_model(monkeypatch):
    queries = []

    def fake_recall_titles(query, **_kwargs):
        queries.append(query)
        if query == "who is your creator?":
            return ["Creator — Omega AI"]
        if query == "describe yourself":
            return ["Omega — sovereign cognitive system"]
        if query == "how does the memory taxonomy work?":
            return ["Memory taxonomy routes records through canonical lobes"]
        return []

    monkeypatch.setattr(bg, "_recall_titles", fake_recall_titles)
    res = bg.check_identity_persona_knowledge()
    assert res.ok, res.detail
    assert all("noah" not in query.lower() for query in queries)
    assert "creator omega founding identity" in queries
    assert "Creator — Omega AI" in res.detail["identity"]
    # The "who is your creator?" wrong-shape regression class: never a base-model leak.
    blob = repr(res.detail).lower()
    assert "google" not in blob and "alibaba" not in blob and "qwen" not in blob


from unittest.mock import patch


def test_main_green_returns_zero_and_does_not_boot(capsys):
    with (
        patch.object(bg, "run_stress_report", return_value=bg.GateResult(True, {"misroute": 0})),
        patch.object(
            bg,
            "check_identity_persona_knowledge",
            return_value=bg.GateResult(True, {"identity": ["Creator — Omega AI"]}),
        ),
    ):
        rc = bg.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "GATE GREEN" in out
    assert "await operator boot" in out
    # The gate must NEVER import/start the loop.
    assert "boot()" not in out  # smoke: no boot summary printed


def test_main_red_on_misroute_returns_one():
    with (
        patch.object(bg, "run_stress_report", return_value=bg.GateResult(False, "misroute=3")),
        patch.object(bg, "check_identity_persona_knowledge", return_value=bg.GateResult(True, {})),
    ):
        assert bg.main([]) == 1
