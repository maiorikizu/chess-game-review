import sys
import os
import re
import json
import time
import base64
import ctypes
import requests
from typing import Dict, Any, Optional, List

import app_paths

APPDATA_DIR = app_paths.get_state_dir()
PERSISTENT_CONFIG_PATH = os.path.join(APPDATA_DIR, "ai_config.json")

_LEGACY_APPDATA_PATH = os.path.join(
    os.environ.get('APPDATA', os.path.expanduser('~')),
    'ChessReviewDesktop', 'ai_config.json'
)
_LEGACY_EXE_PATH = os.path.join(app_paths.get_app_dir(), "ai_config.json")

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = CONFIG_DIR
CONFIG_PATH = PERSISTENT_CONFIG_PATH

_DPAPI_PREFIX = "dpapi:"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_crypt(protect: bool, data: bytes) -> Optional[bytes]:
    if sys.platform != "win32":
        return None
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
        if fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception as e:
        print(f"[AI Provider] DPAPI {'protect' if protect else 'unprotect'} failed: {e}")
    return None


def _protect_secret(value: str) -> str:
    if not value or value.startswith(_DPAPI_PREFIX):
        return value
    enc = _dpapi_crypt(True, value.encode("utf-8"))
    if enc is None:
        return value
    return _DPAPI_PREFIX + base64.b64encode(enc).decode("ascii")


def _unprotect_secret(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if not value.startswith(_DPAPI_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(_DPAPI_PREFIX):])
    except Exception:
        return ""
    dec = _dpapi_crypt(False, raw)
    if dec is None:
        return ""
    try:
        return dec.decode("utf-8")
    except UnicodeDecodeError:
        return ""

LOCAL_MODELS = [
    {
        "id": "Ornith-1.0-9B",
        "name": "Ornith-1.0 9B (Unsloth)",
        "hf_url": "https://huggingface.co/unsloth/Ornith-1.0-9B-GGUF",
        "quant": "Q4_K_S",
        "recommended": True,
        "desc": "Specialized instruction-tuned 9B tactical refutation model (~33.4 tok/s)."
    },
    {
        "id": "Qwen3.5-9B-MTP",
        "name": "Qwen 3.5 9B MTP (Unsloth)",
        "hf_url": "https://huggingface.co/unsloth/Qwen3.5-9B-MTP-GGUF",
        "quant": "Q4_K_S",
        "recommended": False,
        "desc": "Ultra-fast tactical analysis with multi-token prediction (~33.4 tok/s)."
    },
    {
        "id": "gemma-4-12b-it",
        "name": "Gemma 12B Standard (Google / Unsloth)",
        "hf_url": "https://huggingface.co/unsloth/gemma-4-12b-it-GGUF",
        "quant": "Q4_K_M",
        "recommended": False,
        "desc": "High quality reasoning, compact 12B footprint. Perfect for GM analysis."
    },
    {
        "id": "gemma-4-12b-it-qat",
        "name": "Gemma 12B QAT (Google / Unsloth)",
        "hf_url": "https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF",
        "quant": "Q4_K_XL",
        "recommended": False,
        "desc": "Quantization-Aware Training: lossless 4-bit weights matching full FP16 accuracy."
    },
    {
        "id": "Qwen3.5-4B-MTP",
        "name": "Qwen 3.5 4B MTP (Unsloth)",
        "hf_url": "https://huggingface.co/unsloth/Qwen3.5-4B-MTP-GGUF",
        "quant": "Q8_0",
        "recommended": False,
        "desc": "Ultra-lightweight 4B model at high quality (~66.1 tok/s)."
    }
]

NIM_FAILOVER_CASCADE = [
    {"name": "Nemotron 3 Super 120B A12B", "model": "meta/nemotron-3-super-120b-a12b", "creator": "NVIDIA", "max_attempts": 2},
    {"name": "Nemotron 3 Ultra 550B A55B", "model": "meta/nemotron-3-ultra-550b-a55b", "creator": "NVIDIA", "max_attempts": 2},
    {"name": "gpt-oss-20b", "model": "openai/gpt-oss-20b", "creator": "OpenAI", "max_attempts": 2},
    {"name": "Nemotron 3 Nano Omni 30B A3B", "model": "meta/nemotron-3-nano-omni-30b-a3b", "creator": "NVIDIA", "max_attempts": 2},
    {"name": "Gemma 4 31B", "model": "google/gemma-4-31b", "creator": "Google", "max_attempts": 2},
    {"name": "DiffusionGemma 26B A4B", "model": "google/diffusiongemma-26b-a4b", "creator": "Google", "max_attempts": 2},
]

