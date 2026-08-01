"""Ozon Parser — парсер товаров с «Баллами за отзыв»."""

from __future__ import annotations

import traceback
from pathlib import Path
import sys


def _prepare_runtime() -> None:
    """Ensure writable folders exist before the GUI starts."""
    output_dir = Path(r"C:\Ozon")
    session_dir = output_dir / "session"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _show_startup_error(exc: BaseException) -> None:
    message = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "Ozon Parser — ошибка запуска",
            "Не удалось запустить приложение.\n\n"
            "Убедитесь, что установлен Google Chrome или Microsoft Edge.\n\n"
            f"{exc}",
        )
        # Keep a copy for support when the message box is closed.
        log_path = Path(r"C:\Ozon") / "startup_error.log"
        log_path.write_text(message, encoding="utf-8")
        app.quit()
    except Exception:
        log_path = Path(r"C:\Ozon") / "startup_error.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(message, encoding="utf-8")
        except OSError:
            pass
        print(message, file=sys.stderr)


def main() -> int:
    _prepare_runtime()
    try:
        from ozon_parser.app import run_app

        run_app()
        return 0
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1
    except BaseException as exc:
        _show_startup_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
