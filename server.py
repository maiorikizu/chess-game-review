import sys
import os
import json
import socket
import threading
import webbrowser
import http.server
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import engine as engine_module
from engine import AnalysisSession
from openings import build_or_load_openings
import history_manager
import ai_provider
import hardware_scanner
import app_paths

RESOURCE_ROOT = app_paths.get_resource_dir()
APP_ROOT = app_paths.get_app_dir()

ROOT = RESOURCE_ROOT
WEB_DIR = app_paths.first_existing(
    os.path.join(RESOURCE_ROOT, "web"),
    os.path.join(APP_ROOT, "web"),
) or os.path.join(RESOURCE_ROOT, "web")

STATE_DIR = app_paths.get_state_dir()
INITIAL_GAME_FILE = os.path.join(STATE_DIR, "initial_game.json")
_BUNDLED_INITIAL_GAME = app_paths.first_existing(
    os.path.join(RESOURCE_ROOT, "initial_game.json"),
    os.path.join(APP_ROOT, "initial_game.json"),
)

MAX_BODY_BYTES = 8 * 1024 * 1024

CURRENT_GAME_DATA = None
ANALYSIS_STATUS = {
    "status": "idle",
    "current_ply": 0,
    "total_plies": 0,
    "latest_move": None
}
ANALYSIS_LOCK = threading.Lock()


def load_or_create_initial_game():
    global CURRENT_GAME_DATA
    if os.path.exists(INITIAL_GAME_FILE):
        try:
            with open(INITIAL_GAME_FILE, "r", encoding="utf-8") as f:
                CURRENT_GAME_DATA = json.load(f)
                return
        except Exception as e:
            print(f"[Server] Failed loading {INITIAL_GAME_FILE}: {e}")

    if _BUNDLED_INITIAL_GAME and os.path.exists(_BUNDLED_INITIAL_GAME):
        try:
            with open(_BUNDLED_INITIAL_GAME, "r", encoding="utf-8") as f:
                CURRENT_GAME_DATA = json.load(f)
            app_paths.atomic_write_json(INITIAL_GAME_FILE, CURRENT_GAME_DATA)
            return
        except Exception as e:
            print(f"[Server] Failed seeding from {_BUNDLED_INITIAL_GAME}: {e}")

    games = history_manager.list_games()
    if games:
        g = history_manager.get_game(games[0]["id"])
        if g:
            CURRENT_GAME_DATA = g
            return

    CURRENT_GAME_DATA = {
        "white": {"name": "White", "elo": "?", "accuracy": None},
        "black": {"name": "Black", "elo": "?", "accuracy": None},
        "moves": [],
        "opening": "Chess Game"
    }


