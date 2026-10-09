import os
import json
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import app_paths

DATA_DIR = app_paths.get_state_dir()
RESOURCE_DIR = app_paths.get_resource_dir()

DB_PATH = os.path.join(DATA_DIR, "game_history.db")
INITIAL_GAME_FILE = app_paths.first_existing(
    os.path.join(RESOURCE_DIR, "initial_game.json"),
    os.path.join(app_paths.get_app_dir(), "initial_game.json"),
) or os.path.join(RESOURCE_DIR, "initial_game.json")

_DB_TIMEOUT_S = 15.0

_INIT_LOCK = threading.Lock()
_initialized = False


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=_DB_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(_DB_TIMEOUT_S * 1000)}")
    except sqlite3.Error:
        pass
    return conn


def _migrate_legacy_db() -> None:
    if not app_paths.is_frozen():
        return
    legacy_db = os.path.join(app_paths.get_app_dir(), "game_history.db")
    if os.path.exists(DB_PATH) or not os.path.exists(legacy_db):
        return
    try:
        shutil.copy2(legacy_db, DB_PATH)
        print(f"[History] Migrated legacy database from {legacy_db}")
    except Exception as e:
        print(f"[History] Legacy DB migration failed: {e}")


def _safe_accuracy(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def init_db():
    global _initialized
    if _initialized:
        return
    with _INIT_LOCK:
        if _initialized:
            return
        _migrate_legacy_db()
        conn = get_connection()
        try:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='games'"
            ).fetchone() is not None
            conn.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    white_name TEXT NOT NULL,
                    white_elo TEXT,
                    white_accuracy REAL,
                    black_name TEXT NOT NULL,
                    black_elo TEXT,
                    black_accuracy REAL,
                    opening TEXT,
                    result TEXT,
                    total_plies INTEGER,
                    white_categories TEXT,
                    black_categories TEXT,
                    game_data TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()
        _initialized = True

    if not table_exists and INITIAL_GAME_FILE and os.path.exists(INITIAL_GAME_FILE):
        try:
            with open(INITIAL_GAME_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            save_game(data)
            print("[History] Seeded initial demo game into game_history.db")
        except Exception as e:
            print(f"[History] Failed to seed initial game: {e}")


def save_game(game_data: Dict[str, Any]) -> int:
    init_db()
    white = game_data.get("white", {})
    black = game_data.get("black", {})
    moves = game_data.get("moves", [])
    opening = game_data.get("opening", "Unknown Opening")

    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    white_name = white.get("name", "White")
    white_elo = str(white.get("elo", "?"))
    white_acc = _safe_accuracy(white.get("accuracy"))

    black_name = black.get("name", "Black")
    black_elo = str(black.get("elo", "?"))
    black_acc = _safe_accuracy(black.get("accuracy"))

    result = game_data.get("result", "*")
    total_plies = len(moves)

    white_cats_json = json.dumps(white.get("categories", {}))
    black_cats_json = json.dumps(black.get("categories", {}))
    full_game_json = json.dumps(game_data)

    conn = get_connection()
    try:
        cursor = conn.execute("""
            INSERT INTO games (
                created_at, white_name, white_elo, white_accuracy,
                black_name, black_elo, black_accuracy,
                opening, result, total_plies,
                white_categories, black_categories, game_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            created_at, white_name, white_elo, white_acc,
            black_name, black_elo, black_acc,
            opening, result, total_plies,
            white_cats_json, black_cats_json, full_game_json
        ))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def list_games() -> List[Dict[str, Any]]:
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT id, created_at, white_name, white_elo, white_accuracy,
                   black_name, black_elo, black_accuracy,
                   opening, result, total_plies,
                   white_categories, black_categories
            FROM games
            ORDER BY id DESC
        """).fetchall()
    finally:
        conn.close()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "white_name": r["white_name"],
            "white_elo": r["white_elo"],
            "white_accuracy": r["white_accuracy"],
            "black_name": r["black_name"],
            "black_elo": r["black_elo"],
            "black_accuracy": r["black_accuracy"],
            "opening": r["opening"],
            "result": r["result"],
            "total_plies": r["total_plies"],
            "white_categories": json.loads(r["white_categories"]) if r["white_categories"] else {},
            "black_categories": json.loads(r["black_categories"]) if r["black_categories"] else {}
        })
    return results


def get_game(game_id: int) -> Optional[Dict[str, Any]]:
    init_db()
    conn = get_connection()
    try:
        row = conn.execute("SELECT game_data FROM games WHERE id = ?", (game_id,)).fetchone()
    finally:
        conn.close()
    if row and row["game_data"]:
        return json.loads(row["game_data"])
    return None


def delete_game(game_id: int) -> bool:
    init_db()
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("History manager initialized. Existing games count:", len(list_games()))
