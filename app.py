#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TubeTranscript — 輸入 YouTube 網址，用 AssemblyAI 產生中英／中日雙語動態字幕。

流程：yt-dlp 抽音訊 → AssemblyAI 上傳＋轉錄（詞級時間戳）→ 切句 → 翻譯 → 播放器＋字幕檔。
API Key 只存在本次請求的記憶體中，不寫入磁碟。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import subtitles as subs

BASE = Path(__file__).resolve().parent
JOBS_DIR = BASE / "jobs"
STATIC_DIR = BASE / "static"
if not JOBS_DIR.exists():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)

AAI = "https://api.assemblyai.com/v2"
YTDLP = shutil.which("yt-dlp") or "yt-dlp"
PORT = int(os.environ.get("PORT", "8765"))

STEP_NAMES = ["解析影片", "下載音訊", "上傳音訊", "語音辨識", "切分字幕", "翻譯字幕"]
MAX_JOBS = 20
_job_sem = threading.Semaphore(2)

app = FastAPI(title="TubeTranscript")


# ---------------------------------------------------------------- 錯誤


class PipelineError(RuntimeError):
    pass


def _friendly_http(resp: requests.Response, what: str) -> str:
    detail = ""
    try:
        data = resp.json()
        detail = data.get("error") or data.get("message") or ""
    except Exception:
        detail = (resp.text or "")[:200]
    if resp.status_code == 401:
        return "AssemblyAI API Key 無效或已被撤銷，請重新確認。"
    if resp.status_code == 403:
        return f"AssemblyAI 拒絕存取（403）：{detail or '請確認方案權限'}"
    if resp.status_code == 402:
        return "AssemblyAI 帳戶額度不足，請先儲值或更換 Key。"
    if resp.status_code == 429:
        return "AssemblyAI 請求過於頻繁（429），請稍後再試。"
    if resp.status_code == 413:
        return "音訊檔案過大，AssemblyAI 無法接收。"
    return f"{what}失敗（HTTP {resp.status_code}）：{detail}"


# ---------------------------------------------------------------- 任務狀態

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _log(job: dict, message: str, level: str = "info") -> None:
    with _jobs_lock:
        job["logs"].append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": message})
        job["logs"] = job["logs"][-120:]
        job["message"] = message


def _set(job: dict, **kw: Any) -> None:
    with _jobs_lock:
        job.update(kw)


def _snapshot(job: dict) -> dict:
    with _jobs_lock:
        return {
            "id": job["id"],
            "status": job["status"],
            "step": job["step"],
            "steps": STEP_NAMES,
            "progress": round(job["progress"], 1),
            "message": job["message"],
            "logs": list(job["logs"][-40:]),
            "error": job["error"],
            "title": job.get("title") or "",
            "has_result": (job["workdir"] / "result.json").exists(),
        }


