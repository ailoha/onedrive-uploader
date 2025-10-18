import os
import time
import json
import requests
import hashlib
from pathlib import Path
from auth import acquire_token_silent_for_account, acquire_token_interactive
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import random
import threading

# --- Adaptive chunk sizing constants (OneDrive requires 320KiB multiples; practical cap 60MiB) ---
_CHUNK_ALIGN = 320 * 1024  # 320 KiB
_MIN_CHUNK = 2 * 1024 * 1024  # 2 MiB
_MAX_CHUNK = 32 * 1024 * 1024  # 32 MiB (safe, below Graph 60 MiB recommendation)
_TARGET_CHUNK_SECONDS = 8.0
_ADJUST_EVERY_N_CHUNKS = 2
_ADJUST_SMOOTHING = 0.3  # EMA for speed smoothing

def _round_to_320k(n_bytes: int) -> int:
    """Round up to the nearest 320KiB multiple, except allow smaller for the final fragment."""
    if n_bytes & (_CHUNK_ALIGN - 1) == 0:
        return max(n_bytes, _CHUNK_ALIGN)
    return ((n_bytes + _CHUNK_ALIGN - 1) // _CHUNK_ALIGN) * _CHUNK_ALIGN

def _initial_adaptive_chunk_size(file_size: float) -> int:
    """
    Heuristic:
    - small (<128MiB): 8MiB
    - medium (<512MiB): 12MiB
    - large (<2GiB): 16MiB
    - very large: 24MiB
    Then align to 320KiB, clamp to [_MIN_CHUNK, _MAX_CHUNK].
    """
    if file_size < 128 * 1024 * 1024:
        cs = 8 * 1024 * 1024
    elif file_size < 512 * 1024 * 1024:
        cs = 12 * 1024 * 1024
    elif file_size < 2 * 1024 * 1024 * 1024:
        cs = 16 * 1024 * 1024
    else:
        cs = 24 * 1024 * 1024
    cs = _round_to_320k(cs)
    return int(min(max(cs, _MIN_CHUNK), _MAX_CHUNK))

# --- Session helpers for resumable upload ---
# 使用系统支持的用户写入路径，避免 .app 打包后无法写入问题
SESS_DIR = Path.home() / "Library/Application Support/OneDriveUploader/sessions"
SESS_DIR.mkdir(parents=True, exist_ok=True)

# --- Batch-level resume state for multi-file uploads ---
STATE_FILE = SESS_DIR / "batch_state.json"

def _load_batch_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}

def _save_batch_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass

# Thread-local shared HTTP session for robust connection reuse
_tls = threading.local()

def _get_session():
    """Return a per-thread shared requests.Session with retry + connection pooling.
    This avoids rebuilding adapters and TCP pools for every chunk while keeping
    thread-safety. Reuses connections for higher throughput and lower CPU.
    """
    s = getattr(_tls, "session", None)
    if s is not None:
        return s
    s = requests.Session()
    retry = Retry(
        total=5,
        connect=5, read=5, status=5,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["HEAD", "GET", "PUT", "POST"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32, pool_block=True)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"Connection": "keep-alive"})
    _tls.session = s
    return s

def _reset_session():
    """Close and clear the current thread's session so a fresh one is created next time."""
    s = getattr(_tls, "session", None)
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
        _tls.session = None

def clear_old_sessions():
    """清空 .sessions 目录，防止无效续传残留。"""
    for f in SESS_DIR.glob("*.json"):
        try:
            f.unlink()
        except Exception:
            pass

def _session_key(local_path: str, remote_path: str, file_size: float) -> str:
    st = os.stat(local_path)
    # include mtime to invalidate session when file changes
    base = f"{os.path.abspath(local_path)}|{remote_path}|{int(file_size)}|{int(st.st_mtime)}"
    return hashlib.sha256(base.encode('utf-8')).hexdigest()

def _session_path(key: str) -> Path:
    return SESS_DIR / f"{key}.json"

def _save_session(key: str, data: dict):
    try:
        _session_path(key).write_text(json.dumps(data), encoding='utf-8')
    except Exception:
        pass

def _load_session(key: str):
    p = _session_path(key)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None
    return None

