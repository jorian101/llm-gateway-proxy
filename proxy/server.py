#!/usr/bin/env python3
"""
Gateway Proxy - Dual Combo (example)
- 5 top models: kimi-k3 → deepseek-v4-pro-0813 → deepseek-v4-flash-0731 → glm-5.2 → minimax-m3
- Shared _inflight per conn_id (1 per nvidia account), nvidia-first, 4th session waits
- _disabled with TTL, retry before first byte, never hang
- Config via proxy.config.json if present
"""
import os, json, logging, threading, time, uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import requests

# --- Config load ---
DEFAULT_TARGET = "http://127.0.0.1:20128/v1"
DEFAULT_API_KEY = os.getenv("GATEWAY_API_KEY", os.getenv("OMNIROUTE_API_KEY", "<GATEWAY_API_KEY>"))
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_cfg = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH) as f: _cfg = json.load(f)
    except Exception: pass

TARGET = _cfg.get("target", DEFAULT_TARGET)
API_KEY = _cfg.get("api_key", DEFAULT_API_KEY)

NVIDIA_CONNS = _cfg.get("nvidia_conns", {
    "nim-1": "<CONN_ID_1>",
    "nim-2": "<CONN_ID_2>",
    "nim-3": "<CONN_ID_3>",
})
VISION_CONNS = _cfg.get("vision_conns", {
    "openrouter": "<CONN_ID_V1>",
    "groq": "<CONN_ID_V2>",
    "gemini-1": "<CONN_ID_V3>",
    "gemini-2": "<CONN_ID_V4>",
    "mistral": "<CONN_ID_V5>",
})

_http_session = requests.Session()
_http_session.headers.update({"Connection": "keep-alive"})

# Hierarchies loaded from proxy/hierarchies.json (gitignored) if present,
# else fall back to proxy/hierarchies.example.json (committed). Compact format:
#   { "nvidia-start": [ { "model": "...", "weight": 90, "conns": ["nim-1","nim-2","nim-3"] }, ... ] }
# Each entry expands to one tuple per conn alias. Aliases map to conn IDs via
# NVIDIA_CONNS / VISION_CONNS. To add/remove a model, edit hierarchies.json.
HIERARCHIES_FILE = os.path.join(os.path.dirname(__file__), "hierarchies.json")
EXAMPLE_FILE = os.path.join(os.path.dirname(__file__), "hierarchies.example.json")

CONN_BY_ALIAS = {**NVIDIA_CONNS, **VISION_CONNS}

def _provider_for_alias(alias):
    return "nvidia" if alias.startswith("nim-") else alias.split("-")[0]

def _expand_hierarchy(entries):
    out = []
    for e in entries:
        for alias in e["conns"]:
            out.append((_provider_for_alias(alias), CONN_BY_ALIAS[alias], e["model"], e["weight"]))
    return out

def _load_hierarchies(path):
    with open(path) as f: data = json.load(f)
    return {name: _expand_hierarchy(entries) for name, entries in data.items()}

_hiers_path = HIERARCHIES_FILE if os.path.exists(HIERARCHIES_FILE) else EXAMPLE_FILE
HIERARCHIES = _load_hierarchies(_hiers_path)
PORT_COMBO = _cfg.get("port_combo", {20129: "nvidia-start", 20133: "nvidia-vision"})
# json keys are strings, normalize
PORT_COMBO = {int(k): v for k, v in PORT_COMBO.items()}

_conn_id_to_alias = {v: k for k, v in NVIDIA_CONNS.items()}
_conn_id_to_alias.update({v: k for k, v in VISION_CONNS.items()})

_inflight_lock = threading.RLock()
_inflight = {}
_disabled_lock = threading.RLock()
_disabled = {}  # {model_key: (ts, reason)}
DISABLED_TTL = 600
WAIT_SHORT_SEC = 3  # wait for nvidia slot before falling to externo
EXTERNAL_PROVIDERS = {"openrouter", "groq", "gemini", "mistral"}
_logs_lock = threading.RLock()
_recent_logs = []
_start_time = time.time()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)
DROP_HEADERS = {'content-length', 'transfer-encoding', 'connection', 'host'}

def _add_log(level, msg):
    with _logs_lock:
        _recent_logs.append({"timestamp": time.time(), "level": level, "message": msg})
        if len(_recent_logs) > 120: _recent_logs.pop(0)

def _model_key(provider, conn_id, model): return f"{provider}:{conn_id}:{model}"

