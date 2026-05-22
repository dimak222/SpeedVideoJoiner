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
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

class UpdateChecker:
    def __init__(self, current_version: str, title: str):
        self.current = current_version
        self.title = title
        self.repo = f"dimak222/{title}"
        self.latest_tag = None
        self.assets = []

    def check(self) -> bool:
        try:
            url = f"https://api.github.com/repos/{self.repo}/releases/latest"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self.latest_tag = data["tag_name"]
                self.assets = data.get("assets", [])
                return Version(self.latest_tag) > Version(self.current)
        except Exception:
            pass
        return False

    def show_update_dialog(self, app_title, app_ver):
        msg = QMessageBox()
        msg.setWindowTitle(f"{app_title} {app_ver}")
        msg.setText(f"Новая версия {self.latest_tag}\n"
                     f"Текущая версия: {self.current}\n\n"
                     "Скачать и установить обновление?")
        msg.setIcon(QMessageBox.Icon.Information)
        yes_btn = msg.addButton("Да", QMessageBox.ButtonRole.YesRole)
        no_btn = msg.addButton("Нет", QMessageBox.ButtonRole.NoRole)
        msg.setDefaultButton(no_btn)

        icon_path = os.path.join(os.path.dirname(sys.argv[0]), "icon.ico")
        if os.path.exists(icon_path):
            msg.setWindowIcon(QIcon(icon_path))

        # Делаем окно поверх всех окон
        msg.setWindowFlags(msg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        msg.exec()
        return msg.clickedButton() == yes_btn

    def download_and_install(self):
        exe_asset = None
        for asset in self.assets:
            if asset["name"].endswith(".exe"):
                exe_asset = asset
                break
        if not exe_asset:
            # Ошибка — окно тоже поверх всех окон
            err_msg = QMessageBox()
            err_msg.setWindowTitle("Ошибка")
            err_msg.setText("Не найден исполняемый файл в релизе.")
            err_msg.setIcon(QMessageBox.Icon.Warning)
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

        dlg = QDialog()
        dlg.setWindowTitle(f"{self.title} {self.current}")
        icon_path = os.path.join(dir_name, "icon.ico")
        if os.path.exists(icon_path):
            dlg.setWindowIcon(QIcon(icon_path))
        dlg.setMinimumWidth(400)

        # Делаем окно загрузки поверх всех окон
        dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(dlg)
        label = QLabel("Загрузка обновления...")
        layout.addWidget(label)

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setFormat("%p%")
        progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(progress)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        dlg.show()
        QApplication.processEvents()

        try:
            response = requests.get(download_url, stream=True, timeout=30)
            total_size = int(response.headers.get('content-length', 0))
            if total_size > 0:
                progress.setMaximum(total_size)
            else:
                progress.setMaximum(0)

            downloaded = 0
            with open(target_exe, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not dlg.isVisible():
                        f.close()
                        os.unlink(target_exe)
                        return
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        progress.setValue(downloaded)
                    QApplication.processEvents()

            progress.setValue(progress.maximum())
            QApplication.processEvents()
            time.sleep(0.5)

            dlg.close()

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

        except Exception as e:
            dlg.close()
            # Окно ошибки тоже поверх всех окон
            err_msg = QMessageBox()
            err_msg.setWindowTitle("Ошибка")
            err_msg.setText(f"Ошибка при обновлении: {e}")
            err_msg.setIcon(QMessageBox.Icon.Warning)
            err_msg.setWindowFlags(err_msg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            err_msg.exec()
            if os.path.exists(target_exe):
                os.unlink(target_exe)