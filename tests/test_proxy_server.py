"""Unit tests for proxy/server.py pure functions.

Covers:
- _provider_for_alias
- _model_key
- _expand_hierarchy
- _load_hierarchies
- _build_compact_hierarchy (round-trip)
- _validate_hierarchy
- _is_disabled / _mark_disabled (TTL behavior)
- pick_free_connection (NVIDIA-first, max_weight, preferred_model, busy/disabled)
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PROXY_DIR = Path(__file__).resolve().parent.parent / "proxy"
SERVER_PATH = PROXY_DIR / "server.py"


def _load_server():
    """Load server.py as a module without running __main__."""
    spec = importlib.util.spec_from_file_location("server", SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def srv():
    return _load_server()


# --- _provider_for_alias ---------------------------------------------------

class TestProviderForAlias:
    def test_nim_aliases_map_to_nvidia(self, srv):
        assert srv._provider_for_alias("nim-1") == "nvidia"
        assert srv._provider_for_alias("nim-2") == "nvidia"
        assert srv._provider_for_alias("nim-3") == "nvidia"

    def test_gemini_aliases_map_to_gemini(self, srv):
        assert srv._provider_for_alias("gemini-1") == "gemini"
        assert srv._provider_for_alias("gemini-2") == "gemini"

    def test_single_word_aliases_pass_through(self, srv):
        assert srv._provider_for_alias("openrouter") == "openrouter"
        assert srv._provider_for_alias("groq") == "groq"
        assert srv._provider_for_alias("mistral") == "mistral"


# --- _model_key -------------------------------------------------------------

class TestModelKey:
    def test_format(self, srv):
        assert srv._model_key("nvidia", "conn-1", "model/x") == "nvidia:conn-1:model/x"

    def test_unique_per_provider(self, srv):
        a = srv._model_key("nvidia", "c", "m")
        b = srv._model_key("groq", "c", "m")
        assert a != b

    def test_unique_per_conn(self, srv):
        a = srv._model_key("nvidia", "c1", "m")
        b = srv._model_key("nvidia", "c2", "m")
        assert a != b


# --- _expand_hierarchy ------------------------------------------------------

class TestExpandHierarchy:
    def test_single_conn_produces_one_tuple(self, srv):
        entries = [{"model": "m/x", "weight": 50, "conns": ["nim-1"]}]
        out = srv._expand_hierarchy(entries)
        assert len(out) == 1
        provider, conn_id, model, weight = out[0]
        assert provider == "nvidia"
        assert conn_id == srv.NVIDIA_CONNS["nim-1"]
        assert model == "m/x"
        assert weight == 50

    def test_multiple_conns_produce_n_tuples(self, srv):
        entries = [{"model": "m/x", "weight": 50, "conns": ["nim-1", "nim-2", "nim-3"]}]
        out = srv._expand_hierarchy(entries)
        assert len(out) == 3
        assert all(t[1] in (srv.NVIDIA_CONNS["nim-1"], srv.NVIDIA_CONNS["nim-2"], srv.NVIDIA_CONNS["nim-3"]) for t in out)

    def test_mixed_providers(self, srv):
        entries = [
            {"model": "nvidia/m", "weight": 90, "conns": ["nim-1"]},
            {"model": "groq/m", "weight": 10, "conns": ["groq"]},
        ]
        out = srv._expand_hierarchy(entries)
        providers = [t[0] for t in out]
        assert providers == ["nvidia", "groq"]

    def test_unknown_alias_raises_keyerror(self, srv):
        entries = [{"model": "m/x", "weight": 50, "conns": ["bogus-alias"]}]
        with pytest.raises(KeyError):
            srv._expand_hierarchy(entries)

    def test_empty_entries(self, srv):
        assert srv._expand_hierarchy([]) == []


# --- _load_hierarchies ------------------------------------------------------

class TestLoadHierarchies:
    def test_load_example_file(self, srv, tmp_path):
        data = {
            "nvidia-start": [{"model": "m/x", "weight": 50, "conns": ["nim-1"]}],
            "nvidia-vision": [],
        }
        path = tmp_path / "hier.json"
        path.write_text(json.dumps(data))
        loaded = srv._load_hierarchies(str(path))
        assert "nvidia-start" in loaded
        assert "nvidia-vision" in loaded
        assert len(loaded["nvidia-start"]) == 1
        assert loaded["nvidia-vision"] == []

    def test_invalid_json_raises(self, srv, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json {{{")
        with pytest.raises(json.JSONDecodeError):
            srv._load_hierarchies(str(path))


# --- _build_compact_hierarchy ----------------------------------------------

class TestBuildCompactHierarchy:
    def test_round_trip_preserves_models(self, srv):
        """expanded → compact → expanded should be equivalent."""
        # Build known expanded list and inject into HIERARCHIES
        original = srv._get_hierarchies()
        try:
            srv.HIERARCHIES = {
                "nvidia-start": [
                    ("nvidia", srv.NVIDIA_CONNS["nim-1"], "m/a", 100),
                    ("nvidia", srv.NVIDIA_CONNS["nim-2"], "m/a", 100),
                    ("groq", srv.VISION_CONNS["groq"], "m/b", 50),
                ],
                "nvidia-vision": [
                    ("nvidia", srv.NVIDIA_CONNS["nim-1"], "m/v", 90),
                ],
            }
            # Build compact via unbound method (no instance needed)
            compact = srv.ComboProxyHandler._build_compact_hierarchy(None)
            # Verify m/a collapsed into single entry with 2 conns
            ma = [e for e in compact["nvidia-start"] if e["model"] == "m/a"][0]
            assert ma["weight"] == 100
            assert sorted(ma["conns"]) == ["nim-1", "nim-2"]
            # Verify m/b is separate entry
            mb = [e for e in compact["nvidia-start"] if e["model"] == "m/b"][0]
            assert mb["weight"] == 50
            assert mb["conns"] == ["groq"]
            # Verify nvidia-vision preserved
            assert len(compact["nvidia-vision"]) == 1
            assert compact["nvidia-vision"][0]["model"] == "m/v"
        finally:
            srv.HIERARCHIES = original

    def test_empty_hierarchies(self, srv):
        original = srv._get_hierarchies()
        try:
            srv.HIERARCHIES = {}
            compact = srv.ComboProxyHandler._build_compact_hierarchy(None)
            assert compact == {}
        finally:
            srv.HIERARCHIES = original


# --- _validate_hierarchy ----------------------------------------------------

class TestValidateHierarchy:
    @pytest.fixture
    def handler(self, srv):
        # Unbound method: pass None as self (no instance state used by _validate_hierarchy)
        def validate(data):
            return srv.ComboProxyHandler._validate_hierarchy(None, data)
        return type("Handler", (), {"_validate_hierarchy": staticmethod(validate)})()

    def test_valid_data_passes(self, srv, handler):
        data = {
            "nvidia-start": [{"model": "m/x", "weight": 50, "conns": ["nim-1"]}],
            "nvidia-vision": [],
        }
        assert handler._validate_hierarchy(data) is True

    def test_not_dict_returns_false(self, handler):
        assert handler._validate_hierarchy([]) is False
        assert handler._validate_hierarchy("string") is False
        assert handler._validate_hierarchy(None) is False

    def test_unknown_combo_returns_false(self, handler):
        data = {"bogus-combo": []}
        assert handler._validate_hierarchy(data) is False

    def test_missing_keys_returns_false(self, handler):
        data = {"nvidia-start": [{"model": "m", "weight": 50}]}  # no conns
        assert handler._validate_hierarchy(data) is False

    def test_unknown_conn_returns_false(self, handler):
        data = {"nvidia-start": [{"model": "m", "weight": 50, "conns": ["bogus"]}]}
        assert handler._validate_hierarchy(data) is False

    def test_conns_not_list_returns_false(self, handler):
        data = {"nvidia-start": [{"model": "m", "weight": 50, "conns": "nim-1"}]}
        assert handler._validate_hierarchy(data) is False

    def test_entries_not_list_returns_false(self, handler):
        data = {"nvidia-start": "not-a-list"}
        assert handler._validate_hierarchy(data) is False


# --- _is_disabled / _mark_disabled -----------------------------------------

class TestDisabledTTL:
    def test_mark_then_is_disabled(self, srv):
        srv._disabled.clear()
        srv._mark_disabled("k", "test")
        assert srv._is_disabled("k") is True

    def test_unmarked_is_not_disabled(self, srv):
        srv._disabled.clear()
        assert srv._is_disabled("never-marked") is False

    def test_expired_ttl_releases(self, srv):
        srv._disabled.clear()
        # Override TTL via direct injection with old timestamp
        old_ts = time.time() - srv.DISABLED_TTL - 10
        srv._disabled["expired"] = (old_ts, "stale")
        assert srv._is_disabled("expired") is False
        # After check, it should be removed
        assert "expired" not in srv._disabled


# --- pick_free_connection ---------------------------------------------------

class TestPickFreeConnection:
    @pytest.fixture(autouse=True)
    def reset_state(self, srv):
        srv._inflight.clear()
        srv._disabled.clear()
        yield
        srv._inflight.clear()
        srv._disabled.clear()

    @pytest.fixture
    def known_hier(self, srv):
        """Inject a known hierarchy with 2 nvidia + 1 externo, no busy."""
        original = srv._get_hierarchies()
        srv.HIERARCHIES = {
            "nvidia-start": [
                ("nvidia", srv.NVIDIA_CONNS["nim-1"], "nvidia/top", 100),
                ("nvidia", srv.NVIDIA_CONNS["nim-2"], "nvidia/top", 100),
                ("groq", srv.VISION_CONNS["groq"], "groq/fallback", 10),
            ],
        }
        yield
        srv.HIERARCHIES = original

    def test_nvidia_first_when_free(self, srv, known_hier):
        conn_id, model, provider, key = srv.pick_free_connection("nvidia-start")
        assert provider == "nvidia"
        assert model == "nvidia/top"

    def test_skips_disabled(self, srv, known_hier):
        srv._mark_disabled("nvidia:{0}:nvidia/top".format(srv.NVIDIA_CONNS["nim-1"]), "test")
        conn_id, model, provider, key = srv.pick_free_connection("nvidia-start")
        # Should fall to nim-2 (other nvidia account with same model)
        assert provider == "nvidia"
        assert conn_id == srv.NVIDIA_CONNS["nim-2"]

    def test_skips_busy(self, srv, known_hier):
        srv._inflight[srv.NVIDIA_CONNS["nim-1"]] = {"since": time.time()}
        conn_id, model, provider, key = srv.pick_free_connection("nvidia-start")
        assert conn_id == srv.NVIDIA_CONNS["nim-2"]

    def test_skips_tried(self, srv, known_hier):
        # Mark both nvidia conns as tried
        tried = {
            "nvidia:{0}:nvidia/top".format(srv.NVIDIA_CONNS["nim-1"]),
            "nvidia:{0}:nvidia/top".format(srv.NVIDIA_CONNS["nim-2"]),
        }
        conn_id, model, provider, key = srv.pick_free_connection("nvidia-start", tried)
        # Both nvidia tried, should fall to externo (groq)
        assert provider == "groq"

    def test_max_weight_filters_higher(self, srv, known_hier):
        # nvidia/top has weight 100, groq has 10. With max_weight=50, nvidia excluded.
        conn_id, model, provider, key = srv.pick_free_connection(
            "nvidia-start", max_weight=50
        )
        assert provider == "groq"
        assert model == "groq/fallback"

    def test_max_weight_none_keeps_all(self, srv, known_hier):
        conn_id, model, provider, key = srv.pick_free_connection("nvidia-start", max_weight=None)
        assert provider == "nvidia"

    def test_preferred_model_promoted(self, srv, known_hier):
        # Add a lower-weight preferred model
        srv.HIERARCHIES["nvidia-start"].append(
            ("nvidia", srv.NVIDIA_CONNS["nim-1"], "nvidia/preferred", 50)
        )
        conn_id, model, provider, key = srv.pick_free_connection(
            "nvidia-start", preferred_model="nvidia/preferred"
        )
        assert model == "nvidia/preferred"
        assert provider == "nvidia"

    def test_preferred_model_not_in_hierarchy_ignored(self, srv, known_hier):
        conn_id, model, provider, key = srv.pick_free_connection(
            "nvidia-start", preferred_model="does-not-exist"
        )
        # Falls back to normal pick
        assert provider == "nvidia"
        assert model == "nvidia/top"

    def test_unknown_combo_returns_none(self, srv, known_hier):
        assert srv.pick_free_connection("bogus-combo") is None

    def test_all_busy_returns_none_or_externo(self, srv, known_hier):
        # Fill both nvidia slots
        srv._inflight[srv.NVIDIA_CONNS["nim-1"]] = {"since": time.time()}
        srv._inflight[srv.NVIDIA_CONNS["nim-2"]] = {"since": time.time()}
        # Without time mocking, it waits WAIT_SHORT_SEC (3s) then falls to externo
        # Mark groq as busy too
        srv._inflight[srv.VISION_CONNS["groq"]] = {"since": time.time()}
        # Now everything busy
        result = srv.pick_free_connection("nvidia-start")
        assert result is None


# --- Config contracts -------------------------------------------------------

class TestConfigContracts:
    """Ensure critical config keys exist with correct types (regression guard)."""

    def test_config_example_has_nvidia_conns(self):
        path = PROXY_DIR / "config.example.json"
        cfg = json.loads(path.read_text())
        for key in ("nim-1", "nim-2", "nim-3"):
            assert key in cfg["nvidia_conns"], f"missing {key}"

    def test_config_example_has_vision_conns(self):
        path = PROXY_DIR / "config.example.json"
        cfg = json.loads(path.read_text())
        for key in ("openrouter", "groq", "gemini-1", "gemini-2", "mistral"):
            assert key in cfg["vision_conns"], f"missing {key}"

    def test_hierarchies_example_is_valid_compact(self, srv):
        path = PROXY_DIR / "hierarchies.example.json"
        data = json.loads(path.read_text())
        # All conns referenced must exist in CONN_BY_ALIAS
        for combo, entries in data.items():
            assert combo in ("nvidia-start", "nvidia-vision")
            for e in entries:
                for c in e["conns"]:
                    assert c in srv.CONN_BY_ALIAS, f"{combo}: {e['model']} uses unknown conn {c}"
                    assert isinstance(e["weight"], int)
                    assert e["weight"] > 0


# --- Gitignored secrets check (regression) ---------------------------------

class TestNoSecretsCommitted:
    """Ensure no real secrets accidentally land in tracked files."""

    def test_example_configs_use_placeholders(self):
        # config.example.json
        cfg = json.loads((PROXY_DIR / "config.example.json").read_text())
        for k, v in {**cfg.get("nvidia_conns", {}), **cfg.get("vision_conns", {})}.items():
            assert v.startswith("<") and v.endswith(">"), f"{k} not a placeholder"
        assert cfg["api_key"].startswith("<")
        assert cfg["admin_key"].startswith("<")

    def test_env_example_uses_placeholders(self):
        env = Path(__file__).resolve().parent.parent / ".env.example"
        text = env.read_text()
        # Real keys follow specific formats; placeholders are <...> or "..."
        for marker in ("<GATEWAY_API_KEY>", "<sk-...>", "<auto-generated>"):
            assert marker in text