DEFAULT_CONFIG = {
    "provider": "local",
    "local_model": "Ornith-1.0-9B",
    "local_endpoint": "http://localhost:8888/v1",
    "allow_cloud_api": False,
    "nim_api_key": "",
    "openai_api_key": "",
    "claude_api_key": "",
    "openrouter_api_key": ""
}


def load_config() -> Dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)

    candidate_paths = [
        PERSISTENT_CONFIG_PATH,
        _LEGACY_APPDATA_PATH,
        _LEGACY_EXE_PATH,
    ]

    seen = set()
    for path in candidate_paths:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"[AI Provider] Failed reading config {path}: {e}")
            continue
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                if k.endswith("_key") and not v and merged.get(k):
                    continue
                if v is not None:
                    merged[k] = v
        break

    if not merged.get("nim_api_key"):
        env_key = os.environ.get("NVIDIA_API_KEY", "")
        if env_key:
            merged["nim_api_key"] = env_key

    if merged.get("provider") == "local":
        merged["allow_cloud_api"] = False

    for k in list(merged.keys()):
        if k.endswith("_key"):
            merged[k] = _unprotect_secret(merged[k])

    return merged


def save_config(new_config: Dict[str, Any]):
    cfg = load_config()
    for k, v in new_config.items():
        if k.endswith("_key") and not v and cfg.get(k):
            continue
        cfg[k] = v

    if cfg.get("provider") == "local":
        cfg["allow_cloud_api"] = False

    out_cfg = dict(cfg)
    for k in list(out_cfg.keys()):
        if k.endswith("_key") and out_cfg[k]:
            out_cfg[k] = _protect_secret(str(out_cfg[k]))
    try:
        app_paths.atomic_write_json(PERSISTENT_CONFIG_PATH, out_cfg)
    except Exception as e:
        print(f"[AI Provider] Error saving persistent config: {e}")

    for legacy in (_LEGACY_APPDATA_PATH, _LEGACY_EXE_PATH):
        try:
            if os.path.normcase(os.path.abspath(legacy)) == os.path.normcase(os.path.abspath(PERSISTENT_CONFIG_PATH)):
                continue
            if os.path.exists(legacy):
                os.remove(legacy)
                print(f"[AI Provider] Removed legacy plaintext config: {legacy}")
        except Exception as e:
            print(f"[AI Provider] Could not remove legacy config {legacy}: {e}")

    print(f"[AI Provider] Config updated: provider={cfg.get('provider')}, local_model={cfg.get('local_model')}, allow_cloud_api={cfg.get('allow_cloud_api')}")