def parse_pgn_to_skeleton(pgn_text: str):
    import io, chess, chess.pgn
    try:
        parsed_g = chess.pgn.read_game(io.StringIO(pgn_text.strip()))
        if not parsed_g:
            return None
        w_name = parsed_g.headers.get("White", "White")
        b_name = parsed_g.headers.get("Black", "Black")
        w_elo = parsed_g.headers.get("WhiteElo", "?")
        b_elo = parsed_g.headers.get("BlackElo", "?")
        eco = parsed_g.headers.get("ECO", "")
        eco_url = parsed_g.headers.get("ECOUrl", "")
        opening_header = eco_url.split("/")[-1].replace("-", " ") if eco_url else (f"ECO: {eco}" if eco else "Chess Game")

        board = parsed_g.board()
        preloaded_moves = []
        for ply_idx, move in enumerate(parsed_g.mainline_moves()):
            san = board.san(move)
            uci = move.uci()
            color = "white" if board.turn == chess.WHITE else "black"
            fullmove = board.fullmove_number
            board.push(move)
            fen = board.fen()
            preloaded_moves.append({
                "ply": ply_idx,
                "fullmove": fullmove,
                "color": color,
                "san": san,
                "uci": uci,
                "fen": fen,
                "score_cp": 0.0,
                "score_str": "--",
                "win_prob": 50.0,
                "accuracy": None,
                "classification": None,
                "headline": "Evaluating position...",
                "explanation": "Evaluating this position...",
                "deep_explain": "",
                "opponent_headline": "Evaluating position...",
                "opponent_explanation": "Evaluating this position...",
                "opponent_deep_explain": "",
                "best_move_san": None,
                "best_line_san": None,
                "best_arrow": None,
                "threat_arrow": None,
                "top_lines": []
            })

        return {
            "white": {"name": w_name, "elo": w_elo, "accuracy": None, "categories": {c: 0 for c in ["BRILLIANT", "GREAT", "BOOK", "BEST", "EXCELLENT", "GOOD", "INACCURACY", "MISTAKE", "MISS", "BLUNDER"]}},
            "black": {"name": b_name, "elo": b_elo, "accuracy": None, "categories": {c: 0 for c in ["BRILLIANT", "GREAT", "BOOK", "BEST", "EXCELLENT", "GOOD", "INACCURACY", "MISTAKE", "MISS", "BLUNDER"]}},
            "moves": preloaded_moves,
            "opening": opening_header,
            "engine_name": engine_module.ENGINE_NAME
        }
    except Exception as e:
        print(f"[Server] PGN skeleton parse error: {e}")
        return None


def background_analyze(pgn_text: str, time_ms: int):
    global CURRENT_GAME_DATA, ANALYSIS_STATUS
    session = AnalysisSession(time_per_ply_ms=time_ms)
    try:
        session.start()

        def on_progress(move_record, current_ply, total_plies):
            with ANALYSIS_LOCK:
                ANALYSIS_STATUS["current_ply"] = current_ply
                ANALYSIS_STATUS["total_plies"] = total_plies
                ANALYSIS_STATUS["latest_move"] = move_record
                if CURRENT_GAME_DATA is not None and "moves" in CURRENT_GAME_DATA:
                    idx = current_ply - 1
                    if idx < len(CURRENT_GAME_DATA["moves"]):
                        CURRENT_GAME_DATA["moves"][idx] = move_record
                    else:
                        CURRENT_GAME_DATA["moves"].append(move_record)
                    if move_record.get("opening"):
                        CURRENT_GAME_DATA["opening"] = move_record["opening"]

                    w_accs = [m["accuracy"] for m in CURRENT_GAME_DATA["moves"] if m.get("color") == "white" and m.get("accuracy") is not None]
                    b_accs = [m["accuracy"] for m in CURRENT_GAME_DATA["moves"] if m.get("color") == "black" and m.get("accuracy") is not None]
                    if w_accs:
                        CURRENT_GAME_DATA["white"]["accuracy"] = round(sum(w_accs) / len(w_accs), 1)
                    if b_accs:
                        CURRENT_GAME_DATA["black"]["accuracy"] = round(sum(b_accs) / len(b_accs), 1)

                    w_cats = {c: 0 for c in ["BRILLIANT", "GREAT", "BOOK", "BEST", "EXCELLENT", "GOOD", "INACCURACY", "MISTAKE", "MISS", "BLUNDER"]}
                    b_cats = {c: 0 for c in ["BRILLIANT", "GREAT", "BOOK", "BEST", "EXCELLENT", "GOOD", "INACCURACY", "MISTAKE", "MISS", "BLUNDER"]}
                    for m in CURRENT_GAME_DATA["moves"]:
                        c = m.get("classification")
                        col = m.get("color")
                        if c:
                            if col == "white" and c in w_cats:
                                w_cats[c] += 1
                            elif col == "black" and c in b_cats:
                                b_cats[c] += 1
                    CURRENT_GAME_DATA["white"]["categories"] = w_cats
                    CURRENT_GAME_DATA["black"]["categories"] = b_cats

                    try:
                        app_paths.atomic_write_json(INITIAL_GAME_FILE, CURRENT_GAME_DATA)
                    except Exception as e:
                        print(f"[Server] Session file write skipped: {e}")

        result = session.analyze_game(pgn_text, progress_callback=on_progress)
        with ANALYSIS_LOCK:
            CURRENT_GAME_DATA = result
            ANALYSIS_STATUS["status"] = "complete"
            ANALYSIS_STATUS["current_ply"] = len(result["moves"])
            ANALYSIS_STATUS["total_plies"] = len(result["moves"])
            ANALYSIS_STATUS["latest_move"] = result["moves"][-1] if result.get("moves") else None
            try:
                app_paths.atomic_write_json(INITIAL_GAME_FILE, CURRENT_GAME_DATA)
            except Exception as e:
                print(f"[Server] Session file write skipped: {e}")
            try:
                new_id = history_manager.save_game(CURRENT_GAME_DATA)
                print(f"[History] Successfully auto-saved game to history with ID #{new_id}")
            except Exception as e:
                print(f"[History] Failed auto-saving to history: {e}")

    except Exception as e:
        print(f"[Server] Analysis error: {e}")
        with ANALYSIS_LOCK:
            ANALYSIS_STATUS["status"] = "error"
            ANALYSIS_STATUS["error"] = str(e)
            if isinstance(CURRENT_GAME_DATA, dict):
                CURRENT_GAME_DATA["analysis_incomplete"] = True
    finally:
        session.stop()