def _cleanup_old_jobs() -> None:
    try:
        dirs = sorted(
            [d for d in JOBS_DIR.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        for d in dirs[MAX_JOBS:]:
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------- yt-dlp


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def fetch_meta(url: str, cookies: str = "") -> dict:
    cmd = [YTDLP, "--no-playlist", "--no-warnings", "-J"]
    if cookies:
        cmd += ["--cookies-from-browser", cookies]
    cmd.append(url)
    try:
        proc = _run(cmd, timeout=150)
    except subprocess.TimeoutExpired:
        raise PipelineError("解析影片逾時，請確認網路連線或改用其他影片。")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        msg = err[-1] if err else "未知錯誤"
        if "Private video" in msg:
            raise PipelineError("這是私人影片，無法下載。")
        if "age" in msg.lower() and "confirm" in msg.lower():
            raise PipelineError("影片有年齡限制，請在進階選項選擇瀏覽器 cookies 後重試。")
        if "Sign in to confirm" in msg:
            raise PipelineError(
                "YouTube 要求登入驗證（機器人檢查）。請在「進階選項」選擇你的瀏覽器（cookies）後重試，"
                "或先執行 `yt-dlp -U` 更新版本。"
            )
        raise PipelineError(f"無法解析影片：{msg[:220]}")
    try:
        data = json.loads(proc.stdout)
    except Exception:
        raise PipelineError("影片資訊格式異常，請稍後再試。")
    if data.get("_type") == "playlist":
        entries = data.get("entries") or []
        if not entries:
            raise PipelineError("無法讀取播放清單中的影片。")
        data = entries[0]
    if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming"):
        raise PipelineError("直播中或即將開始的影片無法處理，請等直播結束後再試。")
    return {
        "id": data.get("id") or "",
        "title": data.get("title") or "未命名影片",
        "duration": int(data.get("duration") or 0),
        "uploader": data.get("uploader") or data.get("channel") or "",
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor": data.get("extractor_key") or "",
    }


def download_audio(job: dict, url: str, workdir: Path, cookies: str = "") -> Path:
    out_tpl = str(workdir / "audio.%(ext)s")
    cmd = [
        YTDLP,
        "--no-playlist",
        "--newline",
        "--no-warnings",
        "--no-part",
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "4",
        "--postprocessor-args",
        "-ar 16000 -ac 1",
        "-o",
        out_tpl,
    ]
    if cookies:
        cmd += ["--cookies-from-browser", cookies]
    cmd.append(url)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    started = time.time()
    tail: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-20:]
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            pct = float(m.group(1))
            _set(job, progress=8 + pct * 0.30)
        if time.time() - started > 2400:
            proc.kill()
            raise PipelineError("音訊下載逾時（超過 40 分鐘）。")
    proc.wait()
    if proc.returncode != 0:
        msg = next((l for l in reversed(tail) if l.startswith("ERROR")), tail[-1] if tail else "")
        if "Sign in to confirm" in msg or "bot" in msg.lower():
            raise PipelineError(
                "YouTube 要求登入驗證。請在「進階選項」選擇瀏覽器 cookies，或執行 `yt-dlp -U` 更新。"
            )
        raise PipelineError(f"音訊下載失敗：{msg[:220]}")

    files = sorted(workdir.glob("audio.*"), key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        raise PipelineError("找不到下載的音訊檔。")
    audio = files[0]
    size_mb = audio.stat().st_size / 1048576
    _log(job, f"音訊就緒：{audio.name}（{size_mb:.1f} MB）")
    return audio


# ---------------------------------------------------------------- AssemblyAI


def _aai_headers(key: str, json_body: bool = False) -> dict:
    h = {"authorization": key}
    if json_body:
        h["content-type"] = "application/json"
    return h


def aai_upload(job: dict, key: str, audio: Path) -> str:
    _log(job, "上傳音訊到 AssemblyAI…")
    _set(job, progress=41)
    try:
        with open(audio, "rb") as fh:
            resp = requests.post(
                f"{AAI}/upload",
                headers={**_aai_headers(key), "content-type": "application/octet-stream"},
                data=fh,
                timeout=1800,
            )
    except requests.RequestException as exc:
        raise PipelineError(f"上傳失敗：{exc}")
    if resp.status_code >= 400:
        raise PipelineError(_friendly_http(resp, "上傳音訊"))
    url = (resp.json() or {}).get("upload_url")
    if not url:
        raise PipelineError("AssemblyAI 未回傳上傳網址。")
    _log(job, "上傳完成")
    return url


def aai_submit(job: dict, key: str, audio_url: str, src_lang: str) -> str:
    payload: dict[str, Any] = {
        "audio_url": audio_url,
        "punctuate": True,
        "format_text": True,
        "speaker_labels": False,
    }
    if src_lang in ("en", "ja", "ko"):
        payload["language_code"] = src_lang
    else:
        payload["language_detection"] = True
    try:
        resp = requests.post(
            f"{AAI}/transcript",
            headers=_aai_headers(key, True),
            json=payload,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise PipelineError(f"建立轉錄任務失敗：{exc}")
    if resp.status_code >= 400:
        raise PipelineError(_friendly_http(resp, "建立轉錄任務"))
    tid = (resp.json() or {}).get("id")
    if not tid:
        raise PipelineError("AssemblyAI 未回傳任務 ID。")
    _log(job, f"轉錄任務已建立（{tid[:8]}…）")
    return tid


def aai_wait(job: dict, key: str, tid: str, duration_s: int) -> dict:
    est = max(45.0, duration_s * 0.28)
    started = time.time()
    delay = 3.0
    while True:
        try:
            resp = requests.get(
                f"{AAI}/transcript/{tid}", headers=_aai_headers(key), timeout=45
            )
        except requests.RequestException:
            resp = None
        if resp is not None and resp.status_code >= 400:
            raise PipelineError(_friendly_http(resp, "查詢轉錄進度"))
        if resp is not None:
            data = resp.json()
            status = data.get("status")
            if status == "completed":
                _set(job, progress=89)
                _log(job, "語音辨識完成")
                return data
            if status == "error":
                raise PipelineError(f"AssemblyAI 辨識失敗：{data.get('error') or '未知原因'}")
        elapsed = time.time() - started
        frac = min(0.98, elapsed / est)
        _set(job, progress=52 + frac * 36)
        if elapsed > 3600:
            raise PipelineError("語音辨識逾時（超過 60 分鐘）。")
        time.sleep(delay)
        delay = min(6.0, delay + 0.5)


# ---------------------------------------------------------------- 主流程


def run_pipeline(job: dict, req: dict) -> None:
    workdir = job["workdir"]
    url = req["url"]
    key = req["api_key"].strip()
    src = req.get("src_lang") or "auto"
    target = req.get("target_lang") or "zh-TW"
    cookies = (req.get("cookies") or "").strip()

    try:
        # 1 影片資訊
        _set(job, step=0, progress=2)
        _log(job, "解析影片資訊…")
        meta = fetch_meta(url, cookies)
        _set(job, title=meta["title"])
        _log(job, f"《{meta['title']}》 {meta['duration'] // 60}:{meta['duration'] % 60:02d} · {meta['uploader']}")
        if meta["duration"] and meta["duration"] > 10800:
            _log(job, "影片超過 3 小時，處理時間會較長", "warn")

        # 2 下載
        _set(job, step=1, progress=8)
        _log(job, "下載並轉換音訊（16kHz 單聲道 mp3）…")
        audio = download_audio(job, url, workdir, cookies)

        # 3 上傳
        _set(job, step=2)
        upload_url = aai_upload(job, key, audio)
        try:
            audio.unlink()
        except Exception:
            pass

        # 4 轉錄
        _set(job, step=3, progress=52)
        _log(job, "送出語音辨識…")
        tid = aai_submit(job, key, upload_url, src)
        _log(job, "等待 AssemblyAI 辨識結果（通常為音訊長度的 1/4 左右）…")
        data = aai_wait(job, key, tid, meta["duration"] or 600)

        detected = data.get("language_code") or (src if src != "auto" else "en")
        lang_code = detected.split("_")[0]
        if src in ("en", "ja", "ko") and detected and lang_code != src:
            _log(job, f"注意：偵測到的語言是 {detected}，與指定語言不同", "warn")

        # 5 切句
        _set(job, step=4, progress=90)
        _log(job, "切分字幕時間軸…")
        words = [
            {
                "t": (w.get("punctuated_word") or w.get("text") or "").strip(),
                "s": int(w.get("start") or 0),
                "e": int(w.get("end") or 0),
            }
            for w in (data.get("words") or [])
            if (w.get("text") or "").strip()
        ]
        if words:
            cues = subs.segment(words, lang_code)
        else:
            cues = subs.cues_from_text(
                data.get("text") or "",
                int((data.get("audio_duration") or meta["duration"] or 60) * 1000),
                lang_code,
            )
            _log(job, "此語言沒有詞級時間戳，改用句子均分時間", "warn")
        if not cues:
            raise PipelineError("沒有辨識到任何語音內容（可能是純音樂或無人聲）。")
        _log(job, f"切成 {len(cues)} 句字幕")

        # 6 翻譯（以整句為單位，再依時間分配給子字幕）
        _set(job, step=5, progress=92)
        _log(job, f"翻譯為 {'繁體' if target == 'zh-TW' else '簡體'}中文…")
        texts: list[str] = []
        seen: set[str] = set()
        for c in cues:
            key = c.get("sent_text") or c["orig"]
            if key and key not in seen:
                seen.add(key)
                texts.append(key)
        _log(job, f"共 {len(texts)} 個句子待翻譯")

        def on_progress(done: int, total: int) -> None:
            _set(job, progress=92 + 6.5 * (done / max(1, total)))

        trans, warnings = subs.translate_texts(texts, lang_code, target, on_progress)
        for w in warnings:
            _log(job, w, "warn")
        subs.apply_translations(cues, texts, trans)

        title_trans = ""
        try:
            tt, _ = subs.translate_texts([meta["title"]], lang_code, target)
            title_trans = tt[0] if tt else ""
        except Exception:
            pass

        # 完成
        for i, c in enumerate(cues, 1):
            c["i"] = i
            c.pop("sent_text", None)
            c.pop("sent", None)
        result = {
            "video": {
                **meta,
                "title_trans": title_trans,
                "duration": meta["duration"] or int((data.get("audio_duration") or 0)),
            },
            "lang": {"detected": detected, "target": target},
            "cues": cues,
            "stats": {
                "cues": len(cues),
                "words": len(words),
                "translated": sum(1 for c in cues if c.get("trans")),
                "duration": meta["duration"],
            },
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }
        (workdir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), "utf-8"
        )
        _set(job, step=6, progress=100, status="done", message="完成")
        _log(job, f"完成：{len(cues)} 句雙語字幕")

    except PipelineError as exc:
        _set(job, status="error", error=str(exc), message=str(exc))
        _log(job, str(exc), "error")
    except Exception as exc:  # noqa: BLE001
        _set(job, status="error", error=f"未預期錯誤：{exc}", message="處理失敗")
        _log(job, f"未預期錯誤：{exc}", "error")
    finally:
        _job_sem.release()
        _cleanup_old_jobs()


def _worker(job: dict, req: dict) -> None:
    try:
        run_pipeline(job, req)
    finally:
        pass


# ---------------------------------------------------------------- API


class JobReq(BaseModel):
    url: str
    api_key: str
    src_lang: str = "auto"
    target_lang: str = "zh-TW"
    cookies: str = ""


class KeyReq(BaseModel):
    api_key: str


@app.post("/api/validate-key")
def validate_key(req: KeyReq):
    key = (req.api_key or "").strip()
    if len(key) < 20:
        return {"ok": False, "message": "Key 長度不正確"}
    try:
        resp = requests.get(
            f"{AAI}/transcript", headers=_aai_headers(key), params={"limit": 1}, timeout=25
        )
    except requests.RequestException as exc:
        return {"ok": False, "message": f"無法連線 AssemblyAI：{exc}"}
    if resp.status_code == 200:
        return {"ok": True, "message": "API Key 有效"}
    return {"ok": False, "message": _friendly_http(resp, "驗證 Key")}


@app.post("/api/jobs")
def create_job(req: JobReq):
    url = (req.url or "").strip()
    key = (req.api_key or "").strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "請輸入完整的影片網址（https://…）")
    if len(key) < 20:
        raise HTTPException(400, "請輸入 AssemblyAI API Key")
    if not _job_sem.acquire(blocking=False):
        raise HTTPException(429, "目前有太多任務在處理中，請稍後再試")

    job_id = uuid.uuid4().hex[:12]
    workdir = _job_path(job_id)
    if not workdir.exists():
        workdir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id,
        "status": "running",
        "step": 0,
        "progress": 0.0,
        "message": "準備中…",
        "logs": [],
        "error": None,
        "title": "",
        "workdir": workdir,
        "created": time.time(),
    }
    with _jobs_lock:
        _jobs[job_id] = job
    threading.Thread(target=_worker, args=(job, req.model_dump()), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if job:
        return _snapshot(job)
    meta = _job_path(job_id) / "result.json"
    if meta.exists():
        data = json.loads(meta.read_text("utf-8"))
        return {
            "id": job_id,
            "status": "done",
            "step": 6,
            "steps": STEP_NAMES,
            "progress": 100,
            "message": "完成",
            "logs": [],
            "error": None,
            "title": data["video"].get("title", ""),
            "has_result": True,
        }
    raise HTTPException(404, "找不到此任務")


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str):
    path = _job_path(job_id) / "result.json"
    if not path.exists():
        raise HTTPException(404, "結果尚未完成")
    return JSONResponse(json.loads(path.read_text("utf-8")))


