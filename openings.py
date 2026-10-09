import sys
import os
import json
import re
import threading
import urllib.request
from typing import Dict, List, Optional, Tuple

import app_paths

_BUNDLED_CACHE = os.path.join(app_paths.get_resource_dir(), "openings_cache.json")
if app_paths.is_frozen():
    CACHE_FILE = os.path.join(app_paths.get_state_dir(), "openings_cache.json")
else:
    CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openings_cache.json")
BASE_URL = "https://raw.githubusercontent.com/lichess-org/chess-openings/master"

_MIN_FULL_DB_ENTRIES = 1500

_OPENINGS_BY_MOVES: Dict[str, Dict[str, str]] = {}
_CHILD_MOVES: Dict[str, set] = {}
_INITIALIZED = False
_LOCK = threading.RLock()


def _clean_pgn_moves(pgn: str) -> str:
    cleaned = re.sub(r"\d+\.+", "", pgn)
    cleaned = re.sub(r"\b(?:1-0|0-1|1/2-1/2|\*)\b", "", cleaned)
    tokens = cleaned.strip().split()
    return " ".join(tokens)


CORE_OPENINGS = {
    "c4": {"eco": "A10", "name": "English Opening"},
    "c4 e5": {"eco": "A20", "name": "English Opening: King's English Variation"},
    "c4 e5 g3": {"eco": "A20", "name": "English Opening: King's English Variation, 2.g3"},
    "c4 e5 g3 Nf6": {"eco": "A20", "name": "English Opening: King's English Variation, 2...Nf6"},
    "c4 e5 g3 Nf6 Bg2": {"eco": "A20", "name": "English Opening: King's English Variation, 3.Bg2"},
    "c4 e5 g3 Nf6 Bg2 d6": {"eco": "A20", "name": "English Opening: King's English Variation, 3...d6"},
    "c4 e5 g3 Nf6 Bg2 d6 Nc3": {"eco": "A20", "name": "English Opening: King's English Variation, 4.Nc3"},
    "c4 e5 g3 Nf6 Bg2 d6 Nc3 Nbd7": {"eco": "A20", "name": "English Opening: King's English Variation, 4...Nbd7"},
    "c4 e5 g3 Nf6 Bg2 d5": {"eco": "A20", "name": "English Opening: King's English Variation, 3...d5"},
    "c4 e5 g3 Nf6 Bg2 Bc5": {"eco": "A20", "name": "English Opening: King's English Variation, 3...Bc5"},
    "c4 e5 g3 Nf6 Bg2 c6": {"eco": "A20", "name": "English Opening: King's English Variation, 3...c6"},
    "c4 e5 Nc3": {"eco": "A21", "name": "English Opening: King's English, Two Knights"},
    "c4 e5 Nc3 Nf6": {"eco": "A22", "name": "English Opening: King's English, Two Knights"},
    "c4 e5 Nc3 Nf6 g3": {"eco": "A22", "name": "English Opening: King's English, 3.g3"},
    "c4 e5 Nc3 Nf6 Nf3": {"eco": "A28", "name": "English Opening: Four Knights System"},
    "c4 e5 Nc3 Nf6 Nf3 Nc6": {"eco": "A28", "name": "English Opening: Four Knights System"},
    "c4 d5": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense"},
    "c4 d5 cxd5": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense"},
    "c4 d5 cxd5 Qxd5": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense"},
    "c4 d5 cxd5 Qxd5 Nc3": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense"},
    "c4 d5 cxd5 Qxd5 Nc3 Qa5": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense, Malvinas Variation"},
    "c4 d5 cxd5 Qxd5 Nc3 Qa5 Nf3": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense, 4.Nf3"},
    "c4 d5 cxd5 Qxd5 Nc3 Qa5 Nf3 c6": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense, 4...c6"},
    "c4 d5 cxd5 Qxd5 Nc3 Qa5 Nf3 c6 d4": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense, 5.d4"},
    "c4 d5 cxd5 Qxd5 Nc3 Qa5 Nf3 c6 d4 Nf6": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense, 5...Nf6"},
    "c4 d5 cxd5 Qxd5 Nc3 Qa5 Nf3 c6 d4 Nf6 Bd2": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense, 6.Bd2"},
    "c4 d5 cxd5 Qxd5 Nc3 Qa5 Nf3 c6 d4 Nf6 Bd2 Qc7": {"eco": "A10", "name": "English Opening: Anglo-Scandinavian Defense, 6...Qc7"},
    "c4 c5": {"eco": "A30", "name": "English Opening: Symmetrical Variation"},
    "c4 Nf6": {"eco": "A15", "name": "English Opening: Anglo-Indian Defense"},
    "c4 e6": {"eco": "A13", "name": "English Opening: Agincourt Defense"},
    "c4 c6": {"eco": "A11", "name": "English Opening: Caro-Kann Defensive System"},
    "e4 c5 f4": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6 Nf3": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6 Nf3 Nc6": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6 Nf3 Nc6 Bc4": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6 Nf3 Nc6 Bc4 e6": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6 Nf3 Nc6 Bc4 e6 O-O": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6 Nf3 Nc6 Bc4 e6 O-O Nf6": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 d6 Nf3 Nc6 Bc4 e6 O-O Nf6 e5": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 Nc6": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 Nc6 Nf3": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 Nc6 Nf3 g6": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 e6": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 e6 Nf3": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4 c5 f4 e6 Nf3 d5": {"eco": "B21", "name": "Sicilian Defense: McDonnell Attack"},
    "e4": {"eco": "B00", "name": "King's Pawn Game"},
    "e4 e5": {"eco": "C20", "name": "Open Game"},
    "e4 e5 Nf3": {"eco": "C40", "name": "King's Knight Opening"},
    "e4 e5 Nf3 Nc6": {"eco": "C44", "name": "King's Knight Opening: Normal Continuation"},
    "e4 e5 Nf3 Nc6 Bc4": {"eco": "C50", "name": "Italian Game"},
    "e4 e5 Nf3 Nc6 Bc4 Bc5": {"eco": "C50", "name": "Italian Game: Giuoco Piano"},
    "e4 e5 Nf3 Nc6 Bc4 Nf6": {"eco": "C55", "name": "Italian Game: Two Knights Defense"},
    "e4 e5 Nf3 Nc6 Bb5": {"eco": "C60", "name": "Ruy Lopez"},
    "e4 e5 Nf3 Nc6 Bb5 a6": {"eco": "C68", "name": "Ruy Lopez: Morphy Defense"},
    "e4 c5": {"eco": "B20", "name": "Sicilian Defense"},
    "e4 c5 Nf3": {"eco": "B27", "name": "Sicilian Defense"},
    "e4 c5 Nf3 d6": {"eco": "B50", "name": "Sicilian Defense"},
    "e4 c5 Nf3 d6 d4": {"eco": "B53", "name": "Sicilian Defense: Open"},
    "e4 c5 Nf3 d6 d4 cxd4": {"eco": "B53", "name": "Sicilian Defense: Open"},
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4": {"eco": "B53", "name": "Sicilian Defense: Open"},
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6": {"eco": "B53", "name": "Sicilian Defense: Open"},
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3": {"eco": "B53", "name": "Sicilian Defense: Open"},
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6": {"eco": "B90", "name": "Sicilian Defense: Najdorf Variation"},
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6": {"eco": "B70", "name": "Sicilian Defense: Dragon Variation"},
    "e4 c5 Nf3 Nc6": {"eco": "B30", "name": "Sicilian Defense: Old Sicilian"},
    "e4 c5 Nf3 e6": {"eco": "B40", "name": "Sicilian Defense: French Variation"},
    "e4 c5 c3": {"eco": "B22", "name": "Sicilian Defense: Alapin Variation"},
    "e4 c5 Nc3": {"eco": "B23", "name": "Sicilian Defense: Closed"},
    "e4 e6": {"eco": "C00", "name": "French Defense"},
    "e4 e6 d4": {"eco": "C00", "name": "French Defense: Normal Variation"},
    "e4 e6 d4 d5": {"eco": "C00", "name": "French Defense"},
    "e4 c6": {"eco": "B10", "name": "Caro-Kann Defense"},
    "e4 c6 d4": {"eco": "B12", "name": "Caro-Kann Defense"},
    "e4 c6 d4 d5": {"eco": "B12", "name": "Caro-Kann Defense"},
    "d4": {"eco": "A40", "name": "Queen's Pawn Game"},
    "d4 d5": {"eco": "D00", "name": "Closed Game"},
    "d4 d5 c4": {"eco": "D06", "name": "Queen's Gambit"},
    "d4 d5 c4 e6": {"eco": "D30", "name": "Queen's Gambit Declined"},
    "d4 d5 c4 c6": {"eco": "D10", "name": "Slav Defense"},
    "d4 Nf6": {"eco": "A45", "name": "Indian Defense"},
    "d4 Nf6 c4": {"eco": "A50", "name": "Indian Defense: Normal Variation"},
    "d4 Nf6 c4 e6": {"eco": "E00", "name": "Indian Defense"},
    "d4 Nf6 c4 g6": {"eco": "E60", "name": "King's Indian Defense"},
    "d4 Nf6 c4 c5": {"eco": "A60", "name": "Benoni Defense"}
}


