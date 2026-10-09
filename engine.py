import os
import sys
import io
import re
import math
import subprocess
import chess
import chess.pgn
import chess.engine
from typing import Dict, List, Optional, Tuple, Any

from openings import get_opening, is_book_move
import ai_provider

def find_stockfish() -> str:
    if getattr(sys, 'frozen', False):
        app_dir = os.path.dirname(sys.executable)
        meipass = getattr(sys, '_MEIPASS', '')
        candidates = [os.path.join(app_dir, "stockfish.exe")]
        if meipass:
            candidates.append(os.path.join(meipass, "stockfish.exe"))
        for cand in candidates:
            if os.path.exists(cand):
                return cand

    local_dir = os.path.dirname(os.path.abspath(__file__))
    local_cand = os.path.join(local_dir, "stockfish.exe")
    if os.path.exists(local_cand):
        return local_cand

    import shutil
    p = shutil.which("stockfish") or shutil.which("stockfish.exe")
    if p:
        return p

    raise FileNotFoundError(
        "Stockfish engine not found. Please install Stockfish and ensure it is in your PATH "
        "or place 'stockfish.exe' in the application directory."
    )

DEFAULT_TIME_PER_PLY_MS = 15000
STOCKFISH_MULTIPV_ALL = True

_STOCKFISH_PATH: Optional[str] = None
STOCKFISH_THREADS: Optional[int] = None
STOCKFISH_HASH: Optional[int] = None
ENGINE_NAME = "Stockfish"


def get_stockfish_path() -> str:
    global _STOCKFISH_PATH
    if _STOCKFISH_PATH is None:
        _STOCKFISH_PATH = find_stockfish()
    return _STOCKFISH_PATH


def configure_engine_params() -> Tuple[int, int]:
    global STOCKFISH_THREADS, STOCKFISH_HASH
    if STOCKFISH_THREADS is not None and STOCKFISH_HASH is not None:
        return STOCKFISH_THREADS, STOCKFISH_HASH

    threads, hash_mb = 8, 2048
    try:
        import hardware_scanner
        hw = hardware_scanner.get_system_hardware()
        rec_threads = int((hw.get("cpu") or {}).get("threads_rec") or 0)
        if rec_threads > 0:
            threads = max(1, min(rec_threads, 16))
        total_ram_gb = float((hw.get("ram") or {}).get("total_gb") or 0.0)
        if total_ram_gb > 0:
            hash_mb = int(min(8192, max(128, (total_ram_gb / 2.0) * 1024)))
    except Exception as exc:
        print(f"[Stockfish] Hardware-aware config unavailable ({exc}); using defaults.")

    STOCKFISH_THREADS, STOCKFISH_HASH = threads, hash_mb
    return STOCKFISH_THREADS, STOCKFISH_HASH


def __getattr__(name: str):
    if name == "STOCKFISH_PATH":
        return get_stockfish_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def cp_to_win_prob(cp: float) -> float:
    if cp >= 10000:
        return 100.0
    if cp <= -10000:
        return 0.0
    val = 2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0
    return 50.0 + 50.0 * val


def move_accuracy(win_loss: float) -> float:
    loss = max(0.0, win_loss)
    acc = 103.1668 * math.exp(-0.04354 * loss) - 3.1669
    return max(0.0, min(100.0, acc))


def parse_score(score_obj, perspective_color: chess.Color) -> Tuple[float, Optional[int]]:
    pov = score_obj.pov(perspective_color)
    if pov.is_mate():
        m = pov.mate()
        score = 10000.0 - (m * 10) if m > 0 else -10000.0 - (m * 10)
        return float(score), m
    return float(pov.score()), None


PIECE_VALUES_CP = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 320,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 20000,
}


