import os
import time
import json
import requests
from auth import acquire_token_silent_for_account, acquire_token_interactive
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
    使用 OneDrive 分段上传 API 实现大文件上传（支持实时进度与续传）
    """
    import time

    token, _ = acquire_token_silent_for_account(account_home_id)
    if not token:
        token, _ = acquire_token_interactive()

    file_size = float(os.path.getsize(local_path))  # 确保为 float 类型
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # 创建上传会话
    session_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/createUploadSession"
    r = requests.post(session_url, headers=headers, json={})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create upload session: {r.status_code} {r.text}")
    upload_url = r.json()["uploadUrl"]

    # 上传文件分片
    chunk_size = int(chunk_size_mb * 1024 * 1024)
    uploaded_bytes = 0.0
    start_time = time.time()

    with open(local_path, "rb") as f:
        while uploaded_bytes < file_size:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            start = uploaded_bytes
            end = start + len(chunk) - 1
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {int(start)}-{int(end)}/{int(file_size)}"
            }
            resp = requests.put(upload_url, headers=headers, data=chunk)
            if resp.status_code not in (200, 201, 202):
                if log_fn:
                    log_fn(f"Chunk upload failed: {resp.status_code} {resp.text}")
                time.sleep(1)
                continue

            uploaded_bytes += len(chunk)
            elapsed = time.time() - start_time
            speed = uploaded_bytes / elapsed if elapsed > 0 else 0.0
            remaining = file_size - uploaded_bytes
            eta = remaining / speed if speed > 0 else 0.0

            if progress_fn:
                try:
                    progress_fn(float(uploaded_bytes), float(file_size), float(speed), float(eta))
                except TypeError:
                    progress_fn(float(uploaded_bytes), float(file_size))

    if log_fn:
        log_fn(f"Uploaded {remote_path} ({file_size / (1024 * 1024 * 1024):.2f} GB)")

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