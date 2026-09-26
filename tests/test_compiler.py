"""Real uploaded Sheetbend selection/lifetime API with a labeled inference test double."""

from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from sheetbend_any import Request, SheetbendCompiler
from sheetbend_any.errors import GenerationError

sheetbend = pytest.importorskip("sheetbend", reason="Sheetbend dependency is not installed")
from sheetbend.source import Source


def config_file(tmp_path, *, local=True):
    path = tmp_path / "sheetbend.toml"
    text = '''schema_version = 2
default_source = "institution"
[sources.institution]
protocol = "openai-compatible"
base_url = "https://institution.example.invalid/v1"
default_model = "test"
scope = "institutional"
organization = "test"
auth = {type = "none"}
[sources.institution.models.test.capabilities]
json_schema = true
'''
    if local:
        text += '''
[sources.laptop]
protocol = "openai-compatible"
base_url = "http://127.0.0.1:12345/v1"
default_model = "test"
scope = "local"
auth = {type = "none"}
[sources.laptop.models.test.capabilities]
json_schema = true
[sources.laptop.models.test.reasoning]
control = "fixed-off"
'''
    path.write_text(text)
    return path


def test_local_wins_and_response_is_consumed_in_context(recipe, table, tmp_path, monkeypatch):
    seen = []
    state = {"open": False}
    @contextmanager
    def connect(self, model, **kwargs):
        seen.append((self.name, model, kwargs))
        state["open"] = True
        def prompt(text, **options):
            assert options["stream"] is False
            assert options["schema"]["properties"]["runtime"]["const"] == "python-stdlib-v1"
            assert "REQUEST DATA" in text
            def consume():
                assert state["open"]
                return json.dumps(recipe.to_dict())
            return SimpleNamespace(text=consume)
        try:
            yield SimpleNamespace(prompt=prompt)
        finally:
            state["open"] = False
    monkeypatch.setattr(Source, "_connect", connect)
    compiler = SheetbendCompiler(config_path=config_file(tmp_path), allow_institutional=True, reasoning="off")
    result = compiler.generate(Request.bind("filter", input_paths=[table], output_path="-"), [])
    assert seen[0][0] == "laptop"
    assert seen[0][2] == {"reasoning": "off", "application": "any", "tool": "compile"}
    assert result.provenance["scope"] == "local" and result.recipe.digest == recipe.digest
    assert not state["open"]


def test_nonlocal_requires_explicit_permission(recipe, table, tmp_path, monkeypatch):
    path = config_file(tmp_path, local=False)
    def forbidden(*args, **kwargs):
        pytest.fail("Disallowed source must not connect")
    monkeypatch.setattr(Source, "_connect", forbidden)
    compiler = SheetbendCompiler(config_path=path)
    with pytest.raises(GenerationError, match="No configured"):
        compiler.generate(Request.bind("filter", input_paths=[table], output_path="-"), [])


def test_model_failure_never_falls_back_or_exposes_provider_body(table, tmp_path, monkeypatch):
    seen = []
    @contextmanager
    def connect(self, model, **kwargs):
        seen.append(self.name)
        raise RuntimeError("PRIVATE_PROVIDER_RESPONSE")
        yield
    monkeypatch.setattr(Source, "_connect", connect)
    compiler = SheetbendCompiler(config_path=config_file(tmp_path), allow_institutional=True)
    with pytest.raises(GenerationError) as caught:
        compiler.generate(Request.bind("filter", input_paths=[table], output_path="-"), [])
    assert seen == ["laptop"] and "PRIVATE_PROVIDER_RESPONSE" not in str(caught.value)
