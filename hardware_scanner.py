import os
import sys
import socket
import platform
import subprocess
import time
import ipaddress
import requests
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _run_powershell(command: str, timeout: int = 10) -> Optional[str]:
    try:
        kwargs = {}
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            encoding="utf-8", stderr=subprocess.DEVNULL, timeout=timeout,
            **kwargs
        )
        out = out.strip()
        return out or None
    except Exception:
        return None


def probe_hardware_gpu() -> Dict[str, Any]:
    gpu_name = "CPU / Integrated Graphics"
    vram_total_gb = 0.0
    vram_free_gb = 0.0
    bandwidth_gbs = 50.0
    fp16_tflops = 5.0
    is_nvidia = False

    try:
        import ctypes
        cuda = ctypes.CDLL("nvcuda.dll")
        cuda.cuInit.argtypes = [ctypes.c_uint]
        cuda.cuInit.restype = ctypes.c_int
        cuda.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        cuda.cuDeviceGet.restype = ctypes.c_int
        cuda.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        cuda.cuDeviceGetName.restype = ctypes.c_int
        cuda.cuDeviceGetAttribute.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int]
        cuda.cuDeviceGetAttribute.restype = ctypes.c_int
        cuda.cuCtxCreate_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_int]
        cuda.cuCtxCreate_v2.restype = ctypes.c_int
        cuda.cuMemGetInfo_v2.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        cuda.cuMemGetInfo_v2.restype = ctypes.c_int
        cuda.cuCtxDestroy_v2.argtypes = [ctypes.c_void_p]
        cuda.cuCtxDestroy_v2.restype = ctypes.c_int

        if cuda.cuInit(0) == 0:
            dev = ctypes.c_int()
            if cuda.cuDeviceGet(ctypes.byref(dev), 0) == 0:
                name_buf = (ctypes.c_char * 256)()
                if cuda.cuDeviceGetName(name_buf, 256, dev) == 0:
                    decoded = name_buf.value.decode("utf-8", errors="ignore").strip()
                    if decoded:
                        gpu_name = decoded
                is_nvidia = True

                mem_clk_khz = ctypes.c_int()
                bus_width_bits = ctypes.c_int()
                sm_count = ctypes.c_int()
                core_clk_khz = ctypes.c_int()

                cuda.cuDeviceGetAttribute(ctypes.byref(mem_clk_khz), 36, dev)
                cuda.cuDeviceGetAttribute(ctypes.byref(bus_width_bits), 37, dev)
                cuda.cuDeviceGetAttribute(ctypes.byref(sm_count), 16, dev)
                cuda.cuDeviceGetAttribute(ctypes.byref(core_clk_khz), 13, dev)

                if mem_clk_khz.value > 0 and bus_width_bits.value > 0:
                    bandwidth_gbs = round((mem_clk_khz.value * 1000 * 2 * (bus_width_bits.value / 8)) / 1e9, 1)

                if sm_count.value > 0 and core_clk_khz.value > 0:
                    fp16_tflops = round((sm_count.value * 128 * 2 * (core_clk_khz.value * 1000)) / 1e12, 1)

                ctx = ctypes.c_void_p()
                if cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) == 0:
                    try:
                        free_b = ctypes.c_size_t()
                        total_b = ctypes.c_size_t()
                        if cuda.cuMemGetInfo_v2(ctypes.byref(free_b), ctypes.byref(total_b)) == 0:
                            vram_free_gb = round(free_b.value / (1024**3), 1)
                            vram_total_gb = round(total_b.value / (1024**3), 1)
                    finally:
                        cuda.cuCtxDestroy_v2(ctx)
    except Exception:
        pass

    if is_nvidia:
        try:
            kwargs = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            smi = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=memory.total,memory.free', '--format=csv,noheader,nounits'],
                encoding='utf-8', stderr=subprocess.DEVNULL,
                **kwargs
            )
            lines = smi.strip().split('\n')
            if lines and lines[0].strip():
                parts = [p.strip() for p in lines[0].split(',')]
                vram_total_gb = round(float(parts[0]) / 1024.0, 1)
                vram_free_gb = round(float(parts[1]) / 1024.0, 1)
        except Exception:
            pass
    else:
        name_out = _run_powershell(
            "(Get-CimInstance Win32_VideoController | "
            "Where-Object { $_.PNPDeviceID -notlike 'ROOT\\*' } | "
            "Select-Object -First 1).Name"
        )
        if name_out:
            gpu_name = name_out

        adapter_ram_out = _run_powershell(
            "(Get-CimInstance Win32_VideoController | "
            "Where-Object { $_.PNPDeviceID -notlike 'ROOT\\*' } | "
            "Select-Object -First 1).AdapterRAM"
        )
        adapter_ram_bytes = 0.0
        try:
            adapter_ram_bytes = float(adapter_ram_out) if adapter_ram_out else 0.0
        except (TypeError, ValueError):
            adapter_ram_bytes = 0.0

        if adapter_ram_bytes >= 0xE0000000 or adapter_ram_bytes <= 0:
            counter_out = _run_powershell(
                "$s = (Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage' "
                "-ErrorAction Stop).CounterSamples | "
                "Sort-Object CookedValue -Descending | Select-Object -First 1; "
                "if ($s) { [math]::Round($s.CookedValue / 1GB, 1) }"
            )
            try:
                if counter_out:
                    vram_total_gb = float(counter_out)
                    vram_free_gb = vram_total_gb
            except (TypeError, ValueError):
                pass
            if vram_total_gb <= 0 and adapter_ram_bytes > 0:
                vram_total_gb = 4.0
                vram_free_gb = 4.0
        elif adapter_ram_bytes > 0:
            vram_total_gb = round(adapter_ram_bytes / (1024**3), 1)
            vram_free_gb = vram_total_gb

        if vram_total_gb >= 16:
            bandwidth_gbs = 900.0
            fp16_tflops = 60.0
        elif vram_total_gb >= 10:
            bandwidth_gbs = 504.0
            fp16_tflops = 40.0
        elif vram_total_gb >= 6:
            bandwidth_gbs = 448.0
            fp16_tflops = 35.0
        elif vram_total_gb >= 4:
            bandwidth_gbs = 224.0
            fp16_tflops = 20.0

    return {
        "name": gpu_name,
        "vram_total_gb": vram_total_gb,
        "vram_free_gb": vram_free_gb,
        "bandwidth_gbs": bandwidth_gbs,
        "fp16_tflops": fp16_tflops,
        "is_nvidia": is_nvidia
    }