def static_exchange_eval(board: chess.Board, move: chess.Move) -> int:
    dest = move.to_square
    mover = board.piece_at(move.from_square)
    target = board.piece_at(dest)
    if not mover:
        return 0

    gain = [PIECE_VALUES_CP.get(target.piece_type, 0) if target else 0]
    b = board.copy(stack=False)
    b.push(move)
    side = b.turn

    while True:
        attackers = list(b.attackers(side, dest))
        if not attackers:
            break
        least_sq = min(attackers, key=lambda sq: PIECE_VALUES_CP.get(b.piece_at(sq).piece_type, 99999))
        piece = b.piece_at(least_sq)
        val = PIECE_VALUES_CP.get(b.piece_at(dest).piece_type, 0)
        gain.append(val - gain[-1])
        m = chess.Move(least_sq, dest)
        if piece.piece_type == chess.PAWN and (chess.square_rank(dest) in [0, 7]):
            m.promotion = chess.QUEEN
        if not b.is_legal(m):
            break
        b.push(m)
        side = b.turn

    while len(gain) > 1:
        g = gain.pop()
        if g > 0:
            gain[-1] = max(gain[-1], -g)
    return gain[0]


def is_genuine_sacrifice(
    board_before: chess.Board,
    move: chess.Move,
    eval_before_cp: float,
    win_loss: float,
    best_move_san: str,
    san_move: str,
    cpl: float = 0.0
) -> bool:
    is_best_or_excellent = (san_move == best_move_san) or (cpl <= 35 and win_loss < 3.0)
    if not is_best_or_excellent:
        return False

    if move.promotion == chess.QUEEN:
        return False
    if board_before.is_check():
        return False

    mover_piece = board_before.piece_at(move.from_square)
    if not mover_piece or mover_piece.piece_type in [chess.PAWN, chess.KING]:
        return False

    moved_val = PIECE_VALUES_CP[mover_piece.piece_type]
    dest = move.to_square
    captured_piece = board_before.piece_at(dest)
    captured_val = PIECE_VALUES_CP[captured_piece.piece_type] if captured_piece else 0

    if captured_val >= moved_val:
        return False

    see_val = static_exchange_eval(board_before, move)
    if see_val >= 0:
        return False

    opp_color = not board_before.turn
    enemy_attackers = board_before.attackers(opp_color, dest)
    if enemy_attackers:
        enemy_vals = [
            PIECE_VALUES_CP[p.piece_type]
            for sq in enemy_attackers
            if (p := board_before.piece_at(sq)) is not None and p.piece_type in PIECE_VALUES_CP
        ]
        min_enemy_val = min(enemy_vals, default=PIECE_VALUES_CP[chess.PAWN])
        friendly_defenders = [sq for sq in board_before.attackers(board_before.turn, dest) if sq != move.from_square]
        if len(friendly_defenders) > 0 and min_enemy_val >= moved_val:
            return False
        if len(friendly_defenders) >= len(enemy_attackers) and min_enemy_val >= moved_val:
            return False

    return see_val <= -180


def is_great_move(
    best_move_san: str,
    san_move: str,
    candidate_lines: List[Dict[str, Any]],
    win_loss: float,
    is_sac: bool,
    color: chess.Color
) -> bool:
    if is_sac:
        return False
    if san_move != best_move_san or win_loss > 1.0:
        return False
    if not candidate_lines or len(candidate_lines) < 2:
        return False

    line1 = candidate_lines[0]
    line2 = candidate_lines[1]

    score1_white = line1.get("score_cp")
    score2_white = line2.get("score_cp")
    if score1_white is None or score2_white is None:
        return False

    is_white = (color == chess.WHITE)
    score1_mover = score1_white if is_white else -score1_white
    score2_mover = score2_white if is_white else -score2_white

    gap_cp = score1_mover - score2_mover
    win_prob_1 = cp_to_win_prob(score1_mover)
    win_prob_2 = cp_to_win_prob(score2_mover)
    win_prob_gap = win_prob_1 - win_prob_2

    if score1_mover < -400:
        return False
    if score2_mover > 700 and win_prob_gap < 8.0:
        return False

    if gap_cp >= 150 and win_prob_gap >= 8.0:
        return True
    if gap_cp >= 250 and win_prob_gap >= 5.0:
        return True
    if win_prob_gap >= 15.0:
        return True

    return False


