import json
from pathlib import Path
from memory.rollback import restore_canonical_write


def test_restore_canonical_write_restores_prior_content(tmp_path, monkeypatch):
    import memory.rollback as rb

    # point the path-traversal guard's memory root at tmp_path
    monkeypatch.setattr(rb, "OMEGA_ROOT", tmp_path)
    mem_root = tmp_path / "memory"
    mem_root.mkdir(exist_ok=True)
    final = mem_root / "trait_x.json"
    # simulate: prior good record was overwritten by a failed re-write
    prior = {
        "id": "trait_x",
        "coord": [0, 2, 0],
        "type": "emergent_trait",
        "title": "x",
        "summary": "prior",
        "text": "Trait: x (prior)",
    }
    prior_bytes = json.dumps(prior).encode("utf-8")
    final.write_text(json.dumps({"id": "trait_x", "text": "CLOBBERED"}), encoding="utf-8")
    # neutralize index repatch (no real index in this unit test)
    monkeypatch.setattr(rb, "patch_index", lambda meta: None)
    res = restore_canonical_write({"id": "trait_x"}, str(final), prior_bytes)
    assert res["restored"] is True
    assert json.loads(final.read_text(encoding="utf-8"))["text"] == "Trait: x (prior)"


def test_restore_refuses_outside_memory_root(tmp_path, monkeypatch):
    import memory.rollback as rb

    monkeypatch.setattr(rb, "OMEGA_ROOT", tmp_path)
    (tmp_path / "memory").mkdir(exist_ok=True)
    outside = tmp_path / "evil.json"
    outside.write_text("x", encoding="utf-8")
    res = restore_canonical_write({"id": "e"}, str(outside), b"{}")
    assert res["restored"] is False and res["errors"]
