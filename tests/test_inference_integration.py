"""Actual Sheetbend + LLM + SDK over a loopback-only server; no model-quality claim."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread

import pytest

from sheetbend_any import Request, SheetbendCompiler

if os.environ.get("ANY_REQUIRE_INFERENCE") == "1":
    import llm  # noqa: F401
    import openai  # noqa: F401
else:
    pytest.importorskip("llm", reason="Actual LLM/SDK integration is not installed")
    pytest.importorskip("openai", reason="Actual SDK integration is not installed")


@pytest.mark.inference
def test_actual_stack_uses_one_structured_request(recipe, table, tmp_path):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            payload = json.dumps({
                "id": "test", "object": "chat.completion", "created": 1, "model": "test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(recipe.to_dict())}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
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
        result = SheetbendCompiler(config_path=path).generate(
            Request.bind("filter", input_paths=[table], output_path="-"), [],
        )
        assert result.recipe.digest == recipe.digest
        assert len(seen) == 1 and seen[0][0] == "/v1/chat/completions"
        assert seen[0][1]["response_format"]["type"] == "json_schema"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
