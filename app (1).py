"""
CodeLab — Merged Full-Stack Application
Flask backend + serves the HTML frontend at /

Stack:
  - Flask + Flask-SocketIO + gevent  (real-time collaboration)
  - Redis (with in-memory fallback for dev)
  - PyJWT + bcrypt  (auth)
  - Anthropic Claude API  (AI assistant)
  - Subprocess sandbox  (code execution)

Run:
  pip install -r requirements.txt
  python app.py

Optional env vars (.env):
  JWT_SECRET=...
  REDIS_URL=redis://localhost:6379/0
  ANTHROPIC_API_KEY=...
  EXEC_TIMEOUT=5
  PORT=5000
"""

import os, uuid, subprocess, tempfile, time, json, re
from functools import wraps
from pathlib import Path



import bcrypt
import jwt
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
SECRET_KEY   = os.getenv("JWT_SECRET",   "CodeLab-dev-secret-CHANGE-IN-PROD")
REDIS_URL    = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
EXEC_TIMEOUT = int(os.getenv("EXEC_TIMEOUT", "5"))
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SECRET_KEY"] = SECRET_KEY

socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

# ─────────────────────────────────────────────────────────────────────────────
# Redis / in-memory store
# ─────────────────────────────────────────────────────────────────────────────
try:
    import redis as _redis
    rdb = _redis.from_url(REDIS_URL, decode_responses=True)
    rdb.ping()
    USE_REDIS = True
    print("[Redis] Connected ✓")
except Exception as exc:
    print(f"[Redis] Not reachable ({exc}) — using in-memory dict")
    rdb = None
    USE_REDIS = False

_mem: dict = {}

def kv_get(key):
    if USE_REDIS:
        raw = rdb.get(key)
        return json.loads(raw) if raw else None
    return _mem.get(key)

def kv_set(key, value, ex=86400):
    if USE_REDIS:
        rdb.set(key, json.dumps(value), ex=ex)
    else:
        _mem[key] = value

def kv_del(key):
    if USE_REDIS: rdb.delete(key)
    else: _mem.pop(key, None)

# ─────────────────────────────────────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────────────────────────────────────
def make_token(user_id, username):
    return jwt.encode({
        "user_id": user_id, "username": username,
        "iat": int(time.time()), "exp": int(time.time()) + 86400 * 7,
    }, SECRET_KEY, algorithm="HS256")

def decode_token(token):
    try: return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except: return None

def jwt_required(f):
    @wraps(f)
    def _inner(*args, **kwargs):
        auth  = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        data  = decode_token(token)
        if not data: return jsonify({"error": "Invalid or missing token"}), 401
        request.user = data
        return f(*args, **kwargs)
    return _inner

# ─────────────────────────────────────────────────────────────────────────────
# Role / Permissions
# ─────────────────────────────────────────────────────────────────────────────
ROLE_PERMISSIONS = {
    "owner":     {"can_edit":True,"can_run":True,"can_manage_members":True,"can_update_settings":True,"can_delete_session":True,"can_share":True,"can_view_terminal":True,"can_view_output":True,"can_chat":True},
    "developer": {"can_edit":True,"can_run":True,"can_manage_members":False,"can_update_settings":False,"can_delete_session":False,"can_share":False,"can_view_terminal":True,"can_view_output":True,"can_chat":True},
    "tester":    {"can_edit":False,"can_run":True,"can_manage_members":False,"can_update_settings":False,"can_delete_session":False,"can_share":False,"can_view_terminal":True,"can_view_output":True,"can_chat":True},
    "client":    {"can_edit":False,"can_run":False,"can_manage_members":False,"can_update_settings":False,"can_delete_session":False,"can_share":False,"can_view_terminal":False,"can_view_output":True,"can_chat":False},
}

def get_user_role(session_obj, user_id):
    if session_obj.get("owner_id") == user_id: return "owner"
    return session_obj.get("members", {}).get(user_id, {}).get("role", "client")

def has_permission(session_obj, user_id, perm):
    return ROLE_PERMISSIONS.get(get_user_role(session_obj, user_id), {}).get(perm, False)

