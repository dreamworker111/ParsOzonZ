import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

import browser_cookie3

from .config import CHROME_OZON_PROFILE, MOBILE_CHROME_PROFILE, SESSION_DIR

BROWSER_PROFILE_DIR = SESSION_DIR / "browser_profile"
MOBILE_SESSION_MARKER = SESSION_DIR / "mobile_session.json"


@dataclass
class ImportResult:
    cookies: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.cookies)


def ensure_session_dir() -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR


def session_file_path() -> Path:
    return ensure_session_dir() / "ozon_session.json"


def browser_profile_dir() -> Path:
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return BROWSER_PROFILE_DIR


def mobile_browser_profile_dir() -> Path:
    """Return the dedicated persistent profile used only by mobile auth."""
    MOBILE_CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    return MOBILE_CHROME_PROFILE


def _profile_initialized(profile: Path) -> bool:
    markers = ("Default", "Local State", "first_party_sets.db")
    return profile.exists() and any((profile / name).exists() for name in markers)


def has_persistent_profile() -> bool:
    profile = browser_profile_dir()
    return _profile_initialized(profile)


def mark_mobile_session_saved() -> None:
    """Record a mobile session only after the login state was verified."""
    ensure_session_dir()
    MOBILE_SESSION_MARKER.write_text(
        json.dumps(
            {
                "profile": str(MOBILE_CHROME_PROFILE),
                "authenticated": True,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_mobile_session_marker() -> None:
    try:
        MOBILE_SESSION_MARKER.unlink(missing_ok=True)
    except OSError:
        pass


def has_mobile_saved_session() -> bool:
    """Return true only for a profile whose successful login was verified."""
    if not MOBILE_SESSION_MARKER.exists() or not _profile_initialized(MOBILE_CHROME_PROFILE):
        return False
    try:
        marker = json.loads(MOBILE_SESSION_MARKER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        marker.get("authenticated") is True
        and marker.get("profile") == str(MOBILE_CHROME_PROFILE)
    )


def has_saved_session() -> bool:
    state = load_storage_state()
    return bool(state and state.get("cookies"))


def save_storage_state(storage_state: dict) -> None:
    path = session_file_path()
    path.write_text(json.dumps(storage_state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_storage_state() -> dict | None:
    path = session_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ""))


def _discover_cookie_files() -> list[tuple[str, Path]]:
    """Find cookie databases: сначала профиль Chrome для Ozon, затем системные браузеры."""
    candidates: list[tuple[str, Path]] = []

    ozon_profile = CHROME_OZON_PROFILE / "Default"
    if ozon_profile.exists():
        for rel in ("Network/Cookies", "Cookies"):
            cookie_file = ozon_profile / rel
            if cookie_file.exists():
                candidates.append((f"OzonChrome/Default/{rel}", cookie_file))

    roots = (
        ("Chrome", _local_appdata() / "Google" / "Chrome" / "User Data"),
        ("Edge", _local_appdata() / "Microsoft" / "Edge" / "User Data"),
        ("Yandex", _local_appdata() / "Yandex" / "YandexBrowser" / "User Data"),
    )
    profile_names = ["Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4"]

    for browser_name, root in roots:
        if not root.exists():
            continue
        for profile in profile_names:
            profile_dir = root / profile
            if not profile_dir.exists():
                continue
            for rel in ("Network/Cookies", "Cookies"):
                cookie_file = profile_dir / rel
                if cookie_file.exists():
                    candidates.append((f"{browser_name}/{profile}/{rel}", cookie_file))

    return candidates


def _copy_cookie_db(source: Path) -> Path | None:
    tmp = Path(tempfile.gettempdir()) / f"ozon_cookies_{source.stat().st_size}.db"
    try:
        shutil.copy2(source, tmp)
        return tmp
    except PermissionError:
        return None
    except OSError:
        return None


def _read_cookies_sqlite(cookie_file: Path) -> list[dict]:
    """Read ozon cookies directly from SQLite (works when values are not encrypted)."""
    tmp = _copy_cookie_db(cookie_file)
    if not tmp:
        return []

    rows: list[dict] = []
    try:
        conn = sqlite3.connect(str(tmp))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name, value, host_key, path, expires_utc, is_secure, is_httponly
            FROM cookies
            WHERE host_key LIKE '%ozon.ru%'
            """
        )
        for row in cur.fetchall():
            value = row["value"]
            if not value:
                continue
            rows.append(
                {
                    "name": row["name"],
                    "value": value,
                    "domain": row["host_key"],
                    "path": row["path"] or "/",
                    "expires": float(row["expires_utc"]) if row["expires_utc"] else -1,
                    "httpOnly": bool(row["is_httponly"]),
                    "secure": bool(row["is_secure"]),
                    "sameSite": "Lax",
                }
            )
        conn.close()
    except Exception:
        return []
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return rows


def _load_via_browser_cookie3(cookie_file: Path, browser: str) -> list[dict]:
    tmp = _copy_cookie_db(cookie_file)
    if not tmp:
        return []

    cookies: list[dict] = []
    domains = (".ozon.ru", "ozon.ru", "www.ozon.ru", "m.ozon.ru")
    try:
        loader = {
            "Chrome": browser_cookie3.chrome,
            "Edge": browser_cookie3.edge,
            "Yandex": browser_cookie3.chrome,
        }.get(browser, browser_cookie3.chrome)

        for domain in domains:
            try:
                for cookie in loader(domain_name=domain, cookie_file=str(tmp)):
                    cookies.append(
                        {
                            "name": cookie.name,
                            "value": cookie.value,
                            "domain": cookie.domain or ".ozon.ru",
                            "path": cookie.path or "/",
                            "expires": float(cookie.expires) if cookie.expires else -1,
                            "httpOnly": bool(getattr(cookie, "_rest", {}).get("HttpOnly", False)),
                            "secure": bool(cookie.secure),
                            "sameSite": "Lax",
                        }
                    )
            except Exception:
                continue
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return cookies


def _dedupe_cookies(cookies: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for cookie in cookies:
        key = (cookie["name"], cookie.get("domain", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(cookie)
    return result


def import_browser_cookies() -> list[dict]:
    return import_browser_cookies_detailed().cookies


def import_ozon_profile_cookies() -> list[dict]:
    """Cookies из профиля Chrome для Ozon (C:\\Ozon\\ChromeProfile)."""
    profile = CHROME_OZON_PROFILE / "Default"
    if not profile.exists():
        return []
    for rel in ("Network/Cookies", "Cookies"):
        cookie_file = profile / rel
        if not cookie_file.exists():
            continue
        for loader in (
            lambda: _load_via_browser_cookie3(cookie_file, "Chrome"),
            lambda: _read_cookies_sqlite(cookie_file),
        ):
            cookies = loader()
            if cookies:
                return _dedupe_cookies(cookies)
    return []


def import_browser_cookies_detailed() -> ImportResult:
    result = ImportResult()
    discovered = _discover_cookie_files()

    if not discovered:
        result.errors.append("Не найдены профили Chrome, Edge или Yandex на этом компьютере.")
        return result

    browser_locked = False

    for label, cookie_file in discovered:
        browser_name = label.split("/")[0]

        bc3_cookies = _load_via_browser_cookie3(cookie_file, browser_name)
        if bc3_cookies:
            result.cookies.extend(bc3_cookies)
            result.source = label
            break

        if _copy_cookie_db(cookie_file) is None:
            browser_locked = True
            continue

        sqlite_cookies = _read_cookies_sqlite(cookie_file)
        if sqlite_cookies:
            result.cookies.extend(sqlite_cookies)
            result.source = label
            break

    result.cookies = _dedupe_cookies(result.cookies)

    if result.cookies:
        save_storage_state({"cookies": result.cookies, "origins": []})
        return result

    if browser_locked:
        result.errors.append(
            "Браузер открыт — файл cookies заблокирован.\n"
            "Полностью закройте Chrome, Edge и Yandex (включая фоновые процессы в диспетчере задач)."
        )
    else:
        result.errors.append(
            "Cookies Ozon не найдены.\n"
            "Сначала откройте ozon.ru в браузере и дождитесь загрузки сайта."
        )

    result.errors.append(
        "Рекомендуется: нажмите «Войти через браузер» — это работает без импорта cookies."
    )
    return result


def cookies_to_storage_state(cookies: list[dict]) -> dict:
    return {"cookies": cookies, "origins": []}
