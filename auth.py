# auth.py
import json, os, time
from msal import PublicClientApplication, SerializableTokenCache

CFG_PATH = "config.json"

def load_config():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

cfg = load_config()
CLIENT_ID = cfg["client_id"]
SCOPES = cfg.get("scopes", ["Files.ReadWrite.All"])
CACHE_FILE = cfg.get("token_cache_file", "token_cache.bin")

def _load_cache():
    cache = SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        try:
            cache.deserialize(open(CACHE_FILE, "rb").read())
        except Exception:
            pass
    return cache

def _save_cache(cache):
    if cache.has_state_changed:
        with open(CACHE_FILE, "wb") as f:
            data = cache.serialize()
            if isinstance(data, str):
                data = data.encode("utf-8")
            f.write(data)

def create_msal_app():
    cache = _load_cache()
    app = PublicClientApplication(client_id=CLIENT_ID, token_cache=cache)
    return app, cache

def list_accounts():
    app, cache = create_msal_app()
    accounts = app.get_accounts()
    # return list of dicts with username, home_account_id
    return [{"username": a.get("username"), "home_account_id": a.get("home_account_id")} for a in accounts]

def acquire_token_silent_for_account(home_account_id=None):
    app, cache = create_msal_app()
    accounts = app.get_accounts()
    target = None
    if home_account_id:
        for a in accounts:
            if a.get("home_account_id") == home_account_id:
                target = a
                break
    elif accounts:
        target = accounts[0]
    if target:
        result = app.acquire_token_silent(SCOPES, account=target)
        _save_cache(cache)
        if result and "access_token" in result:
            return result["access_token"], target
    return None, None

def acquire_token_device_flow():
    app, cache = create_msal_app()
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError("Failed to create device flow: %r" % flow)
    return flow  # caller should show verification_uri and user_code and then call acquire_token_by_device_flow

def complete_device_flow(flow):
    app, cache = create_msal_app()
    result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    if "access_token" in result:
        accounts = app.get_accounts()
        # return the last account (most recent)
        return result["access_token"], accounts[-1]
    raise RuntimeError("Device flow failed: %r" % result)


# Interactive login flow
def acquire_token_interactive():
    app, cache = create_msal_app()
    try:
        result = app.acquire_token_interactive(scopes=SCOPES)
        _save_cache(cache)
        if "access_token" in result:
            accounts = app.get_accounts()
            return result["access_token"], accounts[-1]
        else:
            raise RuntimeError(f"Interactive flow failed: {result}")
    except Exception as e:
        raise RuntimeError(f"Interactive flow error: {e}")

def remove_account(home_account_id):
    app, cache = create_msal_app()
    accounts = app.get_accounts()
    removed = False
    for a in accounts:
        if a.get("home_account_id") == home_account_id:
            app.remove_account(a)
            removed = True
    _save_cache(cache)
    return removed