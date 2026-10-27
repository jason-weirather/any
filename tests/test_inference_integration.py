"""Actual Sheetbend/LLM/SDK protocol and native runtime; no model-quality claim."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread

import pytest

from sheetbend_any import Limits, Recipe, RecipeStore, Request, Sandbox, SheetbendCompiler
from sheetbend_any.cli import main
from sheetbend_any.errors import AnyError, GenerationError

if os.environ.get("ANY_REQUIRE_INFERENCE") == "1":
    import llm  # noqa: F401
    import openai  # noqa: F401
else:
    pytest.importorskip("llm", reason="Actual LLM/SDK integration is not installed")
    pytest.importorskip("openai", reason="Actual SDK integration is not installed")


def final_document(recipe):
    document = recipe.to_dict()
    document.pop("runtime")
    document.update(
        schema_version=2, kind="python", pipeline=[],
        fallback_reason="CSV quoting and strict numeric validation need a format-aware implementation.",
    )
    return {"action": "final", "commands": [], "recipe": document}


@contextmanager
def loopback_source(tmp_path, responses):
    """Serve prescribed protocol responses with the actual SDK retries disabled by Sheetbend."""
    seen = []
    queue = iter(responses)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({
                "path": self.path, "body": body,
                "authorization_present": "Authorization" in self.headers,
            })
            try:
                answer = next(queue)
            except StopIteration:
                answer = (500, {"error": {"message": "Unexpected extra model request"}})
            if isinstance(answer, tuple):
                status, response_body = answer
            else:
                status = 200
                response_body = {
                    "id": "test", "object": "chat.completion", "created": 1, "model": "test",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": answer if isinstance(answer, str) else json.dumps(answer)},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            payload = json.dumps(response_body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = tmp_path / "sheetbend.toml"
        path.write_text(f'''schema_version = 2
[sources.test]
protocol = "openai-compatible"
base_url = "http://127.0.0.1:{server.server_port}/v1"
default_model = "test"
scope = "local"
auth = {{type = "none"}}
[sources.test.models.test.capabilities]
json_schema = true
''')
        yield path, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.inference
def test_actual_stack_uses_one_structured_request(recipe, table, tmp_path):
    envelope = final_document(recipe)
    with loopback_source(tmp_path, [envelope]) as (path, seen):
        result = SheetbendCompiler(config_path=path).generate(
            Request.bind("filter", input_paths=[table], output_path="-"), [],
        )
    assert result.recipe.digest == Recipe(envelope["recipe"]).digest
    assert result.provenance["planning_attempts"] == 1
    assert len(seen) == 1 and seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["body"]["response_format"]["type"] == "json_schema"
    assert not seen[0]["authorization_present"]


@pytest.mark.inference
def test_actual_stack_lookup_then_invalid_json_then_repair(recipe, table, tmp_path):
    envelope = final_document(recipe)
    responses = [
        {"action": "tools", "commands": ["sort"], "recipe": None},
        "{invalid JSON",
        envelope,
    ]
    with loopback_source(tmp_path, responses) as (path, seen):
        result = SheetbendCompiler(config_path=path, max_attempts=3).generate(
            Request.bind("filter", input_paths=[table], output_path="-"), [],
        )
    assert result.recipe.digest == Recipe(envelope["recipe"]).digest
    assert result.provenance["planning_attempts"] == 3 and len(seen) == 3
    assert all(item["body"]["model"] == "test" for item in seen)
    assert all(item["body"]["response_format"]["type"] == "json_schema" for item in seen)
    second = json.dumps(seen[1]["body"]["messages"])
    third = json.dumps(seen[2]["body"]["messages"])
    assert "tool_results" in second and "features_verified" in second
    assert "validation_error" in third and "valid JSON document" in third


@pytest.mark.inference
def test_actual_http_error_is_not_retried_and_debug_body_is_redacted(table, tmp_path):
    responses = [(400, {"error": {
        "message": "Unsupported response_format; api_key='FAKE_PROVIDER_SECRET'",
        "type": "invalid_request_error", "param": "response_format", "code": "unsupported_schema",
    }})]
    events = []
    with loopback_source(tmp_path, responses) as (path, seen):
        with pytest.raises(GenerationError) as caught:
            SheetbendCompiler(
                config_path=path, max_attempts=4, debug_level=3, on_event=events.append,
            ).generate(Request.bind("filter", input_paths=[table], output_path="-"), [])
    assert len(seen) == 1
    assert "HTTP 400" in str(caught.value) and "No automatic retry" in str(caught.value)
    assert "Unsupported response_format" in "\n".join(events)
    assert "FAKE_PROVIDER_SECRET" not in str(caught.value) + "\n".join(events)
    assert caught.value.__cause__ is not None


@pytest.mark.inference
@pytest.mark.sandbox
def test_actual_cli_plans_caches_preflights_and_executes_native_pipeline(tmp_path, cli_runner):
    """One complete path through real SDK requests, plan bindings and OS confinement."""
    try:
        report = Sandbox(limits=Limits(timeout_seconds=5)).doctor()
        assert report["shell_canaries_passed"]
    except (AnyError, OSError) as exc:
        if os.environ.get("ANY_REQUIRE_SANDBOX") == "1":
            pytest.fail(f"Required native sandbox did not pass: {exc}")
        pytest.skip(f"Actual native sandbox unavailable: {exc}")

    document = json.loads(
        (Path(__file__).parents[1] / "examples/sort-unique.recipe.json").read_text()
    )
    source = tmp_path / "lines with spaces.txt"
    source.write_text("beta\nalpha\nbeta\n")
    destination = tmp_path / "unique.txt"
    cache = tmp_path / "cache"
    responses = [
        {"action": "tools", "commands": ["sort", "uniq"], "recipe": None},
        {"action": "final", "commands": [], "recipe": document},
    ]
    with loopback_source(tmp_path, responses) as (config, seen):
        options = [
            "Return unique complete lines in bytewise sorted order.",
            "-i", str(source), "-o", str(destination),
            "--config-path", str(config), "--cache-directory", str(cache),
            "--planning-attempts", "2",
        ]
        planned = cli_runner.invoke(main, [*options, "--plan-only"])
        assert planned.exit_code == 0, planned.output
        assert planned.stdout == "" and "Plan only" in planned.stderr
        assert len(seen) == 2 and not destination.exists()
        assert "tool_results" in json.dumps(seen[1]["body"]["messages"])
        store = RecipeStore(cache)
        recipe = Recipe(document)
        assert store.get(recipe.digest).digest == recipe.digest
        assert store.runs() == []

        checked = cli_runner.invoke(main, [*options, "--dryrun"])
        assert checked.exit_code == 0, checked.output
        assert checked.stdout == "" and "Preflight passed" in checked.stderr
        assert len(seen) == 2 and not destination.exists() and store.runs() == []

        executed = cli_runner.invoke(main, [*options, "-v"])
        assert executed.exit_code == 0, executed.output
        assert executed.stdout == "" and destination.read_bytes() == b"alpha\nbeta\n"
        assert source.read_text() == "beta\nalpha\nbeta\n"
        assert len(seen) == 2  # Neither cached preflight nor execution called a model.
        records = store.runs()
        assert len(records) == 1
        record = records[0]
        assert record["published"] is True and record["execution_kind"] == "shell"
        assert record["tests_passed"] == 1 and record["recipe_digest"] == recipe.digest
        assert record["backend"] in {"bubblewrap-seccomp", "macos-seatbelt"}
        assert {"bash", "sort", "uniq"}.issubset(record["tools"])
        assert record["tools"]["sort"]["identity"]
        assert record["command"] in executed.stderr and "any_pipeline_status" in record["command"]
        assert "Bound plan" not in executed.stderr and "Preflight candidate" not in executed.stderr

        history = cli_runner.invoke(main, ["--cache-directory", str(cache), "--history"])
        assert history.exit_code == 0, history.output
        assert json.loads(history.stdout) == records and history.stderr == ""
        assert len(seen) == 2