def _index_openings(target_dict: Dict[str, Dict[str, str]], child_index: Dict[str, set]) -> None:
    for move_seq in target_dict:
        tokens = move_seq.split()
        for i in range(len(tokens)):
            prefix = " ".join(tokens[:i])
            nxt = tokens[i]
            if prefix not in child_index:
                child_index[prefix] = set()
            child_index[prefix].add(nxt)


def _load_cache_file(path: str) -> Optional[Dict[str, Dict[str, str]]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        openings = data.get("openings")
        if not isinstance(openings, dict):
            return None
        if len(openings) < _MIN_FULL_DB_ENTRIES:
            print(f"[Openings] Ignoring incomplete cache at {path} ({len(openings)} entries).")
            return None
        return openings
    except Exception as e:
        print(f"[Openings] Cache load failed for {path}: {e}.")
        return None


def build_or_load_openings() -> None:
    global _OPENINGS_BY_MOVES, _CHILD_MOVES, _INITIALIZED
    if _INITIALIZED:
        return

    with _LOCK:
        if _INITIALIZED:
            return

        cached = None
        if os.path.exists(CACHE_FILE):
            cached = _load_cache_file(CACHE_FILE)
        if cached is None and _BUNDLED_CACHE != CACHE_FILE and os.path.exists(_BUNDLED_CACHE):
            cached = _load_cache_file(_BUNDLED_CACHE)

        if cached is not None:
            openings = cached
            openings.update(CORE_OPENINGS)
            child_moves: Dict[str, set] = {}
            _index_openings(openings, child_moves)
            _OPENINGS_BY_MOVES = openings
            _CHILD_MOVES = child_moves
            _INITIALIZED = True
            print(f"[Openings] Loaded {len(openings)} openings from cache + core.")
            return

        volumes = ["a.tsv", "b.tsv", "c.tsv", "d.tsv", "e.tsv"]
        openings = {}
        volumes_ok = 0

        print("[Openings] Downloading official Lichess Opening Database (ECO A-E)...")
        for vol in volumes:
            url = f"{BASE_URL}/{vol}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ChessReviewApp/1.0"})
                with urllib.request.urlopen(req, timeout=10) as res:
                    content = res.read().decode("utf-8")
                    lines = content.strip().splitlines()
                    if not lines:
                        continue
                    header = lines[0].split("\t")
                    eco_idx = header.index("eco") if "eco" in header else 0
                    name_idx = header.index("name") if "name" in header else 1
                    pgn_idx = header.index("pgn") if "pgn" in header else 2

                    for line in lines[1:]:
                        parts = line.split("\t")
                        if len(parts) > max(eco_idx, name_idx, pgn_idx):
                            eco = parts[eco_idx].strip()
                            name = parts[name_idx].strip()
                            raw_pgn = parts[pgn_idx].strip()
                            clean_seq = _clean_pgn_moves(raw_pgn)
                            if clean_seq:
                                openings[clean_seq] = {"eco": eco, "name": name}
                volumes_ok += 1
            except Exception as err:
                print(f"[Openings] Warning: Failed to fetch {vol}: {err}")

        openings.update(CORE_OPENINGS)
        child_moves = {}
        _index_openings(openings, child_moves)

        _OPENINGS_BY_MOVES = openings
        _CHILD_MOVES = child_moves
        _INITIALIZED = True

        if volumes_ok == len(volumes) and len(openings) >= _MIN_FULL_DB_ENTRIES:
            try:
                app_paths.atomic_write_json(CACHE_FILE, {"openings": openings}, indent=None)
                print(f"[Openings] Successfully cached {len(openings)} opening variations!")
            except Exception as e:
                print(f"[Openings] Cache write warning: {e}")
        else:
            print(
                f"[Openings] Cache NOT written (volumes_ok={volumes_ok}/{len(volumes)}, "
                f"entries={len(openings)}); will retry download next launch."
            )


def get_opening(moves_san: List[str]) -> Optional[Dict[str, str]]:
    build_or_load_openings()
    for length in range(len(moves_san), 0, -1):
        prefix = " ".join(moves_san[:length])
        if prefix in _OPENINGS_BY_MOVES:
            return _OPENINGS_BY_MOVES[prefix]
    return None


def is_book_move(moves_san: List[str], next_san: str) -> bool:
    build_or_load_openings()
    prefix = " ".join(moves_san)
    allowed_moves = _CHILD_MOVES.get(prefix, set())
    if next_san in allowed_moves:
        return True
    cand = f"{prefix} {next_san}".strip()
    return cand in _OPENINGS_BY_MOVES