_OPPONENT_RULES: List[Tuple[str, str]] = [
    (r"I wanted you to play\s+(?P<mv0>[A-Za-z0-9+#=-]+)", r"Opponent should have played \g<mv0>"),
    (r"I wanted you to play", "Opponent should have played"),
    (r"I'?m thrilled you played", "Opponent played a strong move with"),
    (r"I am thrilled you played", "Opponent played a strong move with"),
    (r"By playing\s+(?P<mv4>[A-Za-z0-9+#=-]+),\s+you", r"By playing \g<mv4>, opponent"),
    (r"\byou passively concede\b", "opponent passively concedes"),
    (r"\byou concede\b", "opponent concedes"),
    (r"\byou allow me to\b", "allowing you to"),
    (r"\ballow me to\b", "allowing you to"),
    (r"\byou allow\b", "opponent allows"),
    (r"\byou played\b", "opponent played"),
    (r"\byou found\b", "opponent found"),
    (r"\byou missed\b", "opponent missed"),
    (r"\byou blundered\b", "opponent blundered"),
    (r"\byou sacrificed\b", "opponent sacrificed"),
    (r"\byou drop\b", "opponent drops"),
    (r"\byou lose\b", "opponent loses"),
    (r"\byou bring\b", "opponent brings"),
    (r"\byou are\b", "opponent is"),
    (r"\byou have\b", "opponent has"),
    (r"\byou can\b", "opponent can"),
    (r"\byour queen\b", "opponent's queen"),
    (r"\byour king\b", "opponent's king"),
    (r"\byour pieces\b", "opponent's pieces"),
    (r"\byour piece\b", "opponent's piece"),
    (r"\byour pawn\b", "opponent's pawn"),
    (r"\byour knight\b", "opponent's knight"),
    (r"\byour bishop\b", "opponent's bishop"),
    (r"\byour rook\b", "opponent's rook"),
    (r"\byour position\b", "opponent's position"),
    (r"\byour advantage\b", "opponent's advantage"),
    (r"\byour\b", "their"),
    (r"\byou\b", "opponent"),
    (r"\bto me\b", "to you"),
    (r"\bmy center\b", "your center"),
    (r"\bmy calculation[s]?\b", "the calculation"),
    (r"\bI calculated\b", "It was calculated"),
    (r"\bI saw\b", "It was seen"),
    (r"\bI recommend\b", "you should"),
]
_OPPONENT_PATTERN = re.compile(
    "|".join(f"(?P<r{i}>{pat})" for i, (pat, _) in enumerate(_OPPONENT_RULES)),
    flags=re.I,
)
def _opponent_repl(m: "re.Match") -> str:
    for i, (_, repl) in enumerate(_OPPONENT_RULES):
        val = m.group(f"r{i}")
        if val is None:
            continue
        if "\\" in repl or "\\1" in repl:
            return m.expand(repl)
        if val[:1].isupper():
            return repl[:1].upper() + repl[1:]
        return repl
    return m.group(0)


def transform_to_opponent(text: str, san: str, cat: str, best_move: str = "", color: str = "") -> str:
    if cat == "BOOK":
        return f"Opponent is following standard grandmaster opening theory with {san}."
    if not text:
        if cat == "BLUNDER":
            return f"Opponent blunders with {san}, giving you a decisive advantage!"
        if cat == "MISTAKE":
            return f"Opponent makes a mistake with {san}, handing you the initiative."
        if cat == "INACCURACY":
            return f"Opponent slips with {san}, giving you an opportunity to seize control."
        if cat == "BEST":
            return f"Opponent plays the strongest move in the position with {san}."
        return f"Opponent played {san}."

    s = text.strip()
    if s.lower().startswith("opponent"):
        return s

    return _OPPONENT_PATTERN.sub(_opponent_repl, s)