class _BadRequest(ValueError):
    pass


def _redact_secrets(cfg):
    if not isinstance(cfg, dict):
        return cfg
    out = {}
    for k, v in cfg.items():
        if k.endswith("_key"):
            out[k] = ""
            out[f"{k}_set"] = bool(v)
        else:
            out[k] = v
    return out


class ReviewHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def _is_allowed_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            host = self.headers.get("Host", "")
            if origin not in (f"http://{host}", f"https://{host}"):
                return False
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("", "localhost", "127.0.0.1", "[::1]", "::1")

    def _api_forbidden(self) -> bool:
        if not self._is_allowed_origin():
            self.send_error(403, "Cross-origin request rejected")
            return True
        return False

    def _read_json_body(self):
        raw_len = self.headers.get("Content-Length", "0")
        try:
            content_len = int(raw_len)
        except (TypeError, ValueError):
            raise _BadRequest(f"Invalid Content-Length: {raw_len!r}")
        if content_len < 0:
            raise _BadRequest("Negative Content-Length")
        if content_len > MAX_BODY_BYTES:
            raise _BadRequest(f"Request body too large (max {MAX_BODY_BYTES} bytes)")
        body = self.rfile.read(content_len).decode("utf-8", errors="replace") if content_len else ""
        if not body:
            return {}
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise _BadRequest(f"Malformed JSON body: {exc}")
        if not isinstance(payload, dict):
            raise _BadRequest("JSON body must be an object")
        return payload

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        global CURRENT_GAME_DATA
        parsed = urlparse(self.path)

        if parsed.path.startswith("/api/") and self._api_forbidden():
            return

        if parsed.path == "/api/game":
            with ANALYSIS_LOCK:
                payload = CURRENT_GAME_DATA
            if not payload and os.path.exists(INITIAL_GAME_FILE):
                try:
                    with open(INITIAL_GAME_FILE, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                except Exception:
                    payload = None
                if payload:
                    with ANALYSIS_LOCK:
                        if CURRENT_GAME_DATA is None:
                            CURRENT_GAME_DATA = payload
                        else:
                            payload = CURRENT_GAME_DATA
            with ANALYSIS_LOCK:
                data = json.dumps(payload or {}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        elif parsed.path == "/api/progress":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with ANALYSIS_LOCK:
                data = json.dumps(ANALYSIS_STATUS).encode("utf-8")
            self.wfile.write(data)
            return

        elif parsed.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            import time
            last_ply = -1
            last_status = None
            last_keepalive = time.time()
            while True:
                time.sleep(0.4)
                with ANALYSIS_LOCK:
                    status = ANALYSIS_STATUS.get("status")
                    curr = ANALYSIS_STATUS.get("current_ply", 0)
                    total = ANALYSIS_STATUS.get("total_plies", 0)
                    latest = ANALYSIS_STATUS.get("latest_move")
                try:
                    if curr != last_ply or status != last_status:
                        last_ply = curr
                        last_status = status
                        msg = json.dumps({"status": status, "current_ply": curr, "total_plies": total, "latest_move": latest})
                        self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    elif time.time() - last_keepalive >= 5.0:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_keepalive = time.time()
                except Exception:
                    break
                if status in ("complete", "error"):
                    break
                if status != "analyzing" and total == 0:
                    break
            return

        elif parsed.path == "/api/history":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            games = history_manager.list_games()
            self.wfile.write(json.dumps(games).encode("utf-8"))
            return

        elif parsed.path.startswith("/api/history/"):
            try:
                game_id = int(parsed.path.split("/")[-1])
            except ValueError:
                self.send_error(400, "Invalid game ID")
                return
            try:
                game = history_manager.get_game(game_id)
            except Exception as e:
                print(f"[Server] /api/history/{game_id} failed: {e}")
                self.send_error(500, "History read failed")
                return
            if game:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(game).encode("utf-8"))
            else:
                self.send_error(404, "Game not found in history")
            return

        elif parsed.path == "/api/hardware":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            hw = hardware_scanner.get_system_hardware()
            services = hardware_scanner.scan_local_services()
            resp = {
                "hardware": hw,
                "detected_services": services
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        elif parsed.path == "/api/config/ai":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            cfg = _redact_secrets(ai_provider.load_config())
            resp = {
                "config": cfg,
                "local_models": ai_provider.LOCAL_MODELS,
                "nim_cascade": ai_provider.NIM_FAILOVER_CASCADE
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        super().do_GET()

    def do_POST(self):
        global CURRENT_GAME_DATA, ANALYSIS_STATUS
        parsed = urlparse(self.path)

        if parsed.path == "/api/analyze":
            try:
                payload = self._read_json_body()
            except _BadRequest as e:
                self.send_error(400, str(e))
                return
            try:
                pgn = payload.get("pgn", "")
                try:
                    time_ms = int(payload.get("time_ms", 15000))
                except (TypeError, ValueError):
                    self.send_error(400, "time_ms must be an integer")
                    return
                if not isinstance(pgn, str) or not pgn.strip():
                    self.send_error(400, "Missing PGN")
                    return
                time_ms = max(500, min(time_ms, 60000))

                with ANALYSIS_LOCK:
                    if ANALYSIS_STATUS.get("status") == "analyzing":
                        self.send_error(409, "An analysis is already running")
                        return

                skeleton = parse_pgn_to_skeleton(pgn)
                if not skeleton or not skeleton.get("moves"):
                    self.send_error(400, "Could not parse valid moves from PGN")
                    return

                total_plies = len(skeleton["moves"])
                with ANALYSIS_LOCK:
                    CURRENT_GAME_DATA = skeleton
                    ANALYSIS_STATUS["status"] = "analyzing"
                    ANALYSIS_STATUS["current_ply"] = 0
                    ANALYSIS_STATUS["total_plies"] = total_plies
                    ANALYSIS_STATUS["latest_move"] = None
                    ANALYSIS_STATUS.pop("error", None)

                threading.Thread(target=background_analyze, args=(pgn, time_ms), daemon=True).start()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "started",
                    "total_plies": total_plies,
                    "game": skeleton,
                    "game_skeleton": skeleton
                }).encode("utf-8"))
            except Exception as e:
                print(f"[Server] /api/analyze failed: {e!r}")
                self.send_error(500, "Analysis could not be started")
            return

        elif parsed.path == "/api/history/load":
            try:
                payload = self._read_json_body()
            except _BadRequest as e:
                self.send_error(400, str(e))
                return
            try:
                game_id = int(payload.get("id"))
            except (TypeError, ValueError):
                self.send_error(400, "Missing or invalid game id")
                return
            try:
                game = history_manager.get_game(game_id)
            except Exception as e:
                print(f"[Server] /api/history/load failed: {e!r}")
                self.send_error(500, "History read failed")
                return
            if not game:
                self.send_error(404, "Game not found")
                return

            with ANALYSIS_LOCK:
                CURRENT_GAME_DATA = game
                ANALYSIS_STATUS["status"] = "complete"
                ANALYSIS_STATUS["current_ply"] = len(game.get("moves", []))
                ANALYSIS_STATUS["total_plies"] = len(game.get("moves", []))
                ANALYSIS_STATUS["latest_move"] = game.get("moves", [None])[-1]
                ANALYSIS_STATUS.pop("error", None)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "loaded", "game": game}).encode("utf-8"))
            return

        elif parsed.path == "/api/hardware/test-custom":
            try:
                payload = self._read_json_body()
            except _BadRequest as e:
                self.send_error(400, str(e))
                return
            try:
                endpoint = payload.get("endpoint", "")
                result = hardware_scanner.test_custom_endpoint(endpoint)
            except Exception as e:
                print(f"[Server] /api/hardware/test-custom failed: {e!r}")
                self.send_error(500, "Endpoint test failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
            return

        elif parsed.path == "/api/config/ai":
            try:
                new_cfg = self._read_json_body()
            except _BadRequest as e:
                self.send_error(400, str(e))
                return
            try:
                ai_provider.save_config(new_cfg)
                updated = _redact_secrets(ai_provider.load_config())
            except Exception as e:
                print(f"[Server] /api/config/ai save failed: {e!r}")
                self.send_error(500, "Could not save AI configuration")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "updated", "config": updated}).encode("utf-8"))
            return

        self.send_error(404, "Endpoint not found")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if self._api_forbidden():
                return
        if parsed.path.startswith("/api/history/"):
            try:
                game_id = int(parsed.path.split("/")[-1])
            except ValueError:
                self.send_error(400, "Invalid game ID")
                return
            try:
                success = history_manager.delete_game(game_id)
            except Exception as e:
                print(f"[Server] DELETE /api/history/{game_id} failed: {e!r}")
                self.send_error(500, "History delete failed")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"deleted": success, "id": game_id}).encode("utf-8"))
            return
        self.send_error(404, "Endpoint not found")