QUANT_QUALITY_ORDER = ['Q8_0', 'Q6_K', 'Q5_K_M', 'Q5_K_S', 'Q4_K_XL', 'Q4_K_M', 'Q4_K_S', 'Q4_0', 'IQ4_XS']


def evaluate_model_quants(
    model_def: Dict[str, Any],
    vram_free_gb: float,
    bandwidth_gbs: float
) -> Dict[str, Any]:
    kv_bytes = model_def["kv_bytes_4k"]
    is_mtp = model_def.get("is_mtp", False)
    is_qat = model_def.get("is_qat", False)
    quants = model_def.get("quants", [])

    eff = 0.414
    mtp_gain = 1.65 if is_mtp else 1.0

    vram_budget = max(0.0, vram_free_gb - 0.6)

    evaluated = []
    for q in quants:
        w_bytes = q["bytes"]
        total_req_gb = round((w_bytes + kv_bytes) / 1e9, 2)
        per_token = w_bytes + (kv_bytes // 2)

        base_tps = (eff * bandwidth_gbs * 1e9) / per_token
        tps = round(base_tps * mtp_gain, 1)

        fits = bool(total_req_gb <= vram_budget)
        evaluated.append({
            "q": q["q"],
            "filename": q.get("filename", f"{model_def['id']}-{q['q']}.gguf"),
            "total_req_gb": total_req_gb,
            "tps": tps,
            "fits": fits
        })

    fitting = [x for x in evaluated if x["fits"]]
    if fitting:
        fast_fitting = [x for x in fitting if x["tps"] >= 30.0]
        if fast_fitting:
            fast_fitting.sort(
                key=lambda x: QUANT_QUALITY_ORDER.index(x["q"]) if x["q"] in QUANT_QUALITY_ORDER else 99
            )
            selected = fast_fitting[0]
        else:
            fitting.sort(key=lambda x: -x["tps"])
            selected = fitting[0]
        fits_final = True
        offload_type = "100% GPU Offload"
    else:
        evaluated.sort(key=lambda x: x["total_req_gb"])
        def_q = next((x for x in evaluated if x["q"] in ["Q4_K_M", "Q4_K_XL"]), evaluated[0])
        selected = def_q
        fits_final = False
        offload_type = "Does not fit"

    return {
        "id": model_def["id"],
        "name": model_def["name"],
        "hf_url": model_def["hf_url"],
        "quant_suggested": selected["q"],
        "quant_filename": selected["filename"],
        "fits": fits_final,
        "offload_type": offload_type,
        "vram_req_gb": selected["total_req_gb"],
        "tok_s": selected["tps"],
        "priority": model_def["priority"]
    }


def probe_cpu() -> Dict[str, Any]:
    cpu_name = (
        os.environ.get("PROCESSOR_IDENTIFIER")
        or platform.processor()
        or "x86_64 Multi-core"
    )
    logical_threads = os.cpu_count() or 4
    physical_cores = max(1, logical_threads // 2)

    try:
        import psutil
        p = psutil.cpu_count(logical=False)
        if p and p > 0:
            physical_cores = p
        l = psutil.cpu_count(logical=True)
        if l and l > 0:
            logical_threads = l
    except Exception:
        pass

    if physical_cores == max(1, logical_threads // 2):
        cim_out = _run_powershell(
            "$c = Get-CimInstance Win32_Processor | Select-Object -First 1; "
            "if ($c) { \"$($c.NumberOfCores) $($c.NumberOfLogicalProcessors)\" }"
        )
        if cim_out:
            parts = cim_out.split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                physical_cores = max(1, int(parts[0]))
                logical_threads = max(physical_cores, int(parts[1]))

    return {
        "name": cpu_name,
        "physical_cores": physical_cores,
        "logical_threads": logical_threads,
        "cores": physical_cores,
        "threads": logical_threads,
        "threads_rec": min(physical_cores, 8)
    }


def get_system_hardware() -> Dict[str, Any]:
    cpu_info = probe_cpu()

    ram_total_gb = 8.0
    ram_avail_gb = 4.0
    try:
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        ram_total_gb = round(stat.ullTotalPhys / (1024**3), 1)
        ram_avail_gb = round(stat.ullAvailPhys / (1024**3), 1)
    except Exception:
        pass

    gpu_info = probe_hardware_gpu()
    gpu_name = gpu_info["name"]
    vram_total_gb = gpu_info["vram_total_gb"]
    vram_free_gb = gpu_info["vram_free_gb"]
    bandwidth_gbs = gpu_info["bandwidth_gbs"]
    is_nvidia = gpu_info["is_nvidia"]

    kv_qwen_4k = 159383552
    kv_gemma_4k = 872415232

    model_defs = [
        {
            "id": "Ornith-1.0-9B",
            "name": "Ornith-1.0 9B",
            "hf_url": "https://huggingface.co/unsloth/Ornith-1.0-9B-GGUF",
            "priority": 1,
            "is_mtp": False,
            "is_qat": False,
            "kv_bytes_4k": kv_qwen_4k,
            "quants": [
                {"q": "Q8_0", "bytes": 9527502048, "filename": "Ornith-1.0-9B-UD-Q8_0.gguf"},
                {"q": "Q6_K", "bytes": 7458301152, "filename": "Ornith-1.0-9B-UD-Q6_K.gguf"},
                {"q": "Q5_K_M", "bytes": 6577841376, "filename": "Ornith-1.0-9B-UD-Q5_K_M.gguf"},
                {"q": "Q5_K_S", "bytes": 6361146592, "filename": "Ornith-1.0-9B-UD-Q5_K_S.gguf"},
                {"q": "Q4_K_XL", "bytes": 5966095584, "filename": "Ornith-1.0-9B-UD-Q4_K_XL.gguf"},
                {"q": "Q4_K_M", "bytes": 5680522464, "filename": "Ornith-1.0-9B-UD-Q4_K_M.gguf"},
                {"q": "Q4_K_S", "bytes": 5394097376, "filename": "Ornith-1.0-9B-UD-Q4_K_S.gguf"},
                {"q": "IQ4_XS", "bytes": 5168653536, "filename": "Ornith-1.0-9B-UD-IQ4_XS.gguf"},
            ]
        },
        {
            "id": "Qwen3.5-9B-MTP",
            "name": "Qwen 3.5 9B MTP",
            "hf_url": "https://huggingface.co/unsloth/Qwen3.5-9B-MTP-GGUF",
            "priority": 2,
            "is_mtp": True,
            "is_qat": False,
            "kv_bytes_4k": kv_qwen_4k,
            "quants": [
                {"q": "Q8_0", "bytes": 9527502048, "filename": "Qwen3.5-9B-MTP-UD-Q8_0.gguf"},
                {"q": "Q6_K", "bytes": 7458301152, "filename": "Qwen3.5-9B-MTP-UD-Q6_K.gguf"},
                {"q": "Q5_K_M", "bytes": 6577841376, "filename": "Qwen3.5-9B-MTP-UD-Q5_K_M.gguf"},
                {"q": "Q5_K_S", "bytes": 6361146592, "filename": "Qwen3.5-9B-MTP-UD-Q5_K_S.gguf"},
                {"q": "Q4_K_XL", "bytes": 5966095584, "filename": "Qwen3.5-9B-MTP-UD-Q4_K_XL.gguf"},
                {"q": "Q4_K_M", "bytes": 5680522464, "filename": "Qwen3.5-9B-MTP-UD-Q4_K_M.gguf"},
                {"q": "Q4_K_S", "bytes": 5394097376, "filename": "Qwen3.5-9B-MTP-UD-Q4_K_S.gguf"},
                {"q": "IQ4_XS", "bytes": 5168653536, "filename": "Qwen3.5-9B-MTP-UD-IQ4_XS.gguf"},
            ]
        },
        {
            "id": "gemma-4-12b-it",
            "name": "Gemma 12B Standard",
            "hf_url": "https://huggingface.co/unsloth/gemma-4-12b-it-GGUF",
            "priority": 3,
            "is_mtp": False,
            "is_qat": False,
            "kv_bytes_4k": kv_gemma_4k,
            "quants": [
                {"q": "Q8_0", "bytes": 12800000000, "filename": "gemma-4-12b-it-UD-Q8_0.gguf"},
                {"q": "Q6_K", "bytes": 9786022720, "filename": "gemma-4-12b-it-UD-Q6_K.gguf"},
                {"q": "Q5_K_M", "bytes": 8413576000, "filename": "gemma-4-12b-it-UD-Q5_K_M.gguf"},
                {"q": "Q4_K_M", "bytes": 7121861440, "filename": "gemma-4-12b-it-UD-Q4_K_M.gguf"},
                {"q": "Q4_K_S", "bytes": 6764526400, "filename": "gemma-4-12b-it-UD-Q4_K_S.gguf"},
                {"q": "IQ4_XS", "bytes": 6375734080, "filename": "gemma-4-12b-it-UD-IQ4_XS.gguf"},
            ]
        },
        {
            "id": "gemma-4-12b-it-qat",
            "name": "Gemma 12B QAT",
            "hf_url": "https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF",
            "priority": 4,
            "is_mtp": False,
            "is_qat": True,
            "kv_bytes_4k": kv_gemma_4k,
            "quants": [
                {"q": "Q4_K_XL", "bytes": 7366423360, "filename": "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"},
                {"q": "Q4_K_M", "bytes": 7121861440, "filename": "gemma-4-12B-it-qat-UD-Q4_K_M.gguf"},
                {"q": "Q4_0", "bytes": 6738475840, "filename": "gemma-4-12B-it-qat-UD-Q4_0.gguf"},
            ]
        },
        {
            "id": "Qwen3.5-4B-MTP",
            "name": "Qwen 3.5 4B MTP",
            "hf_url": "https://huggingface.co/unsloth/Qwen3.5-4B-MTP-GGUF",
            "priority": 5,
            "is_mtp": True,
            "is_qat": False,
            "kv_bytes_4k": kv_qwen_4k,
            "quants": [
                {"q": "Q8_0", "bytes": 4482403488, "filename": "Qwen3.5-4B-MTP-UD-Q8_0.gguf"},
                {"q": "Q6_K", "bytes": 3525956768, "filename": "Qwen3.5-4B-MTP-UD-Q6_K.gguf"},
                {"q": "Q5_K_M", "bytes": 3143656608, "filename": "Qwen3.5-4B-MTP-UD-Q5_K_M.gguf"},
                {"q": "Q4_K_M", "bytes": 2740937888, "filename": "Qwen3.5-4B-MTP-UD-Q4_K_M.gguf"},
                {"q": "Q4_K_S", "bytes": 2590430368, "filename": "Qwen3.5-4B-MTP-UD-Q4_K_S.gguf"},
            ]
        }
    ]

    optimizations = []
    for m in model_defs:
        opt = evaluate_model_quants(m, vram_free_gb, bandwidth_gbs)
        optimizations.append(opt)

    optimizations.sort(key=lambda x: (0 if x["fits"] else 1, x["priority"]))

    rec_item = optimizations[0]

    return {
        "cpu": {
            "name": cpu_info["name"],
            "cores": cpu_info["physical_cores"],
            "threads": cpu_info["logical_threads"],
            "physical_cores": cpu_info["physical_cores"],
            "logical_threads": cpu_info["logical_threads"],
            "threads_rec": cpu_info["threads_rec"]
        },
        "ram": {
            "total_gb": ram_total_gb,
            "avail_gb": ram_avail_gb
        },
        "vram": {
            "total_gb": vram_total_gb,
            "free_gb": vram_free_gb,
            "is_nvidia": is_nvidia,
            "bandwidth_gbs": bandwidth_gbs
        },
        "recommendation": rec_item["id"],
        "recommendation_name": rec_item["name"],
        "recommendation_summary": f"{rec_item['name']} [Quant: {rec_item['quant_suggested']}] — {rec_item['offload_type']} (~{rec_item['tok_s']} tok/s)",
        "optimizations": optimizations,
        "gpu": {
            "name": gpu_name,
            "vram_total_gb": vram_total_gb,
            "vram_free_gb": vram_free_gb,
            "is_nvidia": is_nvidia,
            "bandwidth_gbs": bandwidth_gbs
        },
        "tier": {
            "code": "optimized",
            "title": rec_item["name"],
            "recommended_model": rec_item["id"],
            "gpu_layers": 32,
            "context_size": 4096,
            "reason": rec_item["offload_type"]
        }
    }


def scan_local_services() -> List[Dict[str, Any]]:
    known_services = [
        {"name": "Unsloth Desktop / llama-server", "port": 8888, "endpoint": "http://localhost:8888/v1"},
        {"name": "Ollama", "port": 11434, "endpoint": "http://localhost:11434/v1"},
        {"name": "LM Studio", "port": 1234, "endpoint": "http://localhost:1234/v1"},
        {"name": "Local vLLM / TextGen", "port": 5000, "endpoint": "http://localhost:5000/v1"},
    ]

    active = []
    for svc in known_services:
        port = svc["port"]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.15)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                continue

        models = []
        detected = False
        try:
            r = requests.get(f"{svc['endpoint']}/models", timeout=0.8, allow_redirects=False)
            if r.status_code == 200:
                data = r.json()
                raw_models = data.get("data", [])
                if isinstance(raw_models, list):
                    detected = True
                models = [m.get("id", "") for m in raw_models if isinstance(m, dict) and m.get("id")]
        except Exception:
            pass

        if not detected:
            try:
                r_ollama = requests.get(f"http://localhost:{port}/api/tags", timeout=0.8, allow_redirects=False)
                if r_ollama.status_code == 200 and isinstance(r_ollama.json().get("models"), list):
                    detected = True
                    models = [m.get("name", "") for m in r_ollama.json().get("models", []) if isinstance(m, dict) and m.get("name")]
            except Exception:
                pass

        if detected:
            active.append({
                "name": svc["name"],
                "port": port,
                "endpoint": svc["endpoint"],
                "models": models,
                "detected": True
            })

    return active


def _endpoint_host_allowed(endpoint: str) -> bool:
    try:
        host = (urlparse(endpoint).hostname or "").strip("[]")
        if not host:
            return False
        if host.lower() == "localhost":
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            infos = socket.getaddrinfo(host, None)
            ips = [ipaddress.ip_address(i[4][0]) for i in infos]
            if not ips:
                return False
            return all(ip.is_loopback or ip.is_private for ip in ips)
        return ip.is_loopback or ip.is_private
    except Exception:
        return False


def test_custom_endpoint(input_str: str) -> Dict[str, Any]:
    raw = input_str.strip()
    if not raw:
        return {"ok": False, "error": "Endpoint or port cannot be empty."}

    if raw.isdigit():
        endpoint = f"http://localhost:{raw}/v1"
    elif raw.startswith(":") and raw[1:].isdigit():
        endpoint = f"http://localhost{raw}/v1"
    else:
        if not raw.startswith("http://") and not raw.startswith("https://"):
            raw = "http://" + raw
        endpoint = raw.rstrip("/")
        if not endpoint.endswith("/v1") and not endpoint.endswith("/v1/chat"):
            endpoint = endpoint + "/v1"

    if not _endpoint_host_allowed(endpoint):
        return {
            "ok": False,
            "error": f"Refused: {endpoint} is not a loopback or private-network address. "
                     "Only local AI servers may be tested."
        }

    t_start = time.time()
    latency_ms = 0
    models = []
    server_type = "OpenAI-Compatible"
    base = endpoint[:-3] if endpoint.endswith("/v1") else endpoint

    try:
        r = requests.get(f"{endpoint}/models", timeout=2.5, allow_redirects=False)
        latency_ms = round((time.time() - t_start) * 1000)
        if r.status_code == 200:
            data = r.json()
            raw_models = data.get("data", [])
            models = [m.get("id", "") for m in raw_models if isinstance(m, dict) and m.get("id")]
            return {
                "ok": True,
                "latency_ms": latency_ms,
                "endpoint": endpoint,
                "models": models,
                "server_type": server_type
            }
    except Exception:
        pass

    try:
        r_tags = requests.get(f"{base}/api/tags", timeout=2.0, allow_redirects=False)
        latency_ms = round((time.time() - t_start) * 1000)
        if r_tags.status_code == 200:
            data = r_tags.json()
            raw = data.get("models", [])
            models = [m.get("name", "") for m in raw if isinstance(m, dict) and m.get("name")]
            return {
                "ok": True,
                "latency_ms": latency_ms,
                "endpoint": endpoint,
                "models": models,
                "server_type": "Ollama"
            }
    except Exception:
        pass

    try:
        r_ping = requests.get(base, timeout=2.0, allow_redirects=False)
        latency_ms = round((time.time() - t_start) * 1000)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "endpoint": endpoint,
            "models": [],
            "server_type": "Custom Server (No model list returned)"
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"Could not connect to {endpoint}: {e}"
        }