def _is_disabled(key):
    with _disabled_lock:
        if key not in _disabled: return False
        ts, _ = _disabled[key]
        if time.time() - ts > DISABLED_TTL:
            del _disabled[key]
            return False
        return True

def _mark_disabled(key, reason):
    with _disabled_lock:
        _disabled[key] = (time.time(), reason)
    _add_log("WARN", f"DISABLED {key} reason={reason} ttl={DISABLED_TTL}s")

def pick_free_connection(combo, tried=None):
    tried = tried or set()
    hierarchy = HIERARCHIES.get(combo, [])
    # first pass: nvidia only, skip disabled and tried and busy
    def find(nvidia_only):
        with _inflight_lock:
            busy = set(_inflight.keys())
        for provider, conn_id, model, weight in hierarchy:
            if nvidia_only and provider in EXTERNAL_PROVIDERS: continue
            if not nvidia_only and provider not in EXTERNAL_PROVIDERS: continue
            key = _model_key(provider, conn_id, model)
            if key in tried: continue
            if _is_disabled(key): continue
            if conn_id in busy: continue
            return conn_id, model, provider, key
        return None
    # nvidia first
    r = find(nvidia_only=True)
    if r: return r
    # no nvidia free: wait short for nvidia slot
    deadline = time.time() + WAIT_SHORT_SEC
    while time.time() < deadline:
        time.sleep(0.5)
        r = find(nvidia_only=True)
        if r:
            _add_log("INFO", f"WAIT nvidia slot freed for {combo} -> {r[1]}")
            return r
    # still none, try externo if free
    r = find(nvidia_only=False)
    if r:
        with _inflight_lock:
            nvidia_busy = sum(1 for cid in _inflight if _conn_id_to_alias.get(cid) in NVIDIA_CONNS)
        if nvidia_busy >= 3:
            _add_log("INFO", f"FALLBACK externo {combo} -> {r[1]} (nvidia full)")
            return r
        # if we have <3 nvidia busy but still no nvidia model free (disabled), also fallback
        _add_log("INFO", f"FALLBACK externo {combo} -> {r[1]} (nvidia disabled/busy)")
        return r
    return None

def acquire_connection(conn_id, model, session_id, combo, provider):
    with _inflight_lock:
        if conn_id in _inflight: return False
        _inflight[conn_id] = {"since": time.time(), "model": model, "session": session_id, "alias": _conn_id_to_alias.get(conn_id, "unknown"), "combo": combo, "provider": provider}
        _add_log("INFO", f"ACQUIRED {combo} {_conn_id_to_alias.get(conn_id, conn_id)} model={model} session={session_id}")
        return True

