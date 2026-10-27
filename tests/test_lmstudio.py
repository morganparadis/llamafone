import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os, sys, json, types, threading, tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, _os.path.join(_REPO, "src"))
pkg = types.ModuleType("llamafone")
pkg.__path__ = [_os.path.join(_REPO, "src", "llamafone")]
sys.modules["llamafone"] = pkg

# Keep test runs out of the real Documents log / prompt file.
tmp = tempfile.mkdtemp()
os.environ["USERPROFILE"] = tmp
os.environ["HOME"] = tmp
os.makedirs(os.path.join(tmp, "Documents"))

import llamafone.config as config
import llamafone.api_client as api

MODE = {"chat": "ok"}
SEEN = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "llama-3.2-3b-instruct", "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        SEEN["path"] = self.path
        SEEN["body"] = json.loads(self.rfile.read(n) or b"{}")
        m = MODE["chat"]
        if m == "ok":
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": "pong"}}]})
        elif m == "empty":
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": ""}}]})
        elif m == "context":
            self._send(400, {"error": "Trying to keep the first 7012 tokens when context the overflows. However, the model is loaded with context length of only 4096 tokens"})
        elif m == "noload":
            self._send(404, {"error": {"message": "No models loaded. Please load a model in the developer page."}})


srv = HTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

cfg_dir = os.path.join(tmp, "Documents", "Electronic Arts", "The Sims 4", "Mods")
os.makedirs(cfg_dir)
with open(os.path.join(cfg_dir, "llamafone.cfg"), "w", encoding="utf-8") as f:
    f.write(config._DEFAULT_CONFIG_TEMPLATE
            .replace("provider = claude", "provider = lmstudio")
            .replace("lmstudio_endpoint = http://localhost:1234", f"lmstudio_endpoint = http://127.0.0.1:{port}/v1")
            .replace("model = claude-haiku-5-5", "model = llama-3.2-3b-instruct")
            .replace("fast_model = claude-haiku-5-5", "fast_model = llama-3.2-3b-instruct"))
config.__file__ = os.path.join(tmp, "x", "Llamafone.ts4script", "llamafone", "config.pyc")
config._mods_folder_cache = None
config._config = None
config._last_config_path = None

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(extra)))
    if not cond:
        fails += 1


def call():
    done = threading.Event()
    res = {}

    def cb(t, e):
        res["t"], res["e"] = t, e
        done.set()
    api.call_ai_async([{"role": "user", "content": "hi"}], system="sys", use_fast_model=True, callback=cb)
    done.wait(20)
    return res.get("t"), res.get("e")


check("provider lmstudio", config.get_provider() == "lmstudio")
check("configured without key", config.is_configured())
h = api.check_lmstudio_health()
check("health reachable (endpoint with /v1 accepted)", h["reachable"], h)
check("health lists model", h["models"] == ["llama-3.2-3b-instruct"], h)

MODE["chat"] = "ok"
t, e = call()
check("chat ok", t == "pong" and e is None, (t, e))
check("hits /v1/chat/completions", SEEN.get("path") == "/v1/chat/completions", SEEN.get("path"))
check("system message first", SEEN["body"]["messages"][0] == {"role": "system", "content": "sys"}, SEEN["body"]["messages"][:1])
check("model sent", SEEN["body"]["model"] == "llama-3.2-3b-instruct")

MODE["chat"] = "empty"
t, e = call()
check("empty reply -> error", e and "empty reply" in e, (t, e))

MODE["chat"] = "context"
t, e = call()
check("context error -> plain fix", e == "LM Studio: " + api._LMSTUDIO_CONTEXT_HINT and "16384" in e, e)

MODE["chat"] = "noload"
t, e = call()
check("not loaded -> plain fix", e and e.startswith("LM Studio: No model is loaded"), e)

log = open(os.path.join(tmp, "Documents", "Llamafone_Log.txt"), encoding="utf-8").read()
check("failures logged with timing", log.count("[api] lmstudio/llama-3.2-3b-instruct failed after") == 3, log)

srv.shutdown()
srv.server_close()
h = api.check_lmstudio_health(f"http://127.0.0.1:{port}")
check("server off -> friendly error", (not h["reachable"]) and "Developer tab" in (h["error"] or ""), h)

check("local timeout 300", api._LOCAL_TIMEOUT == 300)
real = 'Engine protocol predict stream returned an error: {"code":500,"message":"Context size has been exceeded.","type":"server_error"}'
check("real logged error -> plain fix, no JSON", api._clean_lmstudio_error(real) == api._LMSTUDIO_CONTEXT_HINT)
print("FAILS", fails)
