import os
import requests
from auth import acquire_token_silent_for_account, acquire_token_interactive, complete_device_flow

def _normalize_remote_path(base, rel_path):
    if base:
        path = f"{base.rstrip('/')}/{rel_path.lstrip('/')}"
    else:
        path = rel_path.lstrip('/')
    return path.replace("\\", "/")

def upload_file(local_path, remote_path, account_home_id=None, progress_fn=None, log_fn=None):
    """
    上传单个文件到 OneDrive
    """
    token, _ = acquire_token_silent_for_account(account_home_id)
    if not token:
        token, _ = acquire_token_interactive()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream"
    }
    with open(local_path, "rb") as f:
        content = f.read()
    upload_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{remote_path}:/content"
    r = requests.put(upload_url, headers=headers, data=content)
    if log_fn:
        if r.status_code in (200, 201):
            log_fn(f"Uploaded {remote_path}")
        else:
            log_fn(f"Failed {remote_path}: {r.status_code} {r.text}")
    if progress_fn:
        progress_fn(len(content), len(content))

def upload_items(file_list, base_dir="", remote_base="", account_home_id=None, progress_cb=None, log_cb=None):
    import os

    # 保留 base_dir 的最后一级目录作为远程根
    if base_dir:
        base_dir = os.path.abspath(base_dir)
        top_level_name = os.path.basename(base_dir.rstrip(os.sep))
    else:
        top_level_name = ""

    # 收集文件列表，排除 .DS_Store 等隐藏文件
    abs_file_list = []
    total_bytes = 0
    for file_path in file_list:
        abs_path = os.path.abspath(file_path)
        # 排除 macOS 隐藏文件
        if os.path.basename(abs_path).startswith('.'):
            continue
        abs_file_list.append(abs_path)
        total_bytes += os.path.getsize(abs_path)

    uploaded_bytes = 0
    if log_cb: log_cb(f"Found {len(abs_file_list)} files, total {total_bytes} bytes")
    for abs_path in abs_file_list:
        rel = os.path.relpath(abs_path, base_dir) if base_dir else os.path.basename(abs_path)
        # 在最上级目录前加 top_level_name
        rel = os.path.join(top_level_name, rel)
        rp = _normalize_remote_path(remote_base, rel)
        if log_cb: log_cb(f"Uploading {rel}")
        def pf(current, total):
            nonlocal uploaded_bytes
            if progress_cb:
                progress_cb(uploaded_bytes + current, total_bytes)
        upload_file(abs_path, rp, account_home_id=account_home_id, progress_fn=pf, log_fn=log_cb)
        uploaded_bytes += os.path.getsize(abs_path)
        if progress_cb:
            progress_cb(uploaded_bytes, total_bytes)
    if log_cb: log_cb("All files uploaded")
    return True