def release_connection(conn_id):
    with _inflight_lock:
        if conn_id in _inflight:
            info = _inflight.pop(conn_id)
            dur = time.time() - info["since"]
            _add_log("INFO", f"RELEASED {info['combo']} {_conn_id_to_alias.get(conn_id, conn_id)} model={info['model']} dur={dur:.1f}s")

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class ComboProxyHandler(BaseHTTPRequestHandler):
    def _get_combo_for_port(self): return PORT_COMBO.get(self.server.server_port, "nvidia-start")
    def _get_session_id(self): return self.headers.get('X-Session-Id') or self.headers.get('X-Session-ID') or f"anon-{uuid.uuid4().hex[:8]}"
    def _forward_request(self, method):
        combo = self._get_combo_for_port()
        session_id = self._get_session_id()
        content_length = int(self.headers.get('Content-Length', 0)) if method == 'POST' else 0
        body0 = self.rfile.read(content_length) if method == 'POST' else None
        wants_stream0 = False
        if method == 'POST' and body0:
            try: wants_stream0 = json.loads(body0).get('stream', False) == True
            except: pass
        tried = set()
        last_err = None
        for attempt in range(3):
            picked = pick_free_connection(combo, tried)
            if not picked:
                self._send_json(503, {"error": {"message": f"All connections busy for {combo}. Retry shortly.", "type": "all_connections_busy", "code": "ALL_BUSY", "retry_after": 3}}, headers={"Retry-After": "3", "X-Combo": combo, "X-Inflight-Count": str(len(_inflight))})
                return
            conn_id, model, provider, key = picked
            tried.add(key)
            if not acquire_connection(conn_id, model, session_id, combo, provider):
                continue
            # build body with model
            body = body0
            wants_stream = wants_stream0
            if method == 'POST' and body:
                try:
                    req = json.loads(body)
                    wants_stream = req.get('stream', False) == True
                    req['model'] = model
                    body = json.dumps(req).encode()
                except: pass
            headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP_HEADERS}
            headers['Authorization'] = f'Bearer {API_KEY}'
            headers['X-Session-Id'] = session_id
            start_total = time.time()
            try:
                resp = _http_session.request(method, f"{TARGET}{self.path}", data=body, headers=headers, timeout=60, stream=wants_stream)
                # detect model disabled before streaming
                if resp.status_code >= 400:
                    b = b""
                    try: b = resp.content[:2000]
                    except: pass
                    txt = b.decode(errors='ignore').lower()
                    is_model_err = resp.status_code == 404 or "model" in txt and ("not found" in txt or "does not exist" in txt or "no such model" in txt)
                    if is_model_err:
                        _mark_disabled(key, f"{resp.status_code} {txt[:120]}")
                        _add_log("WARN", f"MODEL ERROR {combo} {model} -> next (attempt {attempt+1})")
                        # release and retry
                        release_connection(conn_id)
                        last_err = (resp.status_code, b)
                        time.sleep(0.3)
                        continue
                    # other error: surface to CLI
                    self.send_response(resp.status_code)
                    for k, v in resp.headers.items():
                        if k.lower() not in ('transfer-encoding', 'connection'): self.send_header(k, v)
                    self.send_header('X-Proxy-Combo', combo); self.send_header('X-Proxy-Connection', _conn_id_to_alias.get(conn_id, conn_id)); self.send_header('X-Proxy-Model', model); self.send_header('X-Proxy-Provider', provider); self.send_header('X-Session-Id', session_id)
                    self.end_headers()
                    if wants_stream:
                        for chunk in resp.iter_content(chunk_size=4096):
                            if chunk:
                                try: self.wfile.write(chunk); self.wfile.flush()
                                except: break
                    else:
                        self.wfile.write(resp.content); self.wfile.flush()
                    _add_log("WARN", f"ERROR {combo} {model} {resp.status_code}")
                    return
                # success: proxy response
                self.send_response(resp.status_code)
                for k, v in resp.headers.items():
                    if k.lower() not in ('transfer-encoding', 'connection'): self.send_header(k, v)
                self.send_header('X-Proxy-Combo', combo); self.send_header('X-Proxy-Connection', _conn_id_to_alias.get(conn_id, conn_id)); self.send_header('X-Proxy-Model', model); self.send_header('X-Proxy-Provider', provider); self.send_header('X-Proxy-Connect-Ms', "0"); self.send_header('X-Session-Id', session_id)
                self.end_headers()
                if wants_stream:
                    for chunk in resp.iter_content(chunk_size=4096):
                        if chunk:
                            try: self.wfile.write(chunk); self.wfile.flush()
                            except: break
                else:
                    self.wfile.write(resp.content); self.wfile.flush()
                total = time.time() - start_total
                final_model = resp.headers.get('X-OmniRoute-Model', model)
                if total > 3: _add_log("INFO", f"🔄 [{combo}] {_conn_id_to_alias.get(conn_id, conn_id)} {total:.1f}s model={final_model}")
                else: _add_log("INFO", f"✅ [{combo}] {_conn_id_to_alias.get(conn_id, conn_id)} {total:.2f}s model={final_model}")
                return
            except Exception as e:
                msg = str(e).lower()
                is_net = "timeout" in msg or "read timed out" in msg or "connection" in msg
                _add_log("ERROR", f"Proxy error {combo} {model} attempt {attempt+1}: {e}")
                # if we haven't streamed, retry with next model; else surface
                if is_net and attempt < 2:
                    release_connection(conn_id)
                    # short backoff, don't disable on timeout (rate limit)
                    time.sleep(0.5)
                    continue
                try: self.send_error(502, str(e))
                except: pass
                return
            finally:
                # release if not already and we succeeded/failed terminally
                # if we continued (retry), already released
                # check if still held
                with _inflight_lock:
                    if conn_id in _inflight and _inflight[conn_id].get("session") == session_id:
                        # only release if we are not retrying (retry already released)
                        # detect retry: if we are in retry path we already returned continue, so this finally would release again
                        # use flag: if attempt <2 and is_model_err/is_net, we already released, so skip
                        pass
                # ensure release on terminal paths
                if conn_id in _inflight:
                    # check if session still ours (not race)
                    with _inflight_lock:
                        if conn_id in _inflight and _inflight[conn_id].get("session") == session_id:
                            # if we are about to retry, we already released; detect by last_err set
                            # simple: if we are in retry branch we would have continued before reaching here? actually finally always runs
                            # so need to avoid double release: only release if not continuing
                            # we detect continuing by checking if we set last_err and will loop
                            # simpler: release here always, retry loop will re-acquire next
                            pass
                release_connection(conn_id)
                # if we reached here via retry continue, the loop will continue; else return already
                if last_err and attempt < 2:
                    continue
        # if all attempts exhausted
        if last_err:
            code, body = last_err
            self._send_json(code, {"error": {"message": body.decode(errors='ignore')[:500], "code": "MODEL_DISABLED"}})

    def _send_json(self, status, data, headers=None):
        self.send_response(status); self.send_header("Content-Type", "application/json")
        if headers:
            for k, v in headers.items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        if self.path == '/logs': return self._handle_logs()
        if self.path == '/health': return self._handle_health()
        if self.path == '/status': return self._handle_status()
        self._forward_request('POST')
    def do_GET(self):
        if self.path == '/logs': return self._handle_logs()
        if self.path == '/health': return self._handle_health()
        if self.path == '/status': return self._handle_status()
        self._forward_request('GET')
    def _handle_health(self):
        combo = self._get_combo_for_port()
        with _inflight_lock: c = len(_inflight)
        self._send_json(200, {"status": "ok", "combo": combo, "inflight_connections": c, "max_nvidia_connections": len(NVIDIA_CONNS), "uptime_sec": round(time.time() - _start_time, 1), "timestamp": time.time()})
    def _handle_status(self):
        combo = self._get_combo_for_port()
        hierarchy = HIERARCHIES.get(combo, [])
        with _inflight_lock: inflight_copy = dict(_inflight)
        with _disabled_lock: disabled_copy = dict(_disabled)
        now = time.time()
        connections = []
        for alias, conn_id in NVIDIA_CONNS.items():
            info = inflight_copy.get(conn_id)
            if info: connections.append({"id": alias, "conn_id": conn_id, "provider": info.get("provider","nvidia"), "in_use": True, "since_sec": round(now-info["since"],1), "model": info["model"], "session": info["session"], "combo": info.get("combo")})
            else: connections.append({"id": alias, "conn_id": conn_id, "provider": "nvidia", "in_use": False, "since_sec": 0, "model": None, "session": None, "combo": None})
        for alias, conn_id in VISION_CONNS.items():
            info = inflight_copy.get(conn_id)
            if info: connections.append({"id": alias, "conn_id": conn_id, "provider": info.get("provider", alias), "in_use": True, "since_sec": round(now-info["since"],1), "model": info["model"], "session": info["session"], "combo": info.get("combo")})
            else: connections.append({"id": alias, "conn_id": conn_id, "provider": alias, "in_use": False, "since_sec": 0, "model": None, "session": None, "combo": None})
        disabled_list = [{"key": k, "since_sec": round(now-ts,1), "reason": r[:120]} for k,(ts,r) in disabled_copy.items() if now-ts < DISABLED_TTL]
        self._send_json(200, {"combo": combo, "uptime_sec": round(time.time()-_start_time,1), "timestamp": now, "connections": connections, "models_in_use": {info["model"]: list(inflight_copy.values()).count(info) for info in inflight_copy.values()}, "hierarchy_size": len(hierarchy), "disabled_models": disabled_list, "inflight_count": len(inflight_copy)})
    def _handle_logs(self):
        with _logs_lock: logs = _recent_logs[-80:]
        self._send_json(200, {"logs": logs})
    def log_message(self, *args): pass

def run_proxy(port):
    server = ThreadedHTTPServer(('127.0.0.1', port), ComboProxyHandler)
    combo = PORT_COMBO[port]
    logger.info(f"Proxy listening on :{port} combo={combo} -> {TARGET}")
    server.serve_forever()

if __name__ == '__main__':
    threads = []
    for port in sorted(PORT_COMBO.keys()):
        t = threading.Thread(target=run_proxy, args=(port,), daemon=True); t.start(); threads.append(t); logger.info(f"Started {port} ({PORT_COMBO[port]})")
    logger.info(f"Proxy v4 ready. Combos: {list(PORT_COMBO.values())}. Endpoints: /health /logs /status")
    try:
        while True: time.sleep(60)
    except KeyboardInterrupt: logger.info("Shutting down...")
