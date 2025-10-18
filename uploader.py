def clear_old_sessions():
    """清空 .sessions 目录，防止无效续传残留。"""
    for f in SESS_DIR.glob("*.json"):
        try:
            f.unlink()
        except Exception:
            pass
import os
import time
import json
import requests
import hashlib
from pathlib import Path
from auth import acquire_token_silent_for_account, acquire_token_interactive

# --- Session helpers for resumable upload ---
# 使用系统支持的用户写入路径，避免 .app 打包后无法写入问题
SESS_DIR = Path.home() / "Library/Application Support/OneDriveUploader/sessions"
SESS_DIR.mkdir(parents=True, exist_ok=True)

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
    # nextExpectedRanges like ["0-","10485760-"]
    ranges = resp_json.get('nextExpectedRanges') or []
    if not ranges:
        return 0
    first = str(ranges[0])
    # format "start-end" or "start-"
    start_str = first.split('-')[0]
    try:
        return int(start_str)
    except Exception:
        return 0
def upload_items(file_list, base_dir="", remote_base="", account_home_id=None, progress_cb=None, log_cb=None):

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

    uploaded_bytes = 0
    start_time = time.time()

    for abs_path, size in abs_file_list:
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

        actual_size = upload_file(abs_path, rp, account_home_id=account_home_id, progress_fn=pf, log_fn=log_cb)
        uploaded_bytes += actual_size

        if progress_cb:
            try:
                progress_cb(uploaded_bytes, total_bytes, 0, 0)
            except TypeError:
                progress_cb(uploaded_bytes, total_bytes)

    if log_cb:
        duration = time.time() - start_time
        log_cb(f"All files uploaded ({total_bytes / (1024*1024*1024):.2f} GB in {duration:.1f}s)")
    return True

def upload_file(local_path, remote_path, account_home_id=None, progress_fn=None, log_fn=None, chunk_size_mb=10):
    """
    使用 OneDrive 分段上传会话，支持断点续传与重试。
    会在 ./.sessions 目录保存会话信息，异常中断后可继续上传。
    """
    import time

    token, _ = acquire_token_silent_for_account(account_home_id)
    if not token:
        token, _ = acquire_token_interactive()

    file_size = float(os.path.getsize(local_path))
    chunk_size = max(1, int(chunk_size_mb)) * 1024 * 1024

    key = _session_key(local_path, remote_path, file_size)
    sess = _load_session(key) or {}
    upload_url = sess.get('uploadUrl')

    headers_json = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Create session if none
    if not upload_url:
        session_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/createUploadSession"
        with requests.Session() as session:
            r = session.post(session_url, headers=headers_json, json={}, timeout=10)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Failed to create upload session: {r.status_code} {r.text}")
            resp = r.json()
            upload_url = resp['uploadUrl']
        sess = {"uploadUrl": upload_url, "remote_path": remote_path}
        _save_session(key, sess)
        if log_fn:
            log_fn("Upload session created")

    # Try to query current progress to resume
    uploaded_bytes = 0.0
    try:
        with requests.Session() as session:
            q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if q.status_code in (200, 201, 202):
                uploaded_bytes = float(_parse_next_start(q.json()))
    except Exception:
        pass

    start_time = time.time()
    last_save_time = start_time
    chunks_since_save = 0
    # Emit initial progress if resuming
    if progress_fn and uploaded_bytes > 0:
        try:
            progress_fn(float(uploaded_bytes), float(file_size), 0.0, max(0.0, (file_size-uploaded_bytes)/1.0))
        except TypeError:
            progress_fn(float(uploaded_bytes), float(file_size))

    max_retries = 5
    backoff = 1.0

    with open(local_path, 'rb') as f, requests.Session() as session:
        # seek to resume point
        if uploaded_bytes > 0:
            f.seek(int(uploaded_bytes))
        while uploaded_bytes < file_size:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            start = uploaded_bytes
            end = start + len(chunk) - 1
            put_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {int(start)}-{int(end)}/{int(file_size)}",
            }
            try:
                resp = session.put(upload_url, headers=put_headers, data=chunk, timeout=30)
            except Exception as ex:
                # network error, retry after backoff
                if log_fn:
                    log_fn(f"Network error, retrying: {ex}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                # re-query session position
                try:
                    q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    if q.status_code in (200,201,202):
                        uploaded_bytes = float(_parse_next_start(q.json()))
                        f.seek(int(uploaded_bytes))
                        continue
                except Exception:
                    pass
                continue

            if resp.status_code in (200, 201):
                # finished
                uploaded_bytes = file_size
                if progress_fn:
                    try:
                        progress_fn(float(uploaded_bytes), float(file_size), 0.0, 0.0)
                    except TypeError:
                        progress_fn(float(uploaded_bytes), float(file_size))
                _delete_session(key)
                if log_fn:
                    log_fn(f"Uploaded {remote_path} ({file_size / (1024*1024*1024):.2f} GB)")
                return file_size

            if resp.status_code == 202:
                # accepted partial, advance by reported range or our chunk
                try:
                    uploaded_bytes = float(_parse_next_start(resp.json()))
                except Exception:
                    uploaded_bytes = float(end + 1)

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
                continue

            # other errors -> retry with backoff and re-query nextExpectedRanges
            if log_fn:
                log_fn(f"Chunk upload failed: {resp.status_code} {resp.text[:200]}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            try:
                q = session.get(upload_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
                if q.status_code in (200,201,202):
                    uploaded_bytes = float(_parse_next_start(q.json()))
                    f.seek(int(uploaded_bytes))
            except Exception:
                pass

    # If loop ends without completion, keep session for resume
    if log_fn:
        log_fn("Upload interrupted; session saved for resume")
    return file_size
def _normalize_remote_path(base, rel_path):
    """
    规范化 OneDrive 远程路径，防止重复或反斜杠错误。
    """
    if base:
        path = f"{base.rstrip('/')}/{rel_path.lstrip('/')}"
    else:
        path = rel_path.lstrip('/')
    return path.replace("\\", "/")