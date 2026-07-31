from .auth import (
    has_persistent_profile,
    has_saved_session,
    import_browser_cookies,
    import_ozon_profile_cookies,
    load_storage_state,
)
from .browser import has_ozon_chrome_profile


def resolve_storage_state(use_auth: bool, import_browser: bool = True) -> dict | None:
    """Сессия браузера: профиль Ozon/CDP не требует storage_state."""
    if has_ozon_chrome_profile() and not use_auth:
        return None

    if has_persistent_profile() and not use_auth:
        return None

    state = load_storage_state()
    if state and state.get("cookies"):
        if use_auth or import_browser:
            return state
        return None

    if not import_browser and not use_auth:
        return None

    if not use_auth:
        cookies = import_ozon_profile_cookies()
        if cookies:
            return {"cookies": cookies, "origins": []}

    if use_auth or import_browser:
        cookies = import_browser_cookies()
        if cookies:
            return {"cookies": cookies, "origins": []}

    return None


def session_ready() -> bool:
    return has_ozon_chrome_profile() or has_persistent_profile() or has_saved_session()
