import requests
import os
import sys
import time
import subprocess
from packaging.version import Version
import send2trash
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QProgressBar, QPushButton, QLabel, QMessageBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QIcon


# ================== Фоновый поток загрузки ==================
class DownloadThread(QThread):
    progress = pyqtSignal(int)        # скачано байт
    finished = pyqtSignal(str)        # путь к загруженному файлу
    error = pyqtSignal(str)           # сообщение об ошибке

    def __init__(self, download_url, target_path, total_size):
        super().__init__()
        self.download_url = download_url
        self.target_path = target_path
        self.total_size = total_size

    def run(self):
        try:
            response = requests.get(self.download_url, stream=True, timeout=(10, 30))
            downloaded = 0
            with open(self.target_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if self.isInterruptionRequested():
                        # Прервано пользователем – удаляем временный файл и выходим
                        f.close()
                        try:
                            if os.path.exists(self.target_path):
                                os.unlink(self.target_path)
                        except OSError:
                            pass
                        return
                    f.write(chunk)
                    downloaded += len(chunk)
                    if self.total_size > 0:
                        self.progress.emit(downloaded)
            # Загрузка завершена успешно
            self.finished.emit(self.target_path)
        except Exception as e:
            self.error.emit(str(e))
            try:
                if os.path.exists(self.target_path):
                    os.unlink(self.target_path)
            except OSError:
                pass


# ================== Основной класс проверки обновлений ==================
class UpdateChecker:
    def __init__(self, current_version: str, title: str, log_func=None):
        self.current = current_version
        self.title = title
        self.repo = f"dimak222/{title}"
        self.latest_tag = None
        self.assets = []
        self.log = log_func if log_func else lambda msg: None

    def check(self) -> bool:
        self.log("Проверка обновлений...")
        try:
            url = f"https://api.github.com/repos/{self.repo}/releases/latest"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self.latest_tag = data["tag_name"]
                self.assets = data.get("assets", [])
                if Version(self.latest_tag) > Version(self.current):
                    self.log(f"Найдена новая версия: {self.latest_tag}")
                    return True
                else:
                    self.log("Обновлений нет.")
        except Exception as e:
            self.log(f"Ошибка проверки обновлений: {e}")
        return False

    def show_update_dialog(self, app_title, app_ver, parent=None):
        try:
            msg = QMessageBox(parent)
            msg.setWindowTitle(f"{app_title} {app_ver}")
            msg.setText(f"Новая версия {self.latest_tag}\n"
                         f"Текущая версия: {self.current}\n\n"
                         "Скачать и установить обновление?")
            msg.setIcon(QMessageBox.Icon.Information)
            yes_btn = msg.addButton("Да", QMessageBox.ButtonRole.YesRole)
            no_btn = msg.addButton("Нет", QMessageBox.ButtonRole.NoRole)
            msg.setDefaultButton(yes_btn)

            icon_path = os.path.join(os.path.dirname(sys.argv[0]), "icon.ico")
            if os.path.exists(icon_path):
                msg.setWindowIcon(QIcon(icon_path))

            if parent is None:
                msg.setWindowFlags(msg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            msg.exec()
            return msg.clickedButton() == yes_btn
        except Exception as e:
            self.log(f"Ошибка показа диалога: {e}")
            return False

    # ================== Метод установки обновления ==================
    def download_and_install(self, parent=None):
        exe_asset = None
        for asset in self.assets:
            if asset["name"].endswith(".exe"):
                exe_asset = asset
                break
        if not exe_asset:
            self.log("Ошибка: EXE-файл не найден в релизе.")
            err_msg = QMessageBox(parent)
            err_msg.setWindowTitle("Ошибка")
            err_msg.setText("Не найден исполняемый файл в релизе.")
            err_msg.setIcon(QMessageBox.Icon.Warning)
            if parent is None:
                err_msg.setWindowFlags(err_msg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            err_msg.exec()
            return

        download_url = exe_asset["browser_download_url"]
        old_exe = sys.argv[0]
        dir_name = os.path.dirname(old_exe)
        ext = os.path.splitext(old_exe)[1].lower()

        if ext in ('.py', '.pyw'):
            target_exe = os.path.join(dir_name, f"{self.title}.exe")
        else:
            target_exe = os.path.join(dir_name, f"{self.title}_new.exe")

        total_size = exe_asset.get("size", 0)
        self.log(f"Начало загрузки обновления: {self.latest_tag} ({total_size} байт)")

        dlg = QDialog(parent)
        dlg.setWindowTitle(f"{self.title} {self.current}")
        icon_path = os.path.join(dir_name, "icon.ico")
        if os.path.exists(icon_path):
            dlg.setWindowIcon(QIcon(icon_path))
        dlg.setMinimumWidth(400)
        if parent is None:
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(dlg)
        label = QLabel("Загрузка обновления...")
        layout.addWidget(label)

        progress = QProgressBar()
        progress.setRange(0, total_size if total_size > 0 else 0)
        progress.setFormat("%p%")
        progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(progress)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Отмена")
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        thread = DownloadThread(download_url, target_exe, total_size)
        cancel_done = False
        download_started = False
        is_auto_cancel = False
        timer = None

        def set_started():
            nonlocal download_started
            download_started = True

        def on_finished(path):
            nonlocal cancel_done, download_started
            if cancel_done:
                return
            cancel_done = True
            download_started = True
            if timer is not None:
                timer.stop()
            self.log("Загрузка завершена успешно.")
            dlg.accept()

        def on_error(msg):
            nonlocal cancel_done, download_started
            if cancel_done:
                return
            cancel_done = True
            download_started = True
            if timer is not None:
                timer.stop()
            self.log(f"Ошибка загрузки: {msg}")
            dlg.reject()
            QMessageBox.warning(dlg, "Ошибка", f"Ошибка при обновлении: {msg}")

        def on_cancel():
            nonlocal cancel_done, is_auto_cancel
            if cancel_done:
                return
            cancel_done = True
            if timer is not None:
                timer.stop()

            # Логируем "Загрузка отменена пользователем" только если это не автоматическая отмена
            if not is_auto_cancel:
                self.log("Загрузка отменена пользователем.")

            thread.requestInterruption()
            # Закрываем диалог немедленно, не дожидаясь потока
            try:
                if dlg.isVisible():
                    dlg.reject()
            except RuntimeError:
                pass

        def auto_cancel_if_no_start():
            nonlocal cancel_done, download_started, is_auto_cancel
            if not cancel_done and not download_started:
                is_auto_cancel = True
                self.log("Превышено время ожидания начала загрузки (10 сек). Обновление отменено.")
                on_cancel()

                # Предупреждение автоматически закроется через 5 секунд
                warn = QMessageBox(dlg)
                warn.setWindowTitle("Превышено время ожидания")
                warn.setText("Не удалось начать загрузку в течение 10 секунд.")
                warn.setIcon(QMessageBox.Icon.Warning)
                warn.setStandardButtons(QMessageBox.StandardButton.Ok)
                QTimer.singleShot(5000, warn.close)
                warn.exec()

        timer = QTimer.singleShot(10000, auto_cancel_if_no_start)

        thread.progress.connect(lambda downloaded: (
            progress.setValue(downloaded),
            progress.repaint(),
            set_started()
        ))
        thread.finished.connect(on_finished)
        thread.error.connect(on_error)
        cancel_btn.clicked.connect(on_cancel)
        dlg.rejected.connect(on_cancel)   # крестик или Esc

        thread.start()
        dlg.exec()

        # После закрытия диалога только убедимся, что поток прерван
        # Никаких ожиданий – всё происходит мгновенно
        if thread.isRunning():
            thread.requestInterruption()

        # Если загрузка успешно завершилась (файл есть и не было отмены)
        if os.path.exists(target_exe) and not thread.isInterruptionRequested():
            self.log("Установка обновления...")
            if ext in ('.py', '.pyw'):
                subprocess.Popen([target_exe])
                time.sleep(0.5)
                QApplication.quit()
            else:
                old_backup = old_exe + ".old"
                if os.path.exists(old_backup):
                    os.remove(old_backup)
                os.rename(old_exe, old_backup)
                os.rename(target_exe, old_exe)
                subprocess.Popen([old_exe])
                time.sleep(0.5)
                try:
                    send2trash.send2trash(old_backup)
                except ImportError:
                    try:
                        os.remove(old_backup)
                    except Exception:
                        pass
                QApplication.quit()
        else:
            # Отмена или ошибка — файл уже удалён внутри потока или в error-обработчике
            # Дополнительно подчищаем, если файл остался (параноидальная защита)
            try:
                if os.path.exists(target_exe):
                    os.unlink(target_exe)
            except OSError:
                pass


# ================== Поток для фоновой проверки ==================
class UpdateCheckThread(QThread):
    update_available = pyqtSignal(object)

    def __init__(self, current_version, title, log_func=None):
        super().__init__()
        self.current_version = current_version
        self.title = title
        self.log_func = log_func

    def run(self):
        try:
            updater = UpdateChecker(self.current_version, self.title, log_func=self.log_func)
            if updater.check():
                self.update_available.emit(updater)
        except Exception:
            pass


def start_update_check(app_title, app_ver, parent_widget, log_callback=None):
    """
    Запускает фоновую проверку обновлений.
    При обнаружении новой версии автоматически показывает диалог
    (модально относительно parent_widget) и запускает установку.

    log_callback – функция для вывода сообщений в лог программы.
                     Может принимать один аргумент (строку) или
                     несколько (напр. window.append_log(msg, is_error, show_time)).
    """
    # Оборачиваем переданный колбэк, чтобы гарантированно вызывать с одним аргументом
    def safe_log(msg):
        if log_callback is None:
            return
        try:
            log_callback(msg)
        except TypeError:
            try:
                log_callback(msg, False)
            except TypeError:
                log_callback(msg, False, True)

    thread = UpdateCheckThread(app_ver, app_title, log_func=safe_log)

    def on_available(updater):
        try:
            if updater.show_update_dialog(app_title, app_ver, parent=parent_widget):
                updater.download_and_install(parent=parent_widget)
            else:
                safe_log("Обновление отменено пользователем.")
        except Exception:
            pass

    thread.update_available.connect(on_available)
    thread.start()
    return thread