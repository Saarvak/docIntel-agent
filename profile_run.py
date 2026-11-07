"""Profile a DocIntel pipeline run and write a report you can inspect afterwards.

Intended for a GCP VM running only the agent. MinerU is the hosted API at mineru.net.

    python profile_run.py --require-mineru
    python profile_run.py samples/invoice-sliced.pdf --repeat 2

When it finishes, open the newest folder under profiles/. REPORT.md is the
human-readable summary: instance type, RAM/CPU peaks, per-document timings.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docintel.config import PROJECT_ROOT, get_settings
from docintel.mineru import MinerUClient, MinerUError
from docintel.pipeline import analyze_document

PROFILES_DIR = PROJECT_ROOT / "profiles"
MINERU_NAME_HINTS = ("mineru-api", "mineru")
GCP_METADATA = "http://metadata.google.internal/computeMetadata/v1/"


# ───────────────────────────── helpers ─────────────────────────────


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run(cmd: list[str], timeout: float = 3.0) -> str | None:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip() or None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _mb(n_bytes: float) -> float:
    return round(n_bytes / (1024 * 1024), 1)


# ───────────────────────────── GCP / host ─────────────────────────────


def gcp_metadata() -> dict[str, str] | None:
    def get(path: str) -> str | None:
        req = urllib.request.Request(
            GCP_METADATA + path,
            headers={"Metadata-Flavor": "Google"},
        )
        try:
            with urllib.request.urlopen(req, timeout=1.5) as response:
                return response.read().decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    machine = get("instance/machine-type")
    if not machine:
        return None
    zone = get("instance/zone") or ""
    return {
        "name": get("instance/name") or "",
        "id": get("instance/id") or "",
        "machine_type": machine.rsplit("/", 1)[-1],
        "zone": zone.rsplit("/", 1)[-1],
        "cpu_platform": get("instance/cpu-platform") or "",
        "preemptible": get("instance/scheduling/preemptible") or "",
    }


def nvidia_gpus() -> list[dict[str, Any]]:
    raw = _run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,utilization.memory,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    if not raw:
        return []
    gpus = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_total_mb": float(parts[2]),
                    "memory_used_mb": float(parts[3]),
                    "util_gpu": float(parts[4]),
                    "util_mem": float(parts[5]),
                    "temp_c": float(parts[6]),
                }
            )
        except ValueError:
            continue
    return gpus


def linux_mem() -> dict[str, float] | None:
    text = _read(Path("/proc/meminfo"))
    if not text:
        return None
    kb: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        token = rest.strip().split()[0]
        try:
            kb[key] = int(token)
        except ValueError:
            continue
    total = kb.get("MemTotal", 0)
    available = kb.get("MemAvailable", kb.get("MemFree", 0))
    swap_total = kb.get("SwapTotal", 0)
    swap_free = kb.get("SwapFree", 0)
    return {
        "mem_total_mb": round(total / 1024, 1),
        "mem_available_mb": round(available / 1024, 1),
        "mem_used_mb": round((total - available) / 1024, 1),
        "swap_used_mb": round((swap_total - swap_free) / 1024, 1),
    }


def windows_mem() -> dict[str, float] | None:
    if os.name != "nt":
        return None
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "mem_total_mb": _mb(status.ullTotalPhys),
        "mem_available_mb": _mb(status.ullAvailPhys),
        "mem_used_mb": _mb(status.ullTotalPhys - status.ullAvailPhys),
        "swap_used_mb": _mb(max(0, status.ullTotalPageFile - status.ullAvailPageFile - status.ullTotalPhys)),
    }


def process_rss_mb(pid: int) -> float | None:
    if Path("/proc").is_dir():
        text = _read(Path(f"/proc/{pid}/status"))
        if not text:
            return None
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                try:
                    return round(int(line.split()[1]) / 1024, 1)
                except (IndexError, ValueError):
                    return None
        return None
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    handle = kernel32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        return None
    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    kernel32.CloseHandle(handle)
    if not ok:
        return None
    return _mb(counters.WorkingSetSize)


def pids_matching(hints: tuple[str, ...]) -> list[int]:
    if not Path("/proc").is_dir():
        return []
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        blob = ((_read(entry / "comm") or "") + " " + (_read(entry / "cmdline") or "").replace("\x00", " ")).lower()
        if any(hint in blob for hint in hints):
            found.append(int(entry.name))
    return found


def loadavg() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def cpu_count() -> int:
    return os.cpu_count() or 0


def disk_free_gb(path: Path) -> float | None:
    try:
        usage = os.statvfs(path)
    except (AttributeError, OSError):
        try:
            import shutil

            total, used, free = shutil.disk_usage(path)
            return round(free / (1024**3), 1)
        except OSError:
            return None
    return round((usage.f_bavail * usage.f_frsize) / (1024**3), 1)


def host_snapshot() -> dict[str, Any]:
    mem = linux_mem() or windows_mem() or {}
    gcp = gcp_metadata()
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_count": cpu_count(),
        "loadavg": loadavg(),
        "disk_free_gb": disk_free_gb(PROJECT_ROOT),
        "memory": mem,
        "gpus": nvidia_gpus(),
        "gcp": gcp,
        "on_gcp": gcp is not None,
    }


# ───────────────────────────── sampler ─────────────────────────────


class ResourceSampler:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.pid = os.getpid()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="resource-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 2)

    def _loop(self) -> None:
        while True:
            self.samples.append(self._snap())
            if self._stop.wait(self.interval):
                self.samples.append(self._snap())
                return

    def _snap(self) -> dict[str, Any]:
        mem = linux_mem() or windows_mem() or {}
        mineru_pids = pids_matching(MINERU_NAME_HINTS)
        mineru_rss = sum(filter(None, (process_rss_mb(pid) for pid in mineru_pids))) or None
        gpus = nvidia_gpus()
        row: dict[str, Any] = {
            "t": round(time.time(), 3),
            "iso": datetime.now(timezone.utc).isoformat(),
            "agent_rss_mb": process_rss_mb(self.pid),
            "mineru_rss_mb": round(mineru_rss, 1) if mineru_rss else None,
            "mineru_pids": mineru_pids,
            "mem_used_mb": mem.get("mem_used_mb"),
            "mem_available_mb": mem.get("mem_available_mb"),
            "mem_total_mb": mem.get("mem_total_mb"),
            "swap_used_mb": mem.get("swap_used_mb"),
            "loadavg_1": (loadavg() or [None])[0],
        }
        if gpus:
            row["gpu_util"] = max(g["util_gpu"] for g in gpus)
            row["gpu_mem_used_mb"] = sum(g["memory_used_mb"] for g in gpus)
            row["gpu_temp_c"] = max(g["temp_c"] for g in gpus)
        return row


def _peak(samples: list[dict[str, Any]], key: str) -> float | None:
    values = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
    return round(max(values), 1) if values else None


def _mean(samples: list[dict[str, Any]], key: str) -> float | None:
    values = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
    return round(sum(values) / len(values), 1) if values else None


# ───────────────────────────── report ─────────────────────────────


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    if ms >= 10_000:
        return f" {ms / 1000:.1f}s"
    return f"{ms} ms"


def write_report(
    out_dir: Path,
    host: dict[str, Any],
    settings_info: dict[str, Any],
    documents: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    idle: dict[str, Any] | None,
    errors: list[str],
) -> str:
    gcp = host.get("gcp") or {}
    mem = host.get("memory") or {}
    gpus = host.get("gpus") or []
    peak_used = _peak(samples, "mem_used_mb")
    peak_agent = _peak(samples, "agent_rss_mb")
    peak_mineru = _peak(samples, "mineru_rss_mb")
    peak_gpu = _peak(samples, "gpu_util")
    peak_gpu_mem = _peak(samples, "gpu_mem_used_mb")
    peak_load = _peak(samples, "loadavg_1")
    total_ms = sum(d.get("duration_ms") or 0 for d in documents if d.get("ok"))
    parse_ms = sum((d.get("stages_ms") or {}).get("parse") or 0 for d in documents if d.get("ok"))

    lines = [
        "# DocIntel profile",
        "",
        f"- **Finished:** {datetime.now(timezone.utc).isoformat()}",
        f"- **Host:** {host.get('hostname')} (`{host.get('platform')}`)",
        f"- **Python:** {host.get('python')}  |  **vCPU:** {host.get('cpu_count')}  |  **disk free:** {host.get('disk_free_gb')} GB",
    ]
    if gcp:
        lines.append(
            f"- **GCP:** `{gcp.get('machine_type')}`  |  zone `{gcp.get('zone')}`  |  "
            f"cpu `{gcp.get('cpu_platform')}`  |  instance `{gcp.get('name')}`"
        )
    else:
        lines.append("- **GCP:** not detected (metadata server unreachable — this is fine on a laptop)")

    ram_line = f"- **RAM:** {mem.get('mem_total_mb', '?')} MB total"
    if peak_used is not None:
        ram_line += f", peak used **{peak_used} MB**"
        total = mem.get("mem_total_mb")
        if total:
            ram_line += f" ({round(100 * peak_used / total)}%)"
    if idle and idle.get("mem_used_mb") is not None:
        ram_line += f", idle {idle['mem_used_mb']} MB"
    lines += [ram_line, f"- **Process RSS peaks:** agent {peak_agent} MB, MinerU {peak_mineru} MB"]
    if gpus:
        names = ", ".join(f"{g['name']} ({g['memory_total_mb']:.0f} MB)" for g in gpus)
        lines.append(f"- **GPU:** {names}  |  peak util **{peak_gpu}%**  |  peak VRAM **{peak_gpu_mem} MB**")
    else:
        lines.append("- **GPU:** none (`nvidia-smi` not found)")
    if peak_load is not None:
        lines.append(f"- **Load average (1m) peak:** {peak_load} across {host.get('cpu_count')} CPUs")

    lines += [
        "",
        "## Pipeline",
        f"- MinerU: `{settings_info['mineru_url']}` — {settings_info['mineru_status']}",
        f"- MinerU backend: `{settings_info['mineru_backend']}`",
        f"- Gemini: {settings_info['gemini']}",
        f"- Parser actually used: {settings_info['parsers_used']}",
        "",
        "## Documents",
        "",
        "| phase | file | type | parser | total | parse | classify | extract | llm | validate | result |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for doc in documents:
        stages = doc.get("stages_ms") or {}
        if not doc.get("ok"):
            lines.append(
                f"| {doc.get('phase', '-')} | {doc['filename']} | - | error | - | - | - | - | - | - | {doc.get('error', '')} |"
            )
            continue
        lines.append(
            "| {phase} | {filename} | {doc_type} | {parser} | {total} | {parse} | {classify} | {extract} | {llm} | {validate} | {status} |".format(
                phase=doc.get("phase", "run"),
                filename=doc["filename"],
                doc_type=doc.get("document_type", "-"),
                parser=doc.get("parser", "-"),
                total=_fmt_ms(doc.get("duration_ms")),
                parse=_fmt_ms(stages.get("parse")),
                classify=_fmt_ms(stages.get("classify")),
                extract=_fmt_ms(stages.get("extract")),
                llm=_fmt_ms(stages.get("llm")),
                validate=_fmt_ms(stages.get("validate")),
                status=doc.get("status", "-"),
            )
        )

    ok_docs = [d for d in documents if d.get("ok")]
    lines += [
        "",
        f"- Documents ok / failed: {len(ok_docs)} / {len(documents) - len(ok_docs)}",
        f"- Wall time of pipeline work: {_fmt_ms(total_ms)}",
        f"- Of which MinerU parse: {_fmt_ms(parse_ms)}"
        + (f" ({round(100 * parse_ms / total_ms)}%)" if total_ms else ""),
    ]
    if errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in errors]

    lines += [
        "",
        "## How to read this on GCP",
        "",
        "- **parse** is wait time on the MinerU cloud API, not local OCR. A GPU on this VM will not change it.",
        "- **Peak RAM** sizes the VM for the agent only. With hosted MinerU, a small instance is enough.",
        "- **MinerU RSS** should be empty: parsing does not run on this machine.",
        "- Re-run with `--require-mineru` so every file hits mineru.net.",
        "",
        "Raw files in this folder: `summary.json`, `documents.json`, `timeseries.csv`, `host.json`.",
        "",
    ]
    text = "\n".join(lines)
    (out_dir / "REPORT.md").write_text(text, encoding="utf-8")
    return text


# ───────────────────────────── run ─────────────────────────────


def collect_paths(args: argparse.Namespace, settings) -> list[Path]:
    if args.paths:
        paths = []
        for raw in args.paths:
            path = Path(raw)
            if path.is_dir():
                paths.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".md"}))
            else:
                paths.append(path)
        return paths
    samples = settings.samples_dir
    pdfs = sorted(samples.glob("*.pdf")) if samples.exists() else []
    if not pdfs:
        raise SystemExit(f"No PDFs in {samples} and no paths given.")
    return pdfs


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the DocIntel pipeline and write a GCP-oriented profile report.")
    parser.add_argument("paths", nargs="*", help="files or directories (default: samples/*.pdf)")
    parser.add_argument("--out", type=Path, help="output folder (default: profiles/<timestamp>)")
    parser.add_argument("--interval", type=float, default=0.5, help="resource sample interval in seconds")
    parser.add_argument("--repeat", type=int, default=1, help="times to process each file after warmup")
    parser.add_argument("--no-warmup", action="store_true", help="skip the first extra pass (model-load isolation)")
    parser.add_argument("--require-mineru", action="store_true", help="fail if the MinerU cloud API is not reachable")
    args = parser.parse_args()

    settings = get_settings()
    paths = collect_paths(args, settings)
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit("File not found: " + ", ".join(str(p) for p in missing))

    mineru = MinerUClient(settings)
    mineru_ok = mineru.health()
    if not mineru_ok:
        print(f"MinerU API is not reachable at {settings.mineru_api_url}. Check network / MINERU_API_TOKEN.", file=sys.stderr)
        return 2

    out_dir = args.out or (PROFILES_DIR / _utc_stamp())
    out_dir.mkdir(parents=True, exist_ok=True)

    host = host_snapshot()
    (out_dir / "host.json").write_text(json.dumps(host, indent=2), encoding="utf-8")

    print(f"Writing profile to {out_dir}")
    if host.get("gcp"):
        gcp = host["gcp"]
        print(f"GCP {gcp.get('machine_type')} in {gcp.get('zone')} ({gcp.get('name')})")
    else:
        print("Not on GCP (or metadata server blocked). Still collecting local CPU/RAM/GPU.")
    mineru_mode = "v4 token" if settings.mineru_api_token else "public agent API"
    print(
        f"MinerU API: {settings.mineru_api_url} ({mineru_mode}) — "
        f"{'reachable' if mineru_ok else 'NOT reachable'}"
    )
    print(f"Files: {', '.join(p.name for p in paths)}")

    sampler = ResourceSampler(args.interval)
    sampler.start()
    time.sleep(min(1.5, max(0.4, args.interval * 2)))
    idle = sampler.samples[-1] if sampler.samples else None

    documents: list[dict[str, Any]] = []
    errors: list[str] = []
    parsers_used: set[str] = set()

    def process_file(path: Path) -> dict[str, Any]:
        return analyze_document(path.read_bytes(), path.name, settings=settings, mineru=mineru)

    work: list[tuple[str, Path]] = []
    if not args.no_warmup:
        work.append(("warmup", paths[0]))
    for _ in range(max(1, args.repeat)):
        for path in paths:
            work.append(("run", path))

    try:
        for phase, path in work:
            print(f"  [{phase}] {path.name} ...", flush=True)
            t0 = time.perf_counter()
            try:
                result = process_file(path)
            except MinerUError as exc:
                elapsed = round((time.perf_counter() - t0) * 1000)
                msg = f"{path.name}: {exc}"
                errors.append(msg)
                documents.append({"ok": False, "phase": phase, "filename": path.name, "error": str(exc), "duration_ms": elapsed})
                print(f"    FAILED ({elapsed} ms): {exc}", flush=True)
                continue
            processing = result.get("processing") or {}
            parsers_used.add(str(processing.get("parser")))
            row = {
                "ok": True,
                "phase": phase,
                "filename": path.name,
                "document_id": result.get("document_id"),
                "document_type": result.get("document_type"),
                "status": (result.get("validation") or {}).get("status"),
                "parser": processing.get("parser"),
                "llm_filled": processing.get("llm_filled"),
                "duration_ms": processing.get("duration_ms"),
                "stages_ms": processing.get("stages_ms"),
                "needs_human_review": result.get("needs_human_review"),
            }
            documents.append(row)
            print(
                f"    {row['document_type']} / {row['parser']} / {row['status']} / {_fmt_ms(row['duration_ms'])}",
                flush=True,
            )
    finally:
        sampler.stop()

    settings_info = {
        "mineru_url": settings.mineru_api_url,
        "mineru_backend": settings.mineru_backend,
        "mineru_status": "reachable" if mineru_ok else "unreachable",
        "gemini": "enabled" if settings.gemini_api_keys else "off",
        "parsers_used": ", ".join(sorted(parsers_used)) or "none",
        "require_mineru": True,
        "repeat": args.repeat,
    }

    (out_dir / "documents.json").write_text(json.dumps(documents, indent=2), encoding="utf-8")
    if sampler.samples:
        keys = [
            "iso",
            "agent_rss_mb",
            "mineru_rss_mb",
            "mem_used_mb",
            "mem_available_mb",
            "mem_total_mb",
            "swap_used_mb",
            "loadavg_1",
            "gpu_util",
            "gpu_mem_used_mb",
            "gpu_temp_c",
        ]
        with (out_dir / "timeseries.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for row in sampler.samples:
                writer.writerow(row)

    summary = {
        "out_dir": str(out_dir),
        "host": host,
        "settings": settings_info,
        "idle": idle,
        "peaks": {
            "mem_used_mb": _peak(sampler.samples, "mem_used_mb"),
            "agent_rss_mb": _peak(sampler.samples, "agent_rss_mb"),
            "mineru_rss_mb": _peak(sampler.samples, "mineru_rss_mb"),
            "gpu_util": _peak(sampler.samples, "gpu_util"),
            "gpu_mem_used_mb": _peak(sampler.samples, "gpu_mem_used_mb"),
            "loadavg_1": _peak(sampler.samples, "loadavg_1"),
        },
        "averages": {
            "mem_used_mb": _mean(sampler.samples, "mem_used_mb"),
            "gpu_util": _mean(sampler.samples, "gpu_util"),
        },
        "documents": documents,
        "errors": errors,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    report = write_report(out_dir, host, settings_info, documents, sampler.samples, idle, errors)
    print()
    print(report)
    print(f"Saved: {out_dir / 'REPORT.md'}")
    return 1 if errors and not any(d.get("ok") for d in documents) else 0


if __name__ == "__main__":
    sys.exit(main())