def _delete_session(key: str):
    p = _session_path(key)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass

def _parse_next_start(resp_json: dict) -> int:
    """
    Parse OneDrive upload session's nextExpectedRanges and return the smallest start offset.
    Examples: ["0-"], ["10485760-"], ["10485760-20971519","25165824-"]
    """
    ranges = resp_json.get('nextExpectedRanges') or []
    min_start = None
    for rng in ranges:
        try:
            # accept formats like "start-" or "start-end"
            start_str = str(rng).split('-', 1)[0].strip()
            s = int(start_str)
            if min_start is None or s < min_start:
                min_start = s
        except Exception:
            continue
    return int(min_start or 0)

def _parse_uploaded_from_headers(range_header: str | None) -> int | None:
    """
    Parse server-reported uploaded position from Range/Content-Range headers.
    Examples:
      Range: "bytes=0-10485759" -> returns 10485760
      Content-Range: "bytes 0-10485759/52428800" -> returns 10485760
    """
    if not range_header:
        return None
    try:
        val = range_header.strip()
        # remove prefix like "bytes=" or "bytes "
        if '=' in val:
            val = val.split('=', 1)[1]
        elif ' ' in val:
            val = val.split(' ', 1)[1]
        # now "start-end" or "start-end/total"
        parts = val.split('/', 1)[0]  # drop "/total" if present
        start_end = parts.split('-', 1)
        if len(start_end) != 2:
            return None
        end = int(start_end[1])
        return end + 1
    except Exception:
        return None

def upload_items(file_list, base_dir="", remote_base="", account_home_id=None, progress_cb=None, log_cb=None, should_stop=None):
    # 保留 base_dir 的最后一级目录作为远程根
    if base_dir:
        base_dir = os.path.abspath(base_dir)
        top_level_name = os.path.basename(base_dir.rstrip(os.sep))
    else:
        top_level_name = ""

    # 收集文件（排除隐藏文件）
    abs_file_list = []
    total_bytes = 0
    for file_path in file_list:
        abs_path = os.path.abspath(file_path)
        name = os.path.basename(abs_path)
        if name.startswith('.') or name.startswith('._') or name == 'Icon\r':
            continue
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0
        abs_file_list.append((abs_path, size))
        total_bytes += size

    if log_cb:
        log_cb(f"Found {len(abs_file_list)} files, total {total_bytes / (1024*1024*1024):.2f} GB")

    # --- Batch resume state ---
    state = _load_batch_state()
    file_states = state.get("files", {})

    uploaded_bytes = 0
    # Pre-calculate already uploaded bytes for skipped files
    for abs_path, size in abs_file_list:
        if file_states.get(abs_path) == "done":
            uploaded_bytes += size

    start_time = time.time()

    for abs_path, size in abs_file_list:
        if callable(should_stop) and should_stop():
            if log_cb:
                log_cb("Stop requested by user. Halting before next file.")
            break

        # Skip already uploaded files (batch-level resume)
        if file_states.get(abs_path) == "done":
            if log_cb:
                log_cb(f"Skipping already uploaded file: {abs_path}")
            continue

        rel = os.path.relpath(abs_path, base_dir) if base_dir else os.path.basename(abs_path)
        rel = os.path.join(top_level_name, rel)
        rp = _normalize_remote_path(remote_base, rel)

        if log_cb:
            log_cb(f"Uploading {rel} ({size / (1024*1024):.2f} MB)")

        def pf(current, _ignored_total, speed=None, eta=None):
            """
            统一以全局 total_bytes 为准，避免 UI 被单文件大小误导。
            """
            nonlocal uploaded_bytes
            total_uploaded = uploaded_bytes + current
            overall_eta = None
            if speed and speed > 0:
                remaining = total_bytes - total_uploaded
                overall_eta = remaining / speed
            if progress_cb:
                try:
                    progress_cb(total_uploaded, total_bytes, speed, overall_eta)
                except TypeError:
                    progress_cb(total_uploaded, total_bytes)

        actual_size = upload_file(abs_path, rp, account_home_id=account_home_id, progress_fn=pf, log_fn=log_cb, should_stop=should_stop)

        # --- Batch state update after file upload ---
        if actual_size >= size:
            file_states[abs_path] = "done"
        else:
            file_states[abs_path] = "incomplete"
        _save_batch_state({"base_dir": base_dir, "files": file_states})

        uploaded_bytes += actual_size

        if actual_size < size:
            # Mark as incomplete before breaking
            file_states[abs_path] = "incomplete"
            _save_batch_state({"base_dir": base_dir, "files": file_states})
            if log_cb:
                log_cb("Stopped during file upload. Session saved for resume.")
            break

        if progress_cb:
            try:
                progress_cb(uploaded_bytes, total_bytes, 0, 0)
            except TypeError:
                progress_cb(uploaded_bytes, total_bytes)

    # If all files uploaded successfully, remove batch state file
    all_done = all(file_states.get(abs_path) == "done" for abs_path, _ in abs_file_list)
    if all_done:
        try:
            STATE_FILE.unlink()
        except Exception:
            pass

    if log_cb:
        duration = time.time() - start_time
        log_cb(f"All files uploaded ({total_bytes / (1024*1024*1024):.2f} GB in {duration:.1f}s)")
    return True