def find_free_port(start_port=8080) -> int:
    for p in range(start_port, start_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start_port


def start_server(port=None, open_browser=False):
    threading.Thread(target=build_or_load_openings, daemon=True).start()
    history_manager.init_db()
    load_or_create_initial_game()

    candidates = [port] if port is not None else list(range(8080, 8100))
    server = None
    last_err = None
    for cand in candidates:
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", cand), ReviewHandler)
            port = cand
            break
        except OSError as e:
            last_err = e
            continue
    if server is None:
        raise OSError(f"No free port found in {candidates[0]}-{candidates[-1]}: {last_err}")

    url = f"http://localhost:{port}/index.html"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        ai_provider.migrate_legacy_configs()
    except Exception as e:
        print(f"[AI Provider] Legacy config migration error: {e}")
    cfg = ai_provider.load_config()
    print(f"\n=================================================================")
    print(f"  Chess Review Desktop Backend Running!")
    print(f"  URL: {url}")
    print(f"  AI Mode: {cfg.get('provider', 'local').upper()} ({cfg.get('local_model', 'gemma-4-12b-it')})")
    try:
        sf_path = engine_module.get_stockfish_path()
    except Exception as e:
        sf_path = f"NOT FOUND ({e})"
    print(f"  Stockfish Engine: {sf_path}")
    print(f"=================================================================\n")

    return server, port


if __name__ == "__main__":
    server, port = start_server(port=8080, open_browser=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.shutdown()