@app.get("/api/jobs")
def list_jobs():
    out = []
    for d in sorted(
        [p for p in JOBS_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:MAX_JOBS]:
        f = d / "result.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        out.append(
            {
                "id": d.name,
                "title": data["video"].get("title", ""),
                "title_trans": data["video"].get("title_trans", ""),
                "duration": data["video"].get("duration", 0),
                "cues": data["stats"].get("cues", 0),
                "created": data.get("created", ""),
                "thumb": data["video"].get("thumbnail", ""),
                "lang": data["lang"].get("detected", ""),
            }
        )
    return out


@app.get("/api/jobs/{job_id}/export")
def export(job_id: str, fmt: str = "srt", mode: str = "both"):
    path = _job_path(job_id) / "result.json"
    if not path.exists():
        raise HTTPException(404, "結果尚未完成")
    if fmt not in subs.EXPORTERS:
        raise HTTPException(400, "不支援的格式")
    if mode not in ("both", "orig", "trans"):
        raise HTTPException(400, "不支援的模式")
    data = json.loads(path.read_text("utf-8"))
    fn, ext, media = subs.EXPORTERS[fmt]
    content = fn(data["cues"], mode)
    safe = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", data["video"].get("title", "subtitle")).strip()[:70]
    suffix = {"both": "雙語", "orig": "原文", "trans": "譯文"}[mode]
    filename = f"{safe or 'subtitle'}_{suffix}.{ext}"
    tmp = _job_path(job_id) / f"export_{fmt}_{mode}.{ext}"
    tmp.write_text(content, "utf-8")
    return FileResponse(
        tmp,
        media_type=media,
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{requests.utils.quote(filename)}"},
    )


@app.get("/api/health")
def health():
    return {"ok": True, "ytdlp": bool(shutil.which("yt-dlp")), "ffmpeg": bool(shutil.which("ffmpeg"))}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    print(f"\n  TubeTranscript  →  http://127.0.0.1:{PORT}\n")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