def transform_headline(headline: str, san: str, cat: str) -> str:
    if cat == "BOOK":
        return f"Book: {san}"
    if cat == "MISS":
        return f"Opponent missed a chance with {san}"
    if cat == "BLUNDER":
        return f"Opponent blunders with {san}!"
    if cat == "MISTAKE":
        return f"Opponent makes a mistake with {san}"
    if cat == "INACCURACY":
        return f"Opponent slips with {san}"
    if cat == "BEST":
        return f"Opponent plays best move: {san}"
    if cat == "EXCELLENT":
        return f"Opponent plays strong move: {san}"
    if cat == "GOOD":
        return f"Opponent plays {san}"
    return f"Opponent plays {san}"


class AnalysisSession:

    def __init__(self, time_per_ply_ms: int = DEFAULT_TIME_PER_PLY_MS):
        self.time_per_ply_ms = time_per_ply_ms
        self.engine = None

    def start(self):
        global ENGINE_NAME
        threads, hash_mb = configure_engine_params()
        print(f"[Stockfish] Launching (Threads={threads}, Hash={hash_mb}MB, Time={self.time_per_ply_ms}ms/ply)...")
        popen_args = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            popen_args["startupinfo"] = si
            popen_args["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.engine = chess.engine.SimpleEngine.popen_uci(get_stockfish_path(), **popen_args)
        self.engine.configure({
            "Threads": threads,
            "Hash": hash_mb
        })
        try:
            uci_id = self.engine.id.get("name", "")
            if uci_id:
                ENGINE_NAME = uci_id
        except Exception:
            pass

    def stop(self):
        if self.engine:
            try:
                self.engine.quit()
            except Exception:
                pass
            self.engine = None

    def analyze_game(self, pgn_text: str, progress_callback=None) -> Dict[str, Any]:
        game = chess.pgn.read_game(sys.stdin if pgn_text == "-" else io.StringIO(pgn_text))
        if not game:
            raise ValueError("Could not parse PGN.")

        white_name = game.headers.get("White", "White")
        black_name = game.headers.get("Black", "Black")
        white_elo = game.headers.get("WhiteElo", "?")
        black_elo = game.headers.get("BlackElo", "?")
        eco_header = game.headers.get("ECO", "")
        opening_header = game.headers.get("ECOUrl", "").split("/")[-1].replace("-", " ") or \
            (f"ECO: {eco_header}" if eco_header else "Opening")

        moves = list(game.mainline_moves())
        total_plies = len(moves)

        board = game.board()
        san_history: List[str] = []
        moves_data: List[Dict[str, Any]] = []

        stats = {
            "white": {"accuracies": [], "categories": {c: 0 for c in ["BRILLIANT", "GREAT", "BOOK", "BEST", "EXCELLENT", "GOOD", "INACCURACY", "MISTAKE", "MISS", "BLUNDER"]}},
            "black": {"accuracies": [], "categories": {c: 0 for c in ["BRILLIANT", "GREAT", "BOOK", "BEST", "EXCELLENT", "GOOD", "INACCURACY", "MISTAKE", "MISS", "BLUNDER"]}}
        }

        prev_move_was_blunder = False
        limit_time = max(0.5, self.time_per_ply_ms / 1000.0)

        init_limit = chess.engine.Limit(time=min(limit_time, 2.0))
        num_legal_s0 = max(1, len(list(board.legal_moves)))
        analysis_s0 = self.engine.analyse(board, init_limit, multipv=num_legal_s0)
        s0_pv = analysis_s0[0].get("pv", [])
        prev_best_move_uci = s0_pv[0].uci() if s0_pv else ""
        prev_best_move_san = board.san(s0_pv[0]) if s0_pv else ""
        prev_best_pv_san = board.variation_san(s0_pv[:10]) if s0_pv else ""
        prev_best_score_cp, prev_best_mate = parse_score(analysis_s0[0]["score"], chess.WHITE)
        prev_win_prob = cp_to_win_prob(prev_best_score_cp)
        prev_best_arrow = [prev_best_move_uci[:2], prev_best_move_uci[2:4]] if prev_best_move_uci else None
        prev_top_lines = []
        for line_entry in analysis_s0:
            pv = line_entry.get("pv", [])
            if pv:
                l_score_white, _ = parse_score(line_entry["score"], chess.WHITE)
                prev_top_lines.append({
                    "move": board.san(pv[0]),
                    "score_cp": l_score_white
                })

        in_book = True

        for ply_idx, move in enumerate(moves):
            color = board.turn
            player_key = "white" if color == chess.WHITE else "black"
            fullmove = board.fullmove_number
            san_move = board.san(move)
            san_history.append(san_move)

            opening_info = get_opening(san_history)
            current_opening_name = opening_info["name"] if opening_info else opening_header

            board_before = board.copy(stack=False)
            best_arrow = prev_best_arrow
            best_move_san = prev_best_move_san
            best_pv_san = prev_best_pv_san

            board.push(move)

            num_legal_after = max(1, len(list(board.legal_moves)))
            analysis_after = self.engine.analyse(
                board,
                chess.engine.Limit(time=limit_time),
                multipv=num_legal_after
            )

            actual_depth = analysis_after[0].get("depth", 20)
            score_white_cp, white_mate = parse_score(analysis_after[0]["score"], chess.WHITE)
            score_mover_cp, mover_mate = parse_score(analysis_after[0]["score"], color)

            is_book = False
            if in_book:
                in_theory = (ply_idx < 25 and is_book_move(san_history[:-1], san_move))
                eval_balanced = (white_mate is None and -70 <= score_white_cp <= 70)
                if in_theory and eval_balanced:
                    is_book = True
                else:
                    in_book = False

            win_after = cp_to_win_prob(score_mover_cp)
            win_loss = max(0.0, prev_win_prob - win_after)
            cpl = max(0.0, prev_best_score_cp - score_mover_cp)
            acc = move_accuracy(win_loss)
            is_sac = is_genuine_sacrifice(board_before, move, prev_best_score_cp, win_loss, best_move_san, san_move, cpl)

            has_forced_mate = (prev_best_mate is not None and prev_best_mate > 0)
            missed_forced_mate = False
            if has_forced_mate and san_move != best_move_san:
                if mover_mate is None or mover_mate > prev_best_mate:
                    missed_forced_mate = True

            walked_into_mate = False
            if (prev_best_mate is None or prev_best_mate <= 0) and mover_mate is not None:
                if mover_mate < 0:
                    walked_into_mate = True

            missed_tactic = False
            if not is_book and not missed_forced_mate and san_move != best_move_san:
                if prev_move_was_blunder and win_loss >= 12.0:
                    missed_tactic = True
                elif prev_best_score_cp >= 180 and (prev_best_score_cp - score_mover_cp) >= 220 and win_loss >= 14.0:
                    missed_tactic = True

            if missed_forced_mate:
                win_loss = max(win_loss, 22.0)
                acc = max(15.0, min(50.0, move_accuracy(win_loss)))
            elif missed_tactic:
                win_loss = max(win_loss, 16.0)
                acc = max(20.0, min(55.0, move_accuracy(win_loss)))
            elif walked_into_mate:
                win_loss = max(win_loss, 28.0)
                acc = max(10.0, min(40.0, move_accuracy(win_loss)))

            stats[player_key]["accuracies"].append(acc)

            if white_mate:
                white_score_str = f"+M{white_mate}" if white_mate > 0 else f"-M{abs(white_mate)}"
            else:
                white_score_str = f"{score_white_cp/100.0:+.2f}"

            top_lines_structured = []
            top_lines_san_desc = []
            for i, line_entry in enumerate(analysis_after):
                pv = line_entry.get("pv", [])
                if pv:
                    pv_str = board.variation_san(pv[:10])
                    m_san = board.san(pv[0])
                    l_score_white, l_mate_white = parse_score(line_entry["score"], chess.WHITE)
                    l_eval_str = f"{l_score_white/100.0:+.2f}" if not l_mate_white else (f"+M{l_mate_white}" if l_mate_white > 0 else f"-M{abs(l_mate_white)}")
                    top_lines_structured.append({
                        "rank": i + 1,
                        "move": m_san,
                        "uci": pv[0].uci(),
                        "score_str": l_eval_str,
                        "score_cp": l_score_white,
                        "pv": pv_str
                    })
                    top_lines_san_desc.append(f"Line {i+1}: {pv_str} ({l_eval_str})")

            is_great = is_great_move(best_move_san, san_move, prev_top_lines, win_loss, is_sac, color)
            refutation_pv = analysis_after[0].get("pv", [])
            refutation_san = board.variation_san(refutation_pv[:10]) if refutation_pv else ""
            threat_arrow = [refutation_pv[0].uci()[:2], refutation_pv[0].uci()[2:4]] if refutation_pv else None
            continuation_best_san = board.san(refutation_pv[0]) if refutation_pv else ""

            if is_book:
                coach_data = {
                    "headline": f"Book: {current_opening_name.split(':')[0]}",
                    "classification": "BOOK",
                    "explanation": f"Following standard grandmaster opening theory in the {current_opening_name}.",
                    "deep_explain": f"This move maintains theoretical balance in the {current_opening_name}.",
                    "opponent_headline": f"Book: {san_move}",
                    "opponent_explanation": f"Opponent is following standard grandmaster opening theory with {san_move} in the {current_opening_name}.",
                    "opponent_deep_explain": f"Opponent continues theoretical book development in the {current_opening_name}."
                }
            else:
                if is_sac:
                    expected_classification = "BRILLIANT"
                elif is_great:
                    expected_classification = "GREAT"
                elif missed_forced_mate or missed_tactic:
                    expected_classification = "MISS"
                elif walked_into_mate:
                    expected_classification = "MISTAKE" if prev_best_score_cp <= -500 else "BLUNDER"
                elif san_move == best_move_san or (cpl <= 12 and win_loss < 1.0):
                    expected_classification = "BEST"
                elif cpl <= 35 and win_loss < 3.0:
                    expected_classification = "EXCELLENT"
                elif cpl <= 80 and win_loss < 7.0:
                    expected_classification = "GOOD"
                elif cpl > 320 or win_loss >= 25.0:
                    expected_classification = "BLUNDER"
                elif cpl > 180 or win_loss >= 14.0:
                    expected_classification = "MISTAKE"
                elif cpl > 80 or win_loss >= 7.0:
                    expected_classification = "INACCURACY"
                else:
                    expected_classification = "GOOD"

                best_score_disp = f"+M{prev_best_mate}" if prev_best_mate else f"{prev_best_score_cp/100.0:+.2f}"
                prompt = (
                    f"Game: {white_name} vs {black_name}\n"
                    f"Opening: {current_opening_name}\n"
                    f"Move {fullmove}{'.' if color == chess.WHITE else '...'} {san_move} played by {player_key.capitalize()}.\n"
                    f"Position FEN: {board.fen()}\n\n"
                    f"STOCKFISH ENGINE EVALUATION OF ALL {len(top_lines_structured)} LEGAL MOVES:\n"
                    f"- Best Move on Board: {best_move_san} (Evaluation: {best_score_disp}, Line: {best_pv_san})\n"
                    f"- Move Played by {player_key.capitalize()}: {san_move} (Evaluation: {white_score_str}, CPL: {cpl:.0f}cp, Win Loss: {win_loss:.1f}%)\n"
                    f"- Refutation / Defense after Played Move: {refutation_san}\n\n"
                    f"FULL RANKED TABLE OF ALL LEGAL MOVES (Best to Worst):\n  " + "\n  ".join(top_lines_san_desc) + "\n\n"
                    f"ENGINE CLASSIFICATION GUIDELINE:\n"
                    f"- Calculated Classification: {expected_classification}\n\n"
                    f"FLAGS:\n"
                    f"- Material Sacrifice: {is_sac}\n"
                    f"- Only Move / Great Move: {is_great}\n"
                    f"- Previous Move was Blunder: {prev_move_was_blunder}\n"
                    f"- Checkmate Detected: {white_mate if white_mate else 'None'}\n\n"
                    f"Analyze this move as the Grandmaster Coach speaking directly to {player_key.capitalize()}."
                )
                coach_data = ai_provider.get_coach_review(prompt)

            classification = coach_data.get("classification", "GOOD")

            if not is_book:
                if is_sac:
                    classification = "BRILLIANT"
                elif is_great:
                    classification = "GREAT"
                elif missed_forced_mate or missed_tactic:
                    classification = "MISS"
                elif walked_into_mate:
                    classification = "MISTAKE" if prev_best_score_cp <= -500 else "BLUNDER"
                elif san_move == best_move_san or (cpl <= 12 and win_loss < 1.0):
                    classification = "BEST"
                elif cpl <= 35 and win_loss < 3.0:
                    classification = "EXCELLENT" if classification not in ["BEST", "BRILLIANT", "GREAT"] else classification
                elif cpl <= 80 and win_loss < 7.0:
                    classification = "GOOD" if classification not in ["EXCELLENT", "BEST", "GREAT", "BRILLIANT"] else classification
                elif cpl > 320 or win_loss >= 25.0:
                    classification = "BLUNDER"
                elif cpl > 180 or win_loss >= 14.0:
                    classification = "MISTAKE"
                elif cpl > 80 or win_loss >= 7.0:
                    classification = "INACCURACY"
                else:
                    classification = "GOOD"

            stats[player_key]["categories"][classification] += 1
            prev_move_was_blunder = (classification in ["BLUNDER", "MISS"])

            if classification == "MISS":
                if missed_forced_mate:
                    headline = f"{san_move} is a miss"
                    explanation = f"You missed a forced checkmate in {prev_best_mate} with {best_move_san}! While {san_move} keeps a winning advantage, {best_move_san} would have delivered immediate checkmate."
                    deep_explain = f"I calculated a forced mate starting with {best_pv_san}. Playing {san_move} lets your opponent off the hook from immediate mate."
                    coach_data["opponent_headline"] = f"Opponent missed mate with {san_move}"
                    coach_data["opponent_explanation"] = f"Opponent missed an immediate checkmate in {prev_best_mate} with {best_move_san}."
                    coach_data["opponent_deep_explain"] = f"{player_key.capitalize()} had a forced checkmate with {best_pv_san}, but played {san_move} instead."
                else:
                    headline = f"{san_move} is a miss"
                    explanation = f"You missed a winning tactical opportunity with {best_move_san}."
                    deep_explain = f"The tactical continuation was {best_pv_san}."
                    coach_data["opponent_headline"] = f"Opponent missed tactical win with {san_move}"
                    coach_data["opponent_explanation"] = f"Opponent missed {best_move_san}."
                    coach_data["opponent_deep_explain"] = f"Opponent failed to find {best_pv_san}, letting you back into the game."
            elif walked_into_mate:
                headline = f"{san_move} walks into checkmate"
                explanation = f"Playing {san_move} allows your opponent to force checkmate in {abs(mover_mate)} moves with {continuation_best_san}."
                deep_explain = f"Your opponent's refutation line is {refutation_san}."
                coach_data["opponent_headline"] = f"Opponent walks into mate with {san_move}"
                coach_data["opponent_explanation"] = f"Opponent's {san_move} allows you to force checkmate in {abs(mover_mate)} moves with {continuation_best_san}!"
                coach_data["opponent_deep_explain"] = f"You have a forced mate with {refutation_san}."
            else:
                headline = coach_data.get("headline", f"{san_move} is {classification.lower()}")
                explanation = coach_data.get("explanation", "")
                deep_explain = coach_data.get("deep_explain", "")
                if headline == "Solid move" or "natural move keeping" in explanation:
                    if classification == "BEST":
                        headline = f"Best move: {san_move}"
                        explanation = f"You found the best move on the board with {san_move}."
                    elif classification == "EXCELLENT":
                        headline = f"{san_move} is excellent"
                        explanation = f"{san_move} keeps your position fully sound; {best_move_san} was only marginally stronger." if best_move_san else f"{san_move} keeps your position fully sound."
                    elif classification == "GOOD":
                        headline = f"{san_move} is good"
                        explanation = f"{san_move} keeps your position playable. {best_move_san} was slightly more accurate." if best_move_san else f"{san_move} keeps your position playable."
                    elif classification == "INACCURACY":
                        headline = f"{san_move} is an inaccuracy"
                        explanation = f"{san_move} concedes a small edge. {best_move_san} was more accurate." if best_move_san else f"{san_move} concedes a small edge."
                    elif classification == "MISTAKE":
                        headline = f"{san_move} is a mistake"
                        explanation = f"{san_move} drops an advantage. {best_move_san} was better."
                    elif classification == "BLUNDER":
                        headline = f"{san_move} blunders"
                        explanation = f"{san_move} blunders into trouble. {best_move_san} was required."

            move_record = {
                "ply": ply_idx,
                "fullmove": fullmove,
                "color": "white" if color == chess.WHITE else "black",
                "san": san_move,
                "uci": move.uci(),
                "fen": board.fen(),
                "score_cp": score_white_cp,
                "score_str": white_score_str,
                "win_prob": round(win_after, 1),
                "win_loss": round(win_loss, 1),
                "cpl": round(cpl, 1),
                "accuracy": round(acc, 1),
                "classification": classification,
                "headline": headline,
                "explanation": explanation,
                "deep_explain": deep_explain,
                "opponent_headline": coach_data.get("opponent_headline") or transform_headline(headline, san_move, classification),
                "opponent_explanation": coach_data.get("opponent_explanation") or transform_to_opponent(explanation, san_move, classification, best_move_san, player_key),
                "opponent_deep_explain": coach_data.get("opponent_deep_explain") or transform_to_opponent(deep_explain, san_move, classification, best_move_san, player_key),
                "best_move_san": best_move_san,
                "best_line_san": best_pv_san,
                "best_arrow": best_arrow,
                "threat_arrow": threat_arrow if classification in ["BLUNDER", "MISTAKE", "MISS", "INACCURACY"] else None,
                "opening": current_opening_name,
                "depth": actual_depth,
                "time_limit_s": limit_time,
                "engine_name": ENGINE_NAME,
                "threads": STOCKFISH_THREADS,
                "hash_mb": STOCKFISH_HASH,
                "continuation_best_san": continuation_best_san,
                "top_lines": top_lines_structured
            }
            moves_data.append(move_record)

            if refutation_pv:
                prev_best_move_uci = refutation_pv[0].uci()
                prev_best_move_san = continuation_best_san
                prev_best_pv_san = refutation_san
                prev_best_score_cp, prev_best_mate = parse_score(analysis_after[0]["score"], board.turn)
                prev_win_prob = cp_to_win_prob(prev_best_score_cp)
                prev_best_arrow = [prev_best_move_uci[:2], prev_best_move_uci[2:4]]
            else:
                prev_best_move_uci = ""
                prev_best_move_san = ""
                prev_best_pv_san = ""
                prev_best_score_cp, prev_best_mate = parse_score(analysis_after[0]["score"], board.turn)
                prev_win_prob = cp_to_win_prob(prev_best_score_cp)
                prev_best_arrow = None
            prev_top_lines = top_lines_structured

            if progress_callback:
                progress_callback(move_record, ply_idx + 1, total_plies)

        white_overall = sum(stats["white"]["accuracies"]) / max(1, len(stats["white"]["accuracies"]))
        black_overall = sum(stats["black"]["accuracies"]) / max(1, len(stats["black"]["accuracies"]))

        return {
            "white": {"name": white_name, "elo": white_elo, "accuracy": round(white_overall, 1), "categories": stats["white"]["categories"]},
            "black": {"name": black_name, "elo": black_elo, "accuracy": round(black_overall, 1), "categories": stats["black"]["categories"]},
            "moves": moves_data,
            "opening": moves_data[-1]["opening"] if moves_data and moves_data[-1].get("opening") else opening_header,
            "engine_name": ENGINE_NAME
        }