def migrate_legacy_configs() -> None:
    for legacy in (_LEGACY_APPDATA_PATH, _LEGACY_EXE_PATH):
        try:
            norm = os.path.normcase(os.path.abspath(legacy))
            if norm == os.path.normcase(os.path.abspath(PERSISTENT_CONFIG_PATH)):
                continue
            if not os.path.exists(legacy):
                continue
            if not os.path.exists(PERSISTENT_CONFIG_PATH):
                with open(legacy, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    save_config(data)
            if os.path.exists(legacy):
                os.remove(legacy)
                print(f"[AI Provider] Removed legacy plaintext config: {legacy}")
        except Exception as e:
            print(f"[AI Provider] Legacy config migration failed for {legacy}: {e}")


VALID_CLASSIFICATIONS = ["BRILLIANT", "GREAT", "BOOK", "BEST", "EXCELLENT", "GOOD", "INACCURACY", "MISTAKE", "MISS", "BLUNDER"]

_LOCAL_FAIL_THRESHOLD = 3
_LOCAL_BREAKER_COOLDOWN_S = 300.0
_local_fail_count = 0
_local_circuit_open_until = 0.0


def _normalize_classification(review: Dict[str, Any]) -> Dict[str, Any]:
    raw = review.get("classification")
    if not isinstance(raw, str):
        review["classification"] = "GOOD"
        return review
    cat = raw.strip().upper().replace(" ", "_")
    if cat in VALID_CLASSIFICATIONS:
        review["classification"] = cat
        return review
    hits = [v for v in VALID_CLASSIFICATIONS if v in cat]
    if hits:
        review["classification"] = max(hits, key=len)
    else:
        review["classification"] = "GOOD"
    return review


def strip_thinking_tokens(text: str) -> str:
    text = re.sub(r'<thought>.*?</thought>', '', text, flags=re.DOTALL)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    if "</thought>" in text:
        text = text.split("</thought>")[-1]
    if "</think>" in text:
        text = text.split("</think>")[-1]
    m = re.search(r'<(?:thought|think)>', text, flags=re.I)
    if m:
        text = text[:m.start()]
    return text.strip()


def _extract_balanced_objects(text: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            found.append(obj)
                    except Exception:
                        pass
                    start = -1
    return found


def _pick_review_object(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for obj in candidates:
        if any(k in obj for k in ("headline", "explanation", "classification")):
            return obj
    return candidates[0] if candidates else None


def extract_json_response(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text:
        return None
    clean = strip_thinking_tokens(raw_text)
    if "```" in clean:
        for part in clean.split("```"):
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            picked = _pick_review_object(_extract_balanced_objects(p))
            if picked is not None:
                return picked
    return _pick_review_object(_extract_balanced_objects(clean))


COACH_SYSTEM_PROMPT = (
    "You are the Grandmaster Chess Coach providing an interactive Game Review just like the Chess.com Coach avatar.\n\n"
    "STRICT PERSONA RULES:\n"
    "1. NEVER mention 'Stockfish', 'the engine', 'the computer', 'depth 20', 'centipawns', 'cp', 'eval', '+9980', or win-rate percentages in your speech.\n"
    "2. YOU are the master coach. YOU personally calculated all the deep variations and tactical lines.\n"
    "3. Speak directly to the player in the 1st person ('I calculated...', 'I wanted you to play...', 'You found an astonishing move...', 'This move blunders your knight...').\n"
    "4. Focus on WHY the move works or fails based on the tactical lines:\n"
    "   - Which piece gets trapped, deflected, or pinned?\n"
    "   - What tactical counter-blow did you foresee?\n"
    "   - How does the recommended continuation seize control or defend?\n"
    "5. Keep 'explanation' (spoken coach commentary) and 'deep_explain' (tactical continuation for the Explain drawer) completely DISTINCT.\n"
    "   - 'explanation': 1-2 conversational sentences explaining the general idea or mistake directly to the player.\n"
    "   - 'deep_explain': 2-3 sentences walking through the specific concrete moves and refutation line.\n"
    "6. Also provide 'opponent_headline', 'opponent_explanation', and 'opponent_deep_explain' for when the opponent reviews this move:\n"
    "   - In opponent fields, refer to the player who made the move strictly as 'Opponent' and the person reviewing as 'you'.\n"
    "7. Classify the move into ONE of these 10 categories:\n"
    "   BRILLIANT (!!), GREAT (!), BOOK, BEST (*), EXCELLENT, GOOD, INACCURACY (?!), MISTAKE (?), MISS (X), BLUNDER (??)\n\n"
    "Return ONLY a JSON object with this exact structure:\n"
    "{\n"
    '  "headline": "<3-6 word punchy headline for the mover, e.g. \\"c6 concedes the center\\">",\n'
    '  "classification": "<CATEGORY>",\n'
    '  "explanation": "<1-2 sentence direct conversational coach critique speaking to mover as you>",\n'
    '  "deep_explain": "<2-3 sentence deeper tactical continuation detailing the line>",\n'
    '  "opponent_headline": "<3-6 word headline for opponent review>",\n'
    '  "opponent_explanation": "<1-2 sentences for opponent review>",\n'
    '  "opponent_deep_explain": "<2-3 sentence deeper tactical explanation for opponent review>"\n'
    "}"
)


def _local_breaker_tripped() -> bool:
    return time.time() < _local_circuit_open_until


def _local_call_failed() -> None:
    global _local_fail_count, _local_circuit_open_until
    _local_fail_count += 1
    if _local_fail_count >= _LOCAL_FAIL_THRESHOLD:
        _local_circuit_open_until = time.time() + _LOCAL_BREAKER_COOLDOWN_S
        print(
            f"[Local AI] Circuit breaker OPEN after {_local_fail_count} consecutive failures; "
            f"skipping local calls for {int(_LOCAL_BREAKER_COOLDOWN_S)}s."
        )


def _local_call_succeeded() -> None:
    global _local_fail_count, _local_circuit_open_until
    _local_fail_count = 0
    _local_circuit_open_until = 0.0


def call_local_ai(prompt: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _local_breaker_tripped():
        return None

    raw_endpoint = config.get("local_endpoint", "http://localhost:8888/v1").strip().rstrip("/")
    if raw_endpoint.isdigit():
        raw_endpoint = f"http://localhost:{raw_endpoint}/v1"
    elif not raw_endpoint.startswith("http://") and not raw_endpoint.startswith("https://"):
        raw_endpoint = "http://" + raw_endpoint
    if not raw_endpoint.endswith("/v1"):
        endpoint = raw_endpoint + "/v1"
    else:
        endpoint = raw_endpoint

    url = f"{endpoint}/chat/completions"
    model = config.get("local_model", "gemma-4-12b-it")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": COACH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "stream": False
    }

    try:
        res = requests.post(url, json=payload, timeout=25)
        if res.status_code == 200:
            data = res.json()
            raw_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = extract_json_response(raw_text)
            if parsed:
                _local_call_succeeded()
                return parsed
            _local_call_failed()
            return None
        _local_call_failed()
    except (requests.ConnectionError, requests.Timeout) as e:
        print(f"[Local AI] Request to {url} failed: {e}")
        _local_call_failed()
    except Exception as e:
        print(f"[Local AI] Request to {url} failed: {e}")
        _local_call_failed()

    try:
        base_url = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        ollama_url = f"{base_url}/api/chat"
        ollama_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": COACH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            "stream": False
        }
        res_ollama = requests.post(ollama_url, json=ollama_payload, timeout=25)
        if res_ollama.status_code == 200:
            data = res_ollama.json()
            raw_text = data.get("message", {}).get("content", "")
            parsed = extract_json_response(raw_text)
            if parsed:
                _local_call_succeeded()
            return parsed
    except (requests.ConnectionError, requests.Timeout):
        _local_call_failed()
    except Exception:
        _local_call_failed()

    return None


def call_nim_failover_cascade(prompt: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not config.get("allow_cloud_api", False):
        print("[NIM Cascade Blocked] Privacy Gate Active. Cloud APIs are disabled.")
        return None

    api_key = config.get("nim_api_key") or os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        print("[NIM Cascade] No NVIDIA API key configured.")
        return None

    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    for tier_idx, tier in enumerate(NIM_FAILOVER_CASCADE):
        model_name = tier["model"]
        display_name = tier["name"]
        max_attempts = tier.get("max_attempts", 2)

        for attempt in range(max_attempts):
            print(f"[NIM Cascade Tier {tier_idx+1}/6] Calling {display_name} (Attempt {attempt+1}/{max_attempts})...")
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": COACH_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "stream": False
            }
            try:
                res = requests.post(url, json=payload, headers=headers, timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    raw_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    parsed = extract_json_response(raw_text)
                    if parsed:
                        print(f"[NIM Cascade Success] Received valid review from {display_name}!")
                        parsed["_provider"] = f"NIM: {display_name}"
                        return parsed
                else:
                    print(f"[NIM Cascade] Tier {tier_idx+1} ({display_name}) HTTP {res.status_code}: {res.text[:150]}")
            except Exception as e:
                print(f"[NIM Cascade] Tier {tier_idx+1} ({display_name}) error: {e}")
            time.sleep(0.5)

    print("[NIM Cascade] All 6 tiers exhausted without success.")
    return None


def call_generic_cloud(prompt: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not config.get("allow_cloud_api", False):
        return None

    provider = config.get("provider", "").lower()
    if provider == "openai":
        key = config.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            return None
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": COACH_SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            "temperature": 0.2
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=25)
            if r.status_code == 200:
                raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                return extract_json_response(raw)
        except Exception:
            pass

    elif provider == "claude":
        key = config.get("claude_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return None
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "temperature": 0.2,
            "system": COACH_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=25)
            if r.status_code == 200:
                blocks = r.json().get("content", [])
                raw = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                return extract_json_response(raw)
            else:
                print(f"[Claude] HTTP {r.status_code}: {r.text[:150]}")
        except Exception as e:
            print(f"[Claude] Request failed: {e}")

    elif provider == "openrouter":
        key = config.get("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            return None
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": "auto",
            "messages": [{"role": "system", "content": COACH_SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            "temperature": 0.2
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=25)
            if r.status_code == 200:
                raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                return extract_json_response(raw)
        except Exception:
            pass

    return None


def get_coach_review(prompt: str) -> Dict[str, Any]:
    config = load_config()
    provider = config.get("provider", "local").lower()

    review = None

    if provider == "nim":
        review = call_nim_failover_cascade(prompt, config)
        if not review:
            print("[AI Provider] NIM failed, attempting local AI fallback...")
            review = call_local_ai(prompt, config)

    elif provider in ["openai", "claude", "openrouter"]:
        review = call_generic_cloud(prompt, config)
        if not review:
            review = call_local_ai(prompt, config)

    else:
        review = call_local_ai(prompt, config)
        if not review and config.get("allow_cloud_api") and config.get("nim_api_key"):
            print("[AI Provider] Local AI unreachable, falling back to NIM cascade...")
            review = call_nim_failover_cascade(prompt, config)

    if review and isinstance(review, dict):
        review.pop("_provider", None)
        return _normalize_classification(review)

    return {
        "headline": "Solid move",
        "classification": "GOOD",
        "explanation": "A natural move keeping the position playable.",
        "deep_explain": "This move continues development and maintains structure.",
        "opponent_headline": "Opponent plays solid move",
        "opponent_explanation": "Opponent makes a solid move keeping their position playable.",
        "opponent_deep_explain": "Opponent continues development and maintains structure."
    }