def upload_file(local_path, remote_path, account_home_id=None, progress_fn=None, log_fn=None, should_stop=None, adaptive=True):
    """
    使用 OneDrive 分段上传会话，支持断点续传与重试。启用智能自适应分片算法：
    初始分片根据文件大小自动确定，并在上传过程中动态调整，范围 2–32MiB。
    分片满足 320KiB 对齐规则，目标每片传输时长≈8秒。
    会在 ./.sessions 目录保存会话信息，异常中断后可继续上传。
    """
    token, _ = acquire_token_silent_for_account(account_home_id)
    if not token:
        token, _ = acquire_token_interactive()

    file_size = float(os.path.getsize(local_path))
    # Always use adaptive initial chunk size based on file size
    chunk_size = _initial_adaptive_chunk_size(file_size)

    key = _session_key(local_path, remote_path, file_size)
    sess = _load_session(key) or {}
    upload_url = sess.get('uploadUrl')

    headers_json = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Create session if none
    if not upload_url:
        session_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/createUploadSession"
        session = _get_session()
        r = session.post(session_url, headers=headers_json, json={}, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create upload session: {r.status_code} {r.text}")
        resp = r.json()
        upload_url = resp['uploadUrl']
        sess = {"uploadUrl": upload_url, "remote_path": remote_path}
        _save_session(key, sess)
        if log_fn:
            log_fn("Upload session created")

    # Try to query current progress to resume
    uploaded_bytes = 0
    try:
        session = _get_session()
        q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if q.status_code in (200, 201, 202):
            # Prefer headers if present. Fallback to nextExpectedRanges JSON.
            hdr_pos = _parse_uploaded_from_headers(q.headers.get("Range") or q.headers.get("Content-Range"))
            if hdr_pos is not None:
                uploaded_bytes = int(hdr_pos)
            else:
                uploaded_bytes = int(_parse_next_start(q.json()))
    except Exception:
        pass

    start_time = time.time()
    last_save_time = start_time
    chunks_since_save = 0
    # Adaptive control variables
    chunks_since_adjust = 0
    ema_speed = None  # bytes/sec
    # Emit initial progress if resuming
    if progress_fn and uploaded_bytes > 0:
        try:
            progress_fn(float(uploaded_bytes), float(file_size), 0.0, max(0.0, (file_size - uploaded_bytes)))
        except TypeError:
            progress_fn(float(uploaded_bytes), float(file_size))

    max_retries = 5
    backoff = 1.0

    with open(local_path, 'rb') as f:
        session = _get_session()
        # seek to resume point
        if uploaded_bytes > 0:
            f.seek(int(uploaded_bytes))
        while uploaded_bytes < file_size:
            # Check for user-requested stop before reading next chunk
            if callable(should_stop) and should_stop():
                _save_session(key, {"uploadUrl": upload_url, "remote_path": remote_path, "uploaded": int(uploaded_bytes)})
                if log_fn:
                    log_fn("Stop requested. Current session saved for resume.")
                return uploaded_bytes

            chunk = f.read(chunk_size)
            if not chunk:
                break
            start = int(uploaded_bytes)
            end = start + len(chunk) - 1
            put_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {int(start)}-{int(end)}/{int(file_size)}",
            }
            try:
                t0 = time.time()
                resp = session.put(upload_url, headers=put_headers, data=chunk, timeout=(10, 120))
            except Exception as ex:
                if log_fn:
                    log_fn(f"Network error, resetting session and retrying: {ex}")
                _reset_session()
                session = _get_session()
                # Exponential backoff with jitter
                time.sleep(min(backoff * (0.5 + random.random()), 30))
                backoff = min(backoff * 2, 30)
                chunks_since_adjust = 0
                # re-query session position
                try:
                    q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                    if q.status_code in (200, 201, 202):
                        hdr_pos = _parse_uploaded_from_headers(q.headers.get("Range") or q.headers.get("Content-Range"))
                        uploaded_bytes = int(hdr_pos if hdr_pos is not None else _parse_next_start(q.json()))
                        f.seek(uploaded_bytes)
                        continue
                except Exception:
                    pass
                continue

            if resp.status_code in (409, 416):
                if log_fn:
                    log_fn("Range conflict or resource modified. Realigning to server position.")
                try:
                    q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                    if q.status_code in (200, 201, 202):
                        hdr_pos = _parse_uploaded_from_headers(q.headers.get("Range") or q.headers.get("Content-Range"))
                        uploaded_bytes = int(hdr_pos if hdr_pos is not None else _parse_next_start(q.json()))
                        f.seek(uploaded_bytes)
                        backoff = 1.0
                        continue
                except Exception:
                    pass

            if resp.status_code in (401, 403) and account_home_id:
                if log_fn:
                    log_fn("Auth expired. Refreshing token.")
                new_token, _ = acquire_token_silent_for_account(account_home_id)
                if new_token:
                    token = new_token
                    try:
                        q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                        if q.status_code in (200, 201, 202):
                            hdr_pos = _parse_uploaded_from_headers(q.headers.get("Range") or q.headers.get("Content-Range"))
                            uploaded_bytes = int(hdr_pos if hdr_pos is not None else _parse_next_start(q.json()))
                            f.seek(uploaded_bytes)
                            backoff = 1.0
                            continue
                    except Exception:
                        pass

            if resp.status_code == 404:
                # Upload session expired or invalidated by server. Try to recreate automatically.
                try:
                    q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                    if q.status_code == 404:
                        if log_fn:
                            log_fn("Upload session expired or invalidated by server. Attempting to recreate session and resume.")
                        _delete_session(key)
                        # Create new session
                        session_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/createUploadSession"
                        session = _get_session()
                        r = session.post(session_url, headers=headers_json, json={}, timeout=30)
                        if r.status_code not in (200, 201):
                            if log_fn:
                                log_fn(f"Failed to recreate upload session: {r.status_code} {r.text}")
                            return uploaded_bytes
                        resp = r.json()
                        upload_url = resp['uploadUrl']
                        sess = {"uploadUrl": upload_url, "remote_path": remote_path}
                        _save_session(key, sess)
                        # Seek to current uploaded_bytes position
                        f.seek(uploaded_bytes)
                        if log_fn:
                            log_fn("New upload session created. Resuming upload from previous position.")
                        backoff = 1.0
                        continue
                except Exception as ex:
                    if log_fn:
                        log_fn(f"Failed to recreate upload session after 404: {ex}")
                    return uploaded_bytes

            if resp.status_code in (200, 201):
                # finished
                uploaded_bytes = int(file_size)
                if progress_fn:
                    try:
                        progress_fn(float(uploaded_bytes), float(file_size), 0.0, 0.0)
                    except TypeError:
                        progress_fn(float(uploaded_bytes), float(file_size))
                _delete_session(key)
                if log_fn:
                    log_fn(f"Uploaded {remote_path} ({file_size / (1024*1024*1024):.2f} GB)")
                if log_fn:
                    log_fn(f"Final chunk size used ~{chunk_size / (1024*1024):.1f} MiB")
                return file_size

            if resp.status_code == 202:
                # accepted partial, advance by reported range or our chunk
                hdr_pos = _parse_uploaded_from_headers(resp.headers.get("Range") or resp.headers.get("Content-Range"))
                if hdr_pos is not None:
                    uploaded_bytes = int(hdr_pos)
                else:
                    try:
                        uploaded_bytes = int(_parse_next_start(resp.json()))
                    except Exception:
                        uploaded_bytes = int(end + 1)

                # --- Adaptive resizing based on last successful fragment time ---
                t1 = time.time()
                last_chunk_bytes = len(chunk)
                last_chunk_time = max(1e-3, t1 - t0)
                inst_speed = last_chunk_bytes / last_chunk_time  # bytes/sec
                if ema_speed is None:
                    ema_speed = inst_speed
                else:
                    ema_speed = _ADJUST_SMOOTHING * inst_speed + (1 - _ADJUST_SMOOTHING) * ema_speed
                chunks_since_adjust += 1
                if adaptive and chunks_since_adjust >= _ADJUST_EVERY_N_CHUNKS:
                    target_bytes = ema_speed * _TARGET_CHUNK_SECONDS
                    new_chunk = int(min(max(_round_to_320k(int(target_bytes)), _MIN_CHUNK), _MAX_CHUNK))
                    # Avoid tiny oscillations; only apply if change is significant (>=25%)
                    if abs(new_chunk - chunk_size) / float(chunk_size) >= 0.25:
                        chunk_size = new_chunk
                        if log_fn:
                            log_fn(f"Adjusted chunk size to {chunk_size / (1024*1024):.1f} MiB based on ~{ema_speed/1024/1024:.2f} MiB/s")
                    chunks_since_adjust = 0

                # update speed & eta
                elapsed = max(1e-6, time.time() - start_time)
                speed = uploaded_bytes / elapsed
                eta = (file_size - uploaded_bytes) / speed if speed > 0 else 0.0

                if progress_fn:
                    try:
                        progress_fn(float(uploaded_bytes), float(file_size), float(speed), float(eta))
                    except TypeError:
                        progress_fn(float(uploaded_bytes), float(file_size))

                chunks_since_save += 1
                now = time.time()
                if chunks_since_save >= 3 or (now - last_save_time) >= 30:
                    # persist session after every 3 chunks or 30 seconds
                    _save_session(key, {"uploadUrl": upload_url, "remote_path": remote_path, "uploaded": int(uploaded_bytes)})
                    last_save_time = now
                    chunks_since_save = 0

                # reset backoff on success
                backoff = 1.0

                # allow stop between chunks
                if callable(should_stop) and should_stop():
                    _save_session(key, {"uploadUrl": upload_url, "remote_path": remote_path, "uploaded": int(uploaded_bytes)})
                    if log_fn:
                        log_fn("Stop requested between chunks. Session saved.")
                    return uploaded_bytes
                continue

            # other errors -> retry with backoff and re-query nextExpectedRanges
            if log_fn:
                log_fn(f"Chunk upload failed: {resp.status_code} {resp.text[:200]}")
            # Reset session to avoid stale or closed connections after server errors
            _reset_session()
            session = _get_session()
            time.sleep(min(backoff * (0.5 + random.random()), 30))
            backoff = min(backoff * 2, 30)
            chunks_since_adjust = 0
            try:
                q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                if q.status_code in (200, 201, 202):
                    hdr_pos = _parse_uploaded_from_headers(q.headers.get("Range") or q.headers.get("Content-Range"))
                    uploaded_bytes = int(hdr_pos if hdr_pos is not None else _parse_next_start(q.json()))
                    f.seek(uploaded_bytes)
            except Exception:
                pass

    # If loop ends without completion, keep session for resume
    if log_fn:
        log_fn("Upload interrupted; session saved for resume")
    return uploaded_bytes

def _normalize_remote_path(base, rel_path):
    """
    规范化 OneDrive 远程路径，防止重复或反斜杠错误。
    """
    if base:
        path = f"{base.rstrip('/')}/{rel_path.lstrip('/')}"
    else:
        path = rel_path.lstrip('/')
    return path.replace("\\", "/")