# ─────────────────────────────────────────────────────────────────────────────
# Language / Execution config
# ─────────────────────────────────────────────────────────────────────────────
LANG_CONFIG = {
    "python":     {"ext": ".py",   "run": lambda s: ["python3", s]},
    "javascript": {"ext": ".js",   "run": lambda s: ["node", s]},
    "cpp":        {"ext": ".cpp",  "compile": lambda s,o: ["g++","-std=c++17","-O2",s,"-o",o], "run": lambda s: [s.replace(".cpp","")]},
    "java":       {"ext": ".java", "compile": lambda s,_: ["javac",s], "run": lambda s: ["java","-cp",os.path.dirname(s),"Main"]},
    "rust":       {"ext": ".rs",   "compile": lambda s,o: ["rustc",s,"-o",o], "run": lambda s: [s.replace(".rs","")]},
}

STARTER = {
    "python": "# CodeLab — Python session\n\ndef greet(name: str) -> str:\n    return f'Hello, {name}!'\n\nprint(greet('CodeLab'))\n",
    "javascript": "// CodeLab — JavaScript session\nconst greet = name => `Hello, ${name}!`;\nconsole.log(greet('CodeLab'));\n",
    "cpp": '#include <iostream>\nusing namespace std;\nint main() {\n    cout << "Hello, CodeLab!" << endl;\n    return 0;\n}\n',
    "java": 'public class Main {\n    public static void main(String[] args) {\n        System.out.println("Hello, CodeLab!");\n    }\n}\n',
    "rust": 'fn main() {\n    println!("Hello, CodeLab!");\n}\n',
}

# ─────────────────────────────────────────────────────────────────────────────
# Serve Frontend (index.html)
# ─────────────────────────────────────────────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent

@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

# ─────────────────────────────────────────────────────────────────────────────
# REST — Health
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"status": "ok", "redis": USE_REDIS, "ai": bool(ANTHROPIC_KEY)})

# ─────────────────────────────────────────────────────────────────────────────
# REST — Auth
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/auth/register")
def register():
    body = request.get_json(force=True) or {}
    username = body.get("username","").strip()
    password = body.get("password","")
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    if len(username) < 3 or len(password) < 6:
        return jsonify({"error": "username ≥3 chars, password ≥6 chars"}), 422
    if kv_get(f"user:{username}"):
        return jsonify({"error": "Username already taken"}), 409
    hashed  = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_id = str(uuid.uuid4())
    kv_set(f"user:{username}", {"user_id": user_id, "username": username, "pw_hash": hashed})
    return jsonify({"token": make_token(user_id, username), "username": username, "user_id": user_id}), 201

@app.post("/api/auth/login")
def login():
    body = request.get_json(force=True) or {}
    username = body.get("username","").strip()
    password = body.get("password","")
    user = kv_get(f"user:{username}")
    if not user or not bcrypt.checkpw(password.encode(), user["pw_hash"].encode()):
        return jsonify({"error": "Invalid credentials"}), 401
    return jsonify({"token": make_token(user["user_id"], username), "username": username, "user_id": user["user_id"]})

@app.post("/api/auth/guest")
def guest():
    username = f"Guest_{uuid.uuid4().hex[:5].upper()}"
    user_id  = str(uuid.uuid4())
    return jsonify({"token": make_token(user_id, username), "username": username, "user_id": user_id})

# ─────────────────────────────────────────────────────────────────────────────
# REST — Sessions
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/sessions")
@jwt_required
def create_session():
    body     = request.get_json(force=True) or {}
    language = body.get("language", "python")
    sid      = "cs-" + uuid.uuid4().hex[:8]
    obj = {
        "session_id": sid,
        "owner_id":   request.user["user_id"],
        "owner_name": request.user["username"],
        "language":   language,
        "code":       STARTER.get(language, STARTER["python"]),
        "members":    {},
        "created_at": int(time.time()),
        "files":      {"main.py": STARTER.get(language, STARTER["python"])},
        "git":        {"connected": False, "repo": None, "branch": "main", "commits": []},
    }
    kv_set(f"session:{sid}", obj)
    return jsonify(obj), 201

@app.get("/api/sessions/<sid>")
def get_session(sid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    obj.pop("members", None)   # strip private member data
    return jsonify(obj)

@app.patch("/api/sessions/<sid>")
@jwt_required
def patch_session(sid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    if not has_permission(obj, request.user["user_id"], "can_update_settings"):
        return jsonify({"error": "Not authorized"}), 403
    body = request.get_json(force=True) or {}
    for field in ("code", "language", "files"):
        if field in body: obj[field] = body[field]
    kv_set(f"session:{sid}", obj)
    return jsonify(obj)

@app.delete("/api/sessions/<sid>")
@jwt_required
def delete_session(sid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    if not has_permission(obj, request.user["user_id"], "can_delete_session"):
        return jsonify({"error": "Not authorized"}), 403
    kv_del(f"session:{sid}")
    return jsonify({"deleted": sid})

# ─────────────────────────────────────────────────────────────────────────────
# REST — Members
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/sessions/<sid>/members")
@jwt_required
def add_member(sid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    if not has_permission(obj, request.user["user_id"], "can_manage_members"):
        return jsonify({"error": "Owner-only"}), 403
    body     = request.get_json(force=True) or {}
    username = body.get("username","").strip()
    role     = body.get("role","developer")
    if role == "owner": return jsonify({"error": "Cannot assign owner role"}), 400
    user = kv_get(f"user:{username}")
    if not user: return jsonify({"error": f"User '{username}' not found"}), 404
    uid = user["user_id"]
    obj.setdefault("members", {})[uid] = {"username": username, "role": role}
    kv_set(f"session:{sid}", obj)
    return jsonify({"user_id": uid, "username": username, "role": role})

@app.delete("/api/sessions/<sid>/members/<uid>")
@jwt_required
def remove_member(sid, uid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    if not has_permission(obj, request.user["user_id"], "can_manage_members"):
        return jsonify({"error": "Owner-only"}), 403
    obj.get("members", {}).pop(uid, None)
    kv_set(f"session:{sid}", obj)
    return jsonify({"removed": uid})

@app.get("/api/sessions/<sid>/my-role")
@jwt_required
def my_role(sid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    role  = get_user_role(obj, request.user["user_id"])
    return jsonify({"role": role, "permissions": ROLE_PERMISSIONS[role]})

# ─────────────────────────────────────────────────────────────────────────────
# REST — Git (simulated; real impl would use GitPython / GitHub API)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/sessions/<sid>/git/connect")
@jwt_required
def git_connect(sid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    body   = request.get_json(force=True) or {}
    repo   = body.get("repo","").strip()
    branch = body.get("branch","main").strip()
    if not repo: return jsonify({"error": "repo required"}), 400
    obj["git"] = {"connected": True, "repo": repo, "branch": branch, "commits": [
        {"hash": uuid.uuid4().hex[:7], "msg": "Initial commit", "author": obj["owner_name"], "ts": int(time.time())-3600},
    ]}
    kv_set(f"session:{sid}", obj)
    socketio.emit("git_update", obj["git"], room=sid)
    return jsonify(obj["git"])

@app.post("/api/sessions/<sid>/git/commit")
@jwt_required
def git_commit(sid):
    obj = kv_get(f"session:{sid}")
    if not obj: return jsonify({"error": "Session not found"}), 404
    if not has_permission(obj, request.user["user_id"], "can_edit"):
        return jsonify({"error": "Not authorized"}), 403
    body = request.get_json(force=True) or {}
    msg  = body.get("message","").strip()
    if not msg: return jsonify({"error": "Commit message required"}), 400
    commit = {"hash": uuid.uuid4().hex[:7], "msg": msg,
              "author": request.user["username"], "ts": int(time.time())}
    obj.setdefault("git", {}).setdefault("commits", []).insert(0, commit)
    kv_set(f"session:{sid}", obj)
    socketio.emit("git_update", obj["git"], room=sid)
    return jsonify(commit)

# ─────────────────────────────────────────────────────────────────────────────
# REST — Code Execution (subprocess sandbox)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/execute")
@jwt_required
def execute():
    body       = request.get_json(force=True) or {}
    code       = body.get("code","")
    language   = body.get("language","python")
    session_id = body.get("session_id")

    if session_id:
        obj = kv_get(f"session:{session_id}")
        if obj and not has_permission(obj, request.user["user_id"], "can_run"):
            return jsonify({"error": "Your role does not allow running code"}), 403

    cfg = LANG_CONFIG.get(language, LANG_CONFIG["python"])
    t0  = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="cs_") as tmpdir:
        fname = "Main.java" if language == "java" else f"solution{cfg['ext']}"
        src   = os.path.join(tmpdir, fname)
        with open(src, "w") as f: f.write(code)

        if "compile" in cfg:
            out_bin = src.replace(cfg["ext"], "")
            try:
                cp = subprocess.run(cfg["compile"](src, out_bin), capture_output=True, text=True, timeout=EXEC_TIMEOUT, cwd=tmpdir)
                if cp.returncode != 0:
                    return jsonify({"stdout":"","stderr":cp.stderr.strip(),"exit_code":cp.returncode,"duration_ms":int((time.monotonic()-t0)*1000)})
            except FileNotFoundError:
                return jsonify({"stdout":"","stderr":f"Compiler for '{language}' not found","exit_code":1,"duration_ms":0})
            except subprocess.TimeoutExpired:
                return jsonify({"stdout":"","stderr":"Compilation timed out","exit_code":1,"duration_ms":EXEC_TIMEOUT*1000})

        try:
            rp = subprocess.run(cfg["run"](src), capture_output=True, text=True, timeout=EXEC_TIMEOUT, cwd=tmpdir)
            return jsonify({"stdout":rp.stdout,"stderr":rp.stderr.strip(),"exit_code":rp.returncode,"duration_ms":int((time.monotonic()-t0)*1000)})
        except FileNotFoundError:
            return jsonify({"stdout":"","stderr":f"Runtime for '{language}' not found","exit_code":1,"duration_ms":0})
        except subprocess.TimeoutExpired:
            return jsonify({"stdout":"","stderr":f"Timed out after {EXEC_TIMEOUT}s","exit_code":124,"duration_ms":EXEC_TIMEOUT*1000})

# ─────────────────────────────────────────────────────────────────────────────
# REST — AI Assistant (Claude)
# ─────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are CodeLab AI, an expert coding assistant in a real-time collaborative IDE.
Be concise and actionable. Wrap code in triple-backtick blocks with language tags.
When fixing bugs, show the corrected snippet. Never refuse legal programming tasks."""

@app.post("/api/ai/chat")
@jwt_required
def ai_chat():
    if not GOOGLE_API_KEY:
        return jsonify({"error": "GOOGLE_API_KEY not configured on server"}), 503

    body     = request.get_json(force=True) or {}
    message  = body.get("message","").strip()
    code     = body.get("code","")
    language = body.get("language","python")
    history  = body.get("history",[])

    if not message:
        return jsonify({"error": "message required"}), 400

    try:
        import google.generativeai as genai
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=SYSTEM_PROMPT,
        )

        # Build conversation history
        chat_history = []
        for h in history[-10:]:
            role = "user" if h["role"] == "user" else "model"
            chat_history.append({"role": role, "parts": [h["content"]]})

        chat = model.start_chat(history=chat_history)
        context = f"Current {language} file:\n```{language}\n{code[:3000]}\n```\n\n"
        resp = chat.send_message(context + message)
        return jsonify({"reply": resp.text})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# Socket.IO — Real-time collaboration
# ─────────────────────────────────────────────────────────────────────────────
_peers: dict = {}   # socket_id -> {username, user_id, session_id, ...}

def _push_presence(session_id):
    users = [
        {"username": p["username"], "user_id": p["user_id"],
         "line": p.get("line",0), "ch": p.get("ch",0)}
        for p in _peers.values() if p.get("session_id") == session_id
    ]
    emit("presence", {"users": users}, room=session_id)

@socketio.on("connect")
def ws_connect():
    token = request.args.get("token","")
    payload = decode_token(token)
    if not payload: return False
    _peers[request.sid] = {
        "username": payload["username"],
        "user_id":  payload["user_id"],
        "session_id": None,
        "line": 0, "ch": 0,
    }

@socketio.on("disconnect")
def ws_disconnect():
    peer = _peers.pop(request.sid, {})
    sid  = peer.get("session_id")
    if sid:
        leave_room(sid)
        _push_presence(sid)
        emit("notification", {"msg": f"{peer['username']} left", "kind": "info"}, room=sid)

@socketio.on("join")
def ws_join(data):
    sid = data.get("session_id","")
    obj = kv_get(f"session:{sid}")
    if not obj:
        emit("error", {"msg": "Session not found"}); return
    peer = _peers.get(request.sid, {})
    peer["session_id"] = sid
    join_room(sid)
    emit("snapshot", {"code": obj["code"], "language": obj["language"], "session_id": sid, "files": obj.get("files",{}), "git": obj.get("git",{})})
    _push_presence(sid)
    emit("notification", {"msg": f"{peer.get('username','?')} joined", "kind": "info"}, room=sid, include_self=False)

@socketio.on("code_delta")
def ws_code_delta(data):
    peer = _peers.get(request.sid, {})
    sid  = peer.get("session_id")
    if not sid: return
    obj = kv_get(f"session:{sid}")
    if obj and not has_permission(obj, peer.get("user_id",""), "can_edit"):
        emit("error", {"msg": "Your role does not allow editing"}); return
    new_code = data.get("code","")
    filename = data.get("filename","main.py")
    if obj:
        obj["code"] = new_code
        obj.setdefault("files",{})[filename] = new_code
        kv_set(f"session:{sid}", obj)
    emit("code_update", {"code": new_code, "filename": filename, "username": peer.get("username"), "cursor": data.get("cursor",{})}, room=sid, include_self=False)

@socketio.on("cursor")
def ws_cursor(data):
    peer = _peers.get(request.sid, {})
    sid  = peer.get("session_id")
    if not sid: return
    peer["line"] = data.get("line",0)
    peer["ch"]   = data.get("ch",0)
    emit("cursor_update", {"user_id": peer["user_id"], "username": peer["username"], "line": data.get("line",0), "ch": data.get("ch",0)}, room=sid, include_self=False)

@socketio.on("team_chat")
def ws_team_chat(data):
    peer = _peers.get(request.sid, {})
    sid  = peer.get("session_id")
    if not sid: return
    obj = kv_get(f"session:{sid}")
    if obj and not has_permission(obj, peer.get("user_id",""), "can_chat"):
        emit("error", {"msg": "Your role does not allow chat"}); return
    emit("team_message", {
        "uid": peer["user_id"],
        "username": peer["username"],
        "text": data.get("text",""),
        "refs": data.get("refs",[]),
        "ts": int(time.time()*1000),
    }, room=sid)

@socketio.on("git_op")
def ws_git_op(data):
    peer = _peers.get(request.sid, {})
    sid  = peer.get("session_id")
    if not sid: return
    emit("git_notification", {"op": data.get("op"), "username": peer["username"], "detail": data.get("detail","")}, room=sid, include_self=False)

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import socket as _socket

    def _find_free_port(start: int) -> int:
        for p in range(start, start + 20):
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("0.0.0.0", p))
                    return p
                except OSError:
                    continue
        raise RuntimeError("No free port found in range")

    requested = int(os.getenv("PORT", 5000))
    port = _find_free_port(requested)
    if port != requested:
        print(f"[Warning] Port {requested} is in use — using port {port} instead")

    print(f"""
╔══════════════════════════════════════════╗
║   CodeLab — Full-Stack Collaborative IDE ║
║   http://localhost:{port:<26}║
╚══════════════════════════════════════════╝
    """)
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
