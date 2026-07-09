#-------------------------------------------------------------------------------
# Author:      dimak222
#
# Created:     29.04.2026
# Copyright:   (c) dimak222 2026
# Licence:     No
#-------------------------------------------------------------------------------

title = "SpeedVideoJoiner"
ver = "v26.07.0"

#------------------------------Импорт модулей-----------------------------------

import os
import sys
import subprocess
import ffmpeg
import tempfile
import re
import time
import signal
import glob
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import send2trash

from updater import start_update_check # импортируем модуль обновления

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QProgressBar,
    QDoubleSpinBox, QSpinBox, QMessageBox, QTextEdit, QGroupBox,
    QComboBox, QSizePolicy, QListWidget, QAbstractItemView, QCheckBox,
    QRadioButton, QButtonGroup
)
from PyQt6.QtCore import QThread, pyqtSignal, QObject, Qt, QSettings, QTimer, QEvent
from PyQt6.QtGui import QFont, QColor, QDragEnterEvent, QDropEvent, QKeyEvent, QIcon

# ----------------- Форматирование времени -----------------
def format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    total_secs = int(seconds)
    hours, remainder = divmod(total_secs, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours} ч")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes} мин")
    parts.append(f"{secs} сек")
    return " ".join(parts)


def time_stamp() -> str:
    """Возвращает текущее время в формате HHMMSS."""
    t = time.localtime()
    return f"{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}"


# ----------------- Проверки -----------------

def get_video_info(filepath):
    """Возвращает (длительность, is_valid) одним вызовом ffprobe."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', filepath],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip()), True
        else:
            return 0.0, False
    except Exception:
        return 0.0, False

def repair_video(input_path, output_path):
    """
    Пытается восстановить видеофайл несколькими способами:
    1. Агрессивное перекопирование с игнорированием ошибок.
    2. Переупаковка в Matroska (mkv) и обратно в mp4.
    Возвращает True, если удалось, иначе False.
    """
    # Способ 1: агрессивное копирование
    cmd1 = [
        'ffmpeg', '-y',
        '-fflags', '+genpts+igndts+discardcorrupt',
        '-err_detect', 'ignore_err',
        '-i', input_path,
        '-c', 'copy',
        '-max_interleave_delta', '0',
        '-avoid_negative_ts', 'make_zero',
        output_path
    ]
    try:
        subprocess.run(cmd1, capture_output=True, text=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        # Проверим, что файл создался и не пустой
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return True
    except Exception:
        pass

    # Способ 2: переупаковка в MKV и обратно (иногда помогает при повреждённом mp4)
    tmp_mkv = output_path + ".tmp.mkv"
    cmd2 = [
        'ffmpeg', '-y',
        '-fflags', '+genpts+igndts+discardcorrupt',
        '-err_detect', 'ignore_err',
        '-i', input_path,
        '-c', 'copy',
        '-f', 'matroska',
        tmp_mkv
    ]
    try:
        subprocess.run(cmd2, capture_output=True, text=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        if os.path.exists(tmp_mkv) and os.path.getsize(tmp_mkv) > 0:
            # Конвертируем MKV обратно в MP4
            subprocess.run([
                'ffmpeg', '-y',
                '-i', tmp_mkv,
                '-c', 'copy',
                output_path
            ], capture_output=True, text=True, timeout=30,
               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            os.unlink(tmp_mkv)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
    except Exception:
        if os.path.exists(tmp_mkv):
            os.unlink(tmp_mkv)

    return False

def check_encoder_support(codec_name):
    try:
        result = subprocess.run(
            ['ffmpeg', '-encoders'], capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        return codec_name in result.stdout
    except Exception:
        return False

def is_spherical_mp4(filepath):
    """Проверяет, содержит ли MP4‑файл сферические side‑data (Spherical Mapping)."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream_side_data',
             '-of', 'json', filepath],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        return 'Spherical Mapping' in result.stdout
    except Exception:
        return False

def get_mp4box_path():
    """Возвращает путь к mp4box.exe или None."""
    path = shutil.which("mp4box")
    if path:
        return path
    for p in [r"C:\Program Files\GPAC\mp4box.exe", r"C:\GPAC\mp4box.exe"]:
        if os.path.exists(p):
            return p
    return None

# ----------------- Поток обработки -----------------
class EncodeWorker(QObject):
    progress = pyqtSignal(float)
    progress_verify = pyqtSignal(int, int)   # новый сигнал: (готово, всего)
    status = pyqtSignal(str)
    finished = pyqtSignal(bool, str)
    log = pyqtSignal(str, bool)
    eta_update = pyqtSignal(str)
    progress_collect = pyqtSignal(int)
    update_last_number = pyqtSignal(str)
    update_last_number_file = pyqtSignal(str)

    def __init__(self, sources, output_file, speed, target_fps, vcodec, quality, preset, include_audio=True, encoding_enabled=True,
                 sort_mode=0, last_number="", end_number=""):
        super().__init__()
        self.sources = sources
        self.output_file = output_file
        self.speed = speed
        self.target_fps = target_fps
        self.vcodec = vcodec
        self.quality = quality
        self.preset = preset
        self.include_audio = include_audio
        self.encoding_enabled = encoding_enabled
        self.sort_mode = sort_mode
        self.last_number_str = last_number
        self.end_number_str = end_number
        self._is_canceled = False
        self._process = None
        self._start_time = None
        self._error_lines = []
        self._temp_files_to_clean = []

    @staticmethod
    def extract_last_number(filename_no_ext: str) -> int:
        numbers = re.findall(r'\d+', filename_no_ext)
        if numbers:
            return int(numbers[-1])
        return -1

    def cancel(self):
        self._is_canceled = True
        # Сигналы сброса прогресса

        if self._process and self._process.poll() is None:
            # Пытаемся мягко завершить
            try:
                if self._process.stdin and not self._process.stdin.closed:
                    self._process.stdin.write('q\n')
                    self._process.stdin.flush()
            except Exception:
                pass
            # Ждём до 10 секунд
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Если не завершился, шлём более жёсткий сигнал
                try:
                    if sys.platform == 'win32':
                        self._process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        self._process.send_signal(signal.SIGINT)
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait()

    def run(self):

        self._start_time = time.time()
        self._last_eta_str = None

        try:
            # 1. Сбор файлов
            self.status.emit("Сбор видеофайлов...")
            raw_files = []
            first_ext = None
            extensions = {'.mp4', '.ts', '.mov', '.avi', '.mkv', '.lrf', '.osv'}

            # Подготовка чисел для фильтрации по номеру
            last_num = 0
            end_num = None
            sort_by_number = (self.sort_mode == 1)
            if sort_by_number and (self.last_number_str or self.end_number_str):
                try:
                    last_num = int(self.last_number_str) if self.last_number_str else 0
                except ValueError:
                    last_num = 0
                try:
                    end_num = int(self.end_number_str) if self.end_number_str else None
                except ValueError:
                    end_num = None

            for source in self.sources:
                if self._is_canceled:
                    self.finished.emit(False, "Отменено!")
                    return

                if os.path.isdir(source):
                    # Простой обход – сортировку сделаем позже
                    all_paths = list(glob.iglob(os.path.join(source, '**', '*'), recursive=True))
                    for full_path in all_paths:
                        if self._is_canceled:
                            self.finished.emit(False, "Отменено!")
                            return
                        if not os.path.isfile(full_path):
                            continue
                        ext = os.path.splitext(full_path)[1].lower()
                        if ext not in extensions:
                            continue
                        if first_ext is not None and ext != first_ext:
                            continue
                        full_path = os.path.normpath(full_path)
                        if full_path not in raw_files:
                            raw_files.append(full_path)
                            if first_ext is None:
                                first_ext = ext
                            self.progress_collect.emit(len(raw_files))

                elif os.path.isfile(source):
                    full_path = os.path.normpath(source)
                    ext = os.path.splitext(full_path)[1].lower()
                    if ext not in extensions:
                        continue
                    if first_ext is not None and ext != first_ext:
                        continue
                    if full_path not in raw_files:
                        raw_files.append(full_path)
                        if first_ext is None:
                            first_ext = ext
                        self.progress_collect.emit(len(raw_files))

            if raw_files:
                self.progress_collect.emit(len(raw_files))

            if self._is_canceled:
                self.finished.emit(False, "Отменено!")
                return

            if not raw_files:
                self.finished.emit(False, "Не найдено ни одного видеофайла.")
                return

            # --- Фильтрация и сортировка по номеру, если нужно ---
            if sort_by_number:
                def extract_num(path):
                    name = os.path.splitext(os.path.basename(path))[0]
                    num = self.extract_last_number(name)
                    return num if num != -1 else -1

                # Фильтруем по last_num и end_num, если заданы
                if last_num > 0 or end_num is not None:
                    filtered = []
                    for f in raw_files:
                        num = extract_num(f)
                        if last_num > 0 and num <= last_num:
                            continue
                        if end_num is not None and num > end_num:
                            continue
                        filtered.append(f)
                    raw_files = filtered
                    if not raw_files:
                        self.finished.emit(False, "После фильтрации по номеру не осталось файлов.")
                        return

                # Сортируем по возрастанию номера (или по имени для файлов без номера)
                raw_files.sort(key=lambda p: (extract_num(p), p))

            else:
                # Обычная сортировка по имени
                raw_files.sort()

            # === Предварительная проверка целостности и предложение CHKDSK ===
            broken_files = []

            # Используем однопоточную проверку для определения битых файлов
            for f in raw_files:
                if self._is_canceled:
                    self.finished.emit(False, "Отменено!")
                    return
                _, valid = get_video_info(f)
                if not valid:
                    broken_files.append(f)

            if broken_files:
                # Диалог запроса CHKDSK – показываем из рабочего потока,
                # но с флагом WindowStaysOnTopHint, чтобы он был заметен.
                msg = QMessageBox()
                msg.setWindowTitle("Обнаружены битые файлы")
                msg.setText(
                    f"Найдено {len(broken_files)} повреждённых файлов.\n"
                    "Возможна ошибка файловой системы.\n"
                    "Запустить проверку диска (CHKDSK)?"
                )
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.setWindowFlags(msg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
                yes_btn = msg.addButton("Да", QMessageBox.ButtonRole.YesRole)
                msg.exec()

                if msg.clickedButton() == yes_btn:
                    drives = set()
                    for p in broken_files:
                        drive = os.path.splitdrive(p)[0]
                        if drive:
                            drives.add(drive)
                    for drive in drives:
                        self.log.emit(f"Запуск CHKDSK {drive} /f ...", False)
                        try:
                            subprocess.run(
                                ['chkdsk', drive, '/f'],
                                check=False, timeout=300,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                            )
                        except Exception as e:
                            self.log.emit(f"Ошибка CHKDSK: {e}", True)

                    # Повторная проверка только тех файлов, что были битыми
                    still_broken = []
                    for f in broken_files:
                        if self._is_canceled:
                            self.finished.emit(False, "Отменено!")
                            return
                        _, valid2 = get_video_info(f)
                        if valid2:
                            self.log.emit(f"Файл исправлен после CHKDSK: {os.path.basename(f)}", False)
                        else:
                            still_broken.append(f)
                    broken_files = still_broken
                    if broken_files:
                        self.log.emit(f"После CHKDSK остались повреждёнными: {len(broken_files)} файлов.", True)
                else:
                    self.log.emit("Проверка диска отменена. Битые файлы будут пропущены.", False)

            # === Проверка, все ли файлы 360° MP4 ===
            all_360 = all(
                os.path.splitext(f)[1].lower() == '.mp4' and is_spherical_mp4(f)
                for f in raw_files
            )

            # Создаём выходную папку, если она не существует
            out_dir = os.path.dirname(self.output_file)
            if out_dir and not os.path.exists(out_dir):
                try:
                    os.makedirs(out_dir, exist_ok=True)
                    self.log.emit(f"Создана папка: {out_dir}", False)
                except Exception as e:
                    self.finished.emit(False, f"Не удалось создать папку {out_dir}: {e}")
                    return

            if all_360:
                # Режим 360° – объединение через MP4Box
                if self.encoding_enabled:
                    self.log.emit("Перекодировка 360° видео приведёт к потере сферических метаданных. "
                                  "Обработка будет выполнена без перекодировки.", True)
                    self.encoding_enabled = False

                # Проверка ускорения
                if self.speed != 1.0:
                    self.log.emit("Ускорение 360° видео в данный момент не поддерживается. "
                                  "Объединение будет выполнено без ускорения.", True)
                    self.speed = 1.0   # принудительно отключаем

                mp4box = get_mp4box_path()
                if not mp4box:
                    self.finished.emit(False, "MP4Box не найден. Установите GPAC для объединения 360° видео. https://gpac.io/downloads/gpac-nightly-builds")
                    return

                self.status.emit("Объединение 360° видео через MP4Box...")
                encode_start = time.time()

                total_size = sum(os.path.getsize(f) for f in raw_files)

                tmp_merged = self.output_file + ".tmp.mp4"
                cmd = [mp4box, '-add', raw_files[0]]
                for f in raw_files[1:]:
                    cmd += ['-cat', f]
                cmd += ['-new', tmp_merged]

                # Запускаем MP4Box без перенаправления stdout/stderr, чтобы избежать блокировки буферов
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )

                # Цикл ожидания с обновлением прогресса по размеру файла
                last_percent = 0.0
                while self._process.poll() is None:
                    if self._is_canceled:
                        self._process.terminate()
                        try:
                            self._process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self._process.kill()
                            self._process.wait()
                        break
                    if os.path.exists(tmp_merged):
                        current_size = os.path.getsize(tmp_merged)
                        percent = min(current_size / total_size * 100.0, 99.9) if total_size > 0 else 0.0
                    else:
                        percent = 0.0
                    if abs(percent - last_percent) >= 0.5:
                        self.progress.emit(percent)
                        last_percent = percent
                    time.sleep(0.2)

                # Обработка отмены
                if self._is_canceled:
                    if os.path.exists(tmp_merged):
                        os.unlink(tmp_merged)
                    self.finished.emit(False, "Отменено.")
                    return

                # Проверка ошибок MP4Box
                if self._process.returncode != 0:
                    self.finished.emit(False, f"Ошибка MP4Box (код {self._process.returncode}). "
                                              "Возможно, файлы повреждены или недоступны.")
                    if os.path.exists(tmp_merged):
                        os.unlink(tmp_merged)
                    return

                # Успех – заменяем выходной файл
                os.replace(tmp_merged, self.output_file)

                total_str = format_duration(time.time() - self._start_time)
                encode_str = format_duration(time.time() - encode_start)

                self.progress.emit(100.0)
                self.finished.emit(True, f"Обработка 360° видео завершена за {total_str} (объединение {encode_str}).")
                return

            # 3. Параллельная проверка, восстановление и подсчёт длительности
            self.status.emit("Проверка и восстановление файлов...")
            files = []
            total_src_duration = 0.0
            repaired_count = 0
            lock = threading.Lock()
            concurrent_ffprobes = threading.Semaphore(4)
            total_to_check = len(raw_files)
            completed = 0

            # Сохраняем исходный порядок для быстрой сортировки в конце
            original_order = {path: idx for idx, path in enumerate(raw_files)}

            # Создаём временную директорию в системной TEMP-папке
            temp_dir = tempfile.mkdtemp(prefix="svj_repaired_")
            self._temp_files_to_clean.append(temp_dir)   # будет удалена рекурсивно в finally

            def process_file(f):
                nonlocal repaired_count, total_src_duration, completed
                if self._is_canceled:
                    return None

                with concurrent_ffprobes:
                    if self._is_canceled:
                        return None
                    dur, valid = get_video_info(f)

                if valid:
                    with lock:
                        total_src_duration += dur
                        files.append(f)
                        completed += 1
                        self.progress_verify.emit(completed, total_to_check)
                    return f
                else:
                    self.log.emit(f"Битый файл обнаружен: {os.path.basename(f)}", True)
                    # Восстановленный файл создаём во временной папке
                    tmp_repaired = os.path.join(temp_dir, os.path.basename(f) + ".repaired.mp4")
                    with concurrent_ffprobes:
                        repair_success = repair_video(f, tmp_repaired)
                        if repair_success:
                            dur2, valid2 = get_video_info(tmp_repaired)
                        else:
                            valid2 = False
                    if valid2:
                        # Заменяем оригинальный путь восстановленным файлом (он останется во временной папке)
                        with lock:
                            total_src_duration += dur2
                            files.append(tmp_repaired)   # <-- используем временный файл
                            repaired_count += 1
                            completed += 1
                            self.progress_verify.emit(completed, total_to_check)
                        self.log.emit(f"Файл восстановлен: {os.path.basename(f)}", False)
                        return tmp_repaired
                    else:
                        self.log.emit(f"Не удалось восстановить: {os.path.basename(f)}", True)
                        if os.path.exists(tmp_repaired):
                            os.remove(tmp_repaired)
                        with lock:
                            completed += 1
                            self.progress_verify.emit(completed, total_to_check)
                        return None

            executor = ThreadPoolExecutor(max_workers=4)
            try:
                futures = {executor.submit(process_file, f): i for i, f in enumerate(raw_files)}
                for future in as_completed(futures):
                    if self._is_canceled:
                        break
                    try:
                        future.result()
                    except Exception:
                        pass
            finally:
                if not self._is_canceled:
                    executor.shutdown(wait=True)

            # После завершения проверки восстанавливаем обычный формат прогресс-бара
            self.progress_verify.emit(0, 0)   # сообщим GUI, что проверка закончилась

            if repaired_count > 0:
                self.log.emit(f"Восстановлено файлов: {repaired_count}", False)

            if not files:
                self.finished.emit(False, "Не найдено ни одного читаемого видеофайла.")
                return

            # Быстрое восстановление порядка файлов
            files.sort(key=lambda p: original_order.get(p, len(raw_files)))

            if self.encoding_enabled or self.speed != 1.0:
                effective_duration = total_src_duration / self.speed if self.speed > 0 else total_src_duration
            else:
                effective_duration = total_src_duration

            if self.encoding_enabled:
                self.log.emit(
                    f"Общая длительность исходных: {format_duration(total_src_duration)}, "
                    f"после ускорения: {format_duration(effective_duration)}", False)
            else:
                if self.speed != 1.0:
                    self.log.emit(
                        f"Общая длительность исходных: {format_duration(total_src_duration)}, "
                        f"после ускорения: {format_duration(effective_duration)}", False)
                else:
                    self.log.emit(
                        f"Общая длительность исходных: {format_duration(total_src_duration)}", False)

            if self._is_canceled:
                self.finished.emit(False, "Отменено!")
                return
            self.status.emit("Подготовка временного списка...")
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as list_file:
                list_path = list_file.name
                for f in files:
                    safe_path = f.replace("'", r"'\''")
                    list_file.write(f"file '{safe_path}'\n")

            try:
                self.status.emit("Запуск кодирования...")
                args = get_args_for_ffmpeg(
                    list_path, self.output_file, self.speed, self.target_fps,
                    self.vcodec, self.quality, self.preset, self.include_audio, self.encoding_enabled
                )
                if not args or os.path.basename(args[0]) != 'ffmpeg':
                    args.insert(0, 'ffmpeg')

                encode_start = time.time()
                self._process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True, encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )

                time_pattern = re.compile(r'time=(\d+):(\d+):(\d+)\.(\d+)')
                last_percent = -1.0
                speed_samples = []           # (real_time, video_time)
                last_real_time = None
                last_video_time = None
                prev_video_time = -1.0       # для монотонности

                for line in self._process.stderr:
                    if self._is_canceled:
                        break
                    match = time_pattern.search(line)
                    if match and effective_duration > 0:
                        h, m, s, frac = match.groups()
                        raw_video_time = int(h)*3600 + int(m)*60 + int(s) + int(frac)/100.0
                        # Пропускаем, если время уменьшилось (немонотонность)
                        if raw_video_time < prev_video_time:
                            continue
                        prev_video_time = raw_video_time
                        current_video_time = raw_video_time
                        percent = min(current_video_time / effective_duration * 100, 100.0)

                        now_real = time.time()
                        if last_real_time is not None and last_video_time is not None:
                            dt_real = now_real - last_real_time
                            dt_video = current_video_time - last_video_time
                            if dt_real > 0 and dt_video > 0:
                                speed_samples.append((now_real, current_video_time))
                                # Окно анализа — 60 секунд
                                while speed_samples and (now_real - speed_samples[0][0]) > 60:
                                    speed_samples.pop(0)
                        last_real_time = now_real
                        last_video_time = current_video_time

                        # Обновляем прогресс и ETA только при изменении процента
                        if abs(percent - last_percent) >= 0.1 and percent >= last_percent:
                            last_percent = percent
                            self.progress.emit(percent)

                            # Расчёт ETA по средней скорости за окно
                            if len(speed_samples) >= 2:
                                first_real = speed_samples[0][0]
                                first_video = speed_samples[0][1]
                                last_real = speed_samples[-1][0]
                                last_video = speed_samples[-1][1]
                                window_real = last_real - first_real
                                window_video = last_video - first_video
                                if window_real > 0 and window_video > 0:
                                    avg_speed = window_video / window_real
                                    remaining_video = effective_duration - current_video_time
                                    remaining_real = remaining_video / avg_speed if avg_speed > 0 else 0.0
                                    if remaining_real < 0:
                                        remaining_real = 0.0
                                    elapsed = now_real - encode_start
                                    self._last_eta_str = (
                                        f"Осталось: {format_duration(remaining_real)} "
                                        f"(прошло {format_duration(elapsed)})"
                                    )
                                    self.eta_update.emit(self._last_eta_str)
                                else:
                                    self.eta_update.emit("Осталось: …")
                            else:
                                if hasattr(self, '_last_eta_str') and self._last_eta_str:
                                    self.eta_update.emit(self._last_eta_str)
                                else:
                                    elapsed = now_real - encode_start
                                    self.eta_update.emit(f"Осталось: ....  (прошло {format_duration(elapsed)})")

                    if 'Error' in line or 'Invalid' in line:
                        self._error_lines.append(line.strip())
                        self.log.emit(line.strip(), True)

                try:
                    if self._is_canceled:
                        self._process.wait(timeout=10)
                    else:
                        self._process.wait()
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait()

                encode_time = time.time() - encode_start
                elapsed_total = time.time() - self._start_time
                encode_str = format_duration(encode_time)
                total_str = format_duration(elapsed_total)
                self.eta_update.emit("")

                if self._is_canceled:
                    self.finished.emit(False, f"Отменено после {total_str} (кодирование {encode_str}).")
                elif self._process.returncode == 0:
                    self.progress.emit(100)
                    if self.sort_mode == 1 and files:
                        max_num = max(self.extract_last_number(os.path.splitext(os.path.basename(f))[0]) for f in files)
                        new_num_str = str(max_num).zfill(6)
                        self.update_last_number.emit(new_num_str)
                        self.update_last_number_file.emit(new_num_str)
                    self.finished.emit(True, f"Обработка завершена за {total_str} (кодирование {encode_str}).")
                else:
                    err_detail = "\n".join(self._error_lines[-3:]) if self._error_lines else "см. лог"
                    self.finished.emit(False, f"Ошибка FFmpeg (код {self._process.returncode})\n{err_detail}")

            except FileNotFoundError as fnf_error:
                self.finished.emit(False, f"FFmpeg не найден: {fnf_error}")
            finally:
                if os.path.exists(list_path):
                    os.unlink(list_path)
                # Удаляем временную папку с восстановленными файлами
                for temp_item in self._temp_files_to_clean:
                    try:
                        if os.path.isdir(temp_item):
                            shutil.rmtree(temp_item, ignore_errors=True)
                        elif os.path.isfile(temp_item):
                            os.remove(temp_item)
                    except Exception:
                        pass
                self._temp_files_to_clean.clear()

        except Exception as e:
            self.finished.emit(False, f"Ошибка: {str(e)}")
            # Даже при исключении пытаемся подчистить временные файлы
            for temp_item in self._temp_files_to_clean:
                try:
                    if os.path.isdir(temp_item):
                        shutil.rmtree(temp_item, ignore_errors=True)
                    elif os.path.isfile(temp_item):
                        os.remove(temp_item)
                except Exception:
                    pass

def get_args_for_ffmpeg(list_path, output_file, speed, target_fps, vcodec, quality, preset, include_audio=True, encoding_enabled=True):
    if not encoding_enabled:
        args = ['ffmpeg']
        if speed != 1.0:
            args += ['-itsscale', str(1.0 / speed)]
        args += ['-f', 'concat', '-safe', '0', '-i', list_path]
        args += ['-c:v', 'copy']
        if include_audio:
            if speed != 1.0:
                # При ускорении аудио перекодируем в AAC
                args += ['-c:a', 'aac']
                atempo_chain = []
                s = speed
                while s > 2.0:
                    atempo_chain.append('atempo=2.0')
                    s /= 2.0
                while s < 0.5:
                    atempo_chain.append('atempo=0.5')
                    s /= 0.5
                if s != 1.0:
                    atempo_chain.append(f'atempo={s}')
                filter_audio = ','.join(atempo_chain)
                args += ['-filter:a', filter_audio]

            else:
                # Без ускорения просто копируем аудио
                args += ['-c:a', 'copy']
        else:
            args += ['-an']

        args += ['-y', output_file]
        return args

    # Режим с перекодировкой
    all_videos = ffmpeg.input(list_path, format='concat', safe=0)
    video = all_videos.video

    fast_video = video.filter('setpts', f'{1/speed}*PTS')
    fast_audio = None
    if include_audio:
        audio = all_videos.audio
        fast_audio = audio
        s = speed
        while s > 2.0:
            fast_audio = fast_audio.filter('atempo', 2.0)
            s /= 2.0
        while s < 0.5:
            fast_audio = fast_audio.filter('atempo', 0.5)
            s /= 0.5
        if s != 1.0:
            fast_audio = fast_audio.filter('atempo', s)

    out_params = {
        'vcodec': vcodec,
        'r': str(target_fps),
    }
    if include_audio:
        out_params['acodec'] = 'aac'

    if vcodec == 'av1_nvenc':
        out_params['rc'] = 'vbr'
        out_params['cq'] = quality
        out_params['b:v'] = '0'
    else:
        out_params['rc'] = 'vbr'
        out_params['cq'] = quality
        out_params['preset'] = preset

    if include_audio:
        out = ffmpeg.output(fast_video, fast_audio, output_file, **out_params)
    else:
        out = ffmpeg.output(fast_video, output_file, **out_params)

    out = out.overwrite_output()
    return out.get_args()


# ----------------- Кастомные спинбоксы -----------------
class SpinBox(QDoubleSpinBox):
    def textFromValue(self, value):
        if value == int(value):
            return str(int(value))
        else:
            return f"{value:.2f}".rstrip('0').rstrip('.')

    def validate(self, text: str, pos: int):
        locale = self.locale()
        decimal_point = locale.decimalPoint()
        text = text.replace('.', decimal_point)
        return super().validate(text, pos)

    def valueFromText(self, text: str):
        text = text.replace('.', self.locale().decimalPoint())
        return super().valueFromText(text)


# ----------------- Кастомная строка для выходного файла с Drag&Drop -----------------
class DropLineEdit(QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.textChanged.connect(self._remove_quotes)

    def _remove_quotes(self, text):
        cleaned = text.replace('"', '').replace("'", '')
        if cleaned != text:
            cursor_pos = self.cursorPosition()
            self.blockSignals(True)
            self.setText(cleaned)
            self.blockSignals(False)
            self.setCursorPosition(min(cursor_pos, len(cleaned)))

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0].toLocalFile()
            url = os.path.normpath(url)
            self.setText(url)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


# ----------------- Кастомный список с D&D и Delete -----------------
class FolderListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                path = os.path.normpath(path)
                if os.path.isdir(path) and not self._path_exists(path):
                    self.addItem(path)
                    if self.count() == 1:
                        main_win = self._get_main_window()
                        if main_win:
                            main_win.try_read_last_number_from_folder()
                elif os.path.isfile(path):
                    ext = os.path.splitext(path)[1].lower()
                    if ext in ('.mp4', '.ts', '.mov', '.avi', '.mkv', '.lrf', '.osv') and not self._path_exists(path):
                        self.addItem(path)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Delete:
            for item in self.selectedItems():
                self.takeItem(self.row(item))
        else:
            super().keyPressEvent(event)

    def _path_exists(self, path):
        for i in range(self.count()):
            if self.item(i).text() == path:
                return True
        return False

    def _get_main_window(self):
        p = self.parent()
        while p is not None:
            if isinstance(p, MainWindow):
                return p
            p = p.parent()
        return None


# ----------------- Главное окно -----------------
class MainWindow(QMainWindow):
    app_title = title
    app_ver = ver

    def __init__(self):
        super().__init__()

        self.setWindowTitle(f"{self.app_title} {self.app_ver}")
        self.settings = QSettings("VideoSpeedUp", "Config")
        self._processing = False

        self._last_progress = 0
        self._show_eta = False
        self._ignore_worker_signals = False


        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        font = QFont()
        font.setPointSize(10)
        QApplication.instance().setFont(font)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        log_font = QFont("Courier New")
        log_font.setPointSize(9)
        self.log_text.setFont(log_font)
        # Разрешаем очистку лога клавишей Delete
        self.log_text.installEventFilter(self)

        self.restore_settings()

        # Проверка кодеков
        self.available_codecs = []
        self.available_codec_ids = []
        for codec_id, display in [
            ("hevc_nvenc", "HEVC (H.265)"),
            ("h264_nvenc", "H.264"),
            ("av1_nvenc", "AV1")
        ]:
            if check_encoder_support(codec_id):
                self.available_codecs.append(display)
                self.available_codec_ids.append(codec_id)

        if not self.available_codecs:
            self._show_auto_close_message("Ошибка", "Нет аппаратных энкодеров NVENC.",
                                         QMessageBox.Icon.Critical, is_error=True)
            sys.exit(1)

        if self.saved_codec_id not in self.available_codec_ids:
            self.saved_codec_id = self.available_codec_ids[0]

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(4)

        # --- Группа параметров ---
        settings_group = QGroupBox("Параметры")
        settings_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        form_layout = QVBoxLayout(settings_group)

        # Исходные файлы и папки
        form_layout.addWidget(QLabel("Исходные файлы и папки:"))
        self.folder_list = FolderListWidget()
        self.folder_list.setMaximumHeight(150)
        form_layout.addWidget(self.folder_list)

        self._empty_hint_label = QLabel("Перетащите файлы и папки в область выше")
        self._empty_hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint_label.setStyleSheet("color: gray;")
        form_layout.addWidget(self._empty_hint_label)
        self._empty_hint_label.setVisible(self.folder_list.count() == 0)
        self.folder_list.model().rowsInserted.connect(lambda: self._empty_hint_label.setVisible(False))
        self.folder_list.model().rowsRemoved.connect(lambda: self._empty_hint_label.setVisible(self.folder_list.count() == 0))

        btn_layout = QHBoxLayout()
        add_folder_btn = QPushButton("Добавить папку")
        add_folder_btn.clicked.connect(self.add_folders)
        btn_layout.addWidget(add_folder_btn)
        add_file_btn = QPushButton("Добавить файлы")
        add_file_btn.clicked.connect(self.add_files)
        btn_layout.addWidget(add_file_btn)
        clear_btn = QPushButton("Очистить список")
        clear_btn.clicked.connect(self.clear_sources)
        btn_layout.addWidget(clear_btn)
        form_layout.addLayout(btn_layout)

        # Выходной файл
        out_layout = QHBoxLayout()
        out_layout.addWidget(QLabel("Выходной файл:"))
        self.out_edit = DropLineEdit()
        self.out_edit.setPlaceholderText("Куда сохранить результат...")
        if self.saved_output:
            self.out_edit.setText(self.saved_output)
        out_layout.addWidget(self.out_edit)
        self.out_btn = QPushButton("Обзор...")
        self.out_btn.clicked.connect(self.select_output)
        out_layout.addWidget(self.out_btn)
        form_layout.addLayout(out_layout)

        # --- Блок сортировки ---
        sort_group = QGroupBox("Сортировка")
        sort_layout = QVBoxLayout(sort_group)

        self.sort_btn_group = QButtonGroup()
        self.sort_asc_radio = QRadioButton("По возрастанию")
        self.sort_num_radio = QRadioButton("Порядковый номер с")
        self.sort_btn_group.addButton(self.sort_asc_radio, 0)
        self.sort_btn_group.addButton(self.sort_num_radio, 1)

        sort_layout.addWidget(self.sort_asc_radio)

        num_line_layout = QHBoxLayout()
        num_line_layout.addWidget(self.sort_num_radio)
        self.last_number_edit = QLineEdit()
        self.last_number_edit.setPlaceholderText("Введите №")
        self.last_number_edit.setFixedWidth(80)
        self.last_number_edit.setToolTip("Последний обработанный номер")
        if self.saved_last_number:
            self.last_number_edit.setText(self.saved_last_number)
        num_line_layout.addWidget(self.last_number_edit)

        self.label_po = QLabel("по")
        num_line_layout.addWidget(self.label_po)

        self.end_number_edit = QLineEdit()
        self.end_number_edit.setPlaceholderText("Введите №")
        self.end_number_edit.setFixedWidth(80)
        self.end_number_edit.setToolTip("Конечный обрабатываемый номер")
        if self.saved_end_number:
            self.end_number_edit.setText(self.saved_end_number)
        num_line_layout.addWidget(self.end_number_edit)

        self.dup_to_folder_check = QCheckBox("Дублировать в папку")
        self.dup_to_folder_check.setChecked(self.saved_dup_to_folder)
        self.dup_to_folder_check.setToolTip("Дублировать в исходную папку")
        num_line_layout.addWidget(self.dup_to_folder_check)

        num_line_layout.addStretch()
        sort_layout.addLayout(num_line_layout)

        if self.saved_sort_mode == 1:
            self.sort_num_radio.setChecked(True)
        else:
            self.sort_asc_radio.setChecked(True)

        self.toggle_last_number_field(self.saved_sort_mode)
        self.sort_btn_group.idToggled.connect(self.toggle_last_number_field)

        form_layout.addWidget(sort_group)

        for path in self.saved_sources:
            path = os.path.normpath(path)
            if os.path.exists(path):
                self.folder_list.addItem(path)
        self._empty_hint_label.setVisible(self.folder_list.count() == 0)

        self.try_read_last_number_from_folder()

        # --- Строка: Ускорение, Целевой FPS, Звук ---
        common_layout = QHBoxLayout()
        common_layout.addWidget(QLabel("Ускорение (раз):"))
        self.speed_spin = SpinBox()
        self.speed_spin.setRange(0.1, 1000.0)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setValue(self.saved_speed)
        self.speed_spin.setSingleStep(1.0)
        common_layout.addWidget(self.speed_spin)

        common_layout.addWidget(QLabel("Целевой FPS:"))
        self.fps_spin = SpinBox()
        self.fps_spin.setRange(1.0, 240.0)
        self.fps_spin.setDecimals(2)
        self.fps_spin.setValue(self.saved_fps)
        self.fps_spin.setSingleStep(1.0)
        common_layout.addWidget(self.fps_spin)

        self.audio_check = QCheckBox("Звук в видео")
        self.audio_check.setChecked(self.saved_include_audio)
        common_layout.addWidget(self.audio_check)
        common_layout.addStretch()
        form_layout.addLayout(common_layout)

        # --- Блок «Кодировка» ---
        self.encoding_group = QGroupBox("Кодировка")
        self.encoding_group.setCheckable(True)
        self.encoding_group.setChecked(self.saved_encoding_enabled)
        self.encoding_group.toggled.connect(self.on_encoding_toggled)
        encoding_layout = QVBoxLayout(self.encoding_group)

        codec_layout = QHBoxLayout()
        codec_layout.addWidget(QLabel("Кодек:"))
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(self.available_codecs)
        saved_idx = self.available_codec_ids.index(self.saved_codec_id)
        self.codec_combo.setCurrentIndex(saved_idx)
        self.codec_combo.setMinimumWidth(105)
        codec_layout.addWidget(self.codec_combo)

        codec_layout.addWidget(QLabel("Качество:"))
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 51)
        self.quality_spin.setValue(self.saved_quality)
        self.quality_spin.setFixedWidth(40)
        codec_layout.addWidget(self.quality_spin)

        codec_layout.addWidget(QLabel("Пресет:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["p1", "p2", "p3", "p4", "p5", "p6", "p7"])
        self.preset_combo.setCurrentText(self.saved_preset)
        self.preset_combo.setFixedWidth(45)
        codec_layout.addWidget(self.preset_combo)
        codec_layout.addStretch()
        encoding_layout.addLayout(codec_layout)

        self.on_encoding_toggled(self.saved_encoding_enabled)
        form_layout.addWidget(self.encoding_group)

        self.fps_spin.setEnabled(self.encoding_group.isChecked())
        self.encoding_group.toggled.connect(lambda enabled: self.fps_spin.setEnabled(enabled))

        main_layout.addWidget(settings_group)

        # Прогресс и ETA
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("")
        self.progress_bar.setValue(0)
        self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.progress_bar)

        self.action_btn = QPushButton("Запустить")
        self.action_btn.clicked.connect(self.toggle_action)
        main_layout.addWidget(self.action_btn)

        main_layout.addWidget(self.log_text)

        self._worker = None
        self._thread = None

    def eventFilter(self, source, event):
        # Очистка только выделенного текста в логе по Delete
        if source is self.log_text and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Delete:
                cursor = self.log_text.textCursor()
                if cursor.hasSelection():
                    cursor.removeSelectedText()
                    self.log_text.setTextCursor(cursor)
                return True
        return super().eventFilter(source, event)

    @staticmethod
    def sanitize_filename(filename):
        forbidden = r'/:*?"<>|'
        for ch in forbidden:
            filename = filename.replace(ch, '')
        return filename

    def toggle_last_number_field(self, mode_id):
        self.last_number_edit.setEnabled(mode_id == 1)
        self.end_number_edit.setEnabled(mode_id == 1)
        self.label_po.setEnabled(mode_id == 1)
        self.dup_to_folder_check.setEnabled(mode_id == 1)

    def try_read_last_number_from_folder(self):
        if not self.dup_to_folder_check.isChecked() or not self.sort_num_radio.isChecked():
            return
        if self.folder_list.count() == 0:
            return
        first_path = self.folder_list.item(0).text()
        if os.path.isdir(first_path):
            last_number_file = os.path.join(first_path, "LastNumber.txt")
            if os.path.exists(last_number_file):
                try:
                    with open(last_number_file, 'r') as f:
                        num = f.read().strip()
                        if num and num != self.last_number_edit.text():
                            self.last_number_edit.setText(num)
                            self.append_log(f"Прочитан последний номер из {last_number_file}: {num}", False)
                except Exception as e:
                    self.append_log(f"Ошибка чтения LastNumber.txt: {str(e)}", True)

    def save_last_number_to_folder(self, number_str):
        if not self.dup_to_folder_check.isChecked() or not self.sort_num_radio.isChecked():
            return
        if self.folder_list.count() == 0:
            return
        first_path = self.folder_list.item(0).text()
        if os.path.isdir(first_path):
            last_number_file = os.path.join(first_path, "LastNumber.txt")
            try:
                with open(last_number_file, 'w') as f:
                    f.write(number_str)
                self.append_log(f"Номер {number_str} записан в {last_number_file}", False)
            except Exception as e:
                self.append_log(f"Ошибка записи LastNumber.txt: {str(e)}", True)

    def clear_sources(self):
        self.folder_list.clear()

    def on_encoding_toggled(self, enabled):
        for child in self.encoding_group.findChildren(QWidget):
            if child != self.encoding_group:
                child.setEnabled(enabled)
        self.fps_spin.setEnabled(enabled)

    def add_folders(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку с видео")
        if folder:
            folder = os.path.normpath(folder)
            if os.path.isdir(folder) and not self.folder_list._path_exists(folder):
                self.folder_list.addItem(folder)
                if self.folder_list.count() == 1:
                    self.try_read_last_number_from_folder()

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите видеофайлы", "",
            "Видео (*.mp4 *.ts *.mov *.avi *.mkv *.lrf *.osv);;Все файлы (*.*)")
        for f in files:
            f = os.path.normpath(f)
            if os.path.isfile(f) and not self.folder_list._path_exists(f):
                self.folder_list.addItem(f)

    def select_output(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить как", "", "MP4 файлы (*.mp4);;Все файлы (*)")
        if file_path:
            file_path = os.path.normpath(file_path)
            directory, filename = os.path.split(file_path)
            safe_filename = self.sanitize_filename(filename)
            safe_path = os.path.join(directory, safe_filename)
            self.out_edit.setText(safe_path)

    def toggle_action(self):
        if self.action_btn.text() == "Запустить":
            self.start_processing()
        else:
            self.cancel_processing()

    def start_processing(self):

        self._ignore_worker_signals = False    # разрешаем сигналы для нового воркера

        if self._processing:
            return
        sources = [self.folder_list.item(i).text() for i in range(self.folder_list.count())]

        if not sources:
            self._show_auto_close_message("Ошибка", "Добавьте хотя бы один исходный файл или папку.",
                                         QMessageBox.Icon.Warning, is_error=True)
            return
        output_file = self.out_edit.text().strip().strip('"\'')
        if not output_file:
            self._show_auto_close_message("Ошибка", "Укажите путь для выходного файла.",
                                         QMessageBox.Icon.Warning, is_error=True)
            return

        # Проверка существования выходного файла
        if os.path.exists(output_file):
            msg = QMessageBox(self)
            msg.setWindowTitle(f"{self.app_title} {self.app_ver}")
            msg.setText(f"Файл \"{output_file}\" уже существует.\nПерезаписать его?")
            msg.setIcon(QMessageBox.Icon.Question)
            yes_btn = msg.addButton(QMessageBox.StandardButton.Yes)
            no_btn = msg.addButton(QMessageBox.StandardButton.No)
            yes_btn.setText("Да")
            no_btn.setText("Нет")
            msg.exec()
            if msg.clickedButton() != yes_btn:
                return

        encoding_enabled = self.encoding_group.isChecked()
        speed = self.speed_spin.value()
        if speed <= 0:
            self._show_auto_close_message("Ошибка", "Скорость должна быть больше 0.",
                                         QMessageBox.Icon.Warning, is_error=True)
            return
        target_fps = self.fps_spin.value() if encoding_enabled else 0.0

        sort_mode = self.sort_btn_group.checkedId()
        last_number = self.last_number_edit.text().strip() if sort_mode == 1 else ""
        end_number = self.end_number_edit.text().strip() if sort_mode == 1 else ""

        if sort_mode == 1 and last_number and end_number:
            try:
                ln = int(last_number)
                en = int(end_number)
                if en <= ln:
                    self._show_auto_close_message("Ошибка",
                        "Конечный номер должен быть больше последнего обработанного.",
                        QMessageBox.Icon.Warning, is_error=True)
                    return
            except ValueError:
                pass

        if len(sources) == 1:
            source_log = sources[0]
        else:
            source_log = f"{sources[0]}… и ещё {len(sources)-1}"

        self.append_log("-----------------------------------------------------", False, show_time=False)
        self.append_log(f"Источники: {source_log}", False, show_time=False)
        self.append_log(f"Выходной файл: {output_file}", False, show_time=False)
        self.append_log(f"Ускорение: {speed:.2f}x", False, show_time=False)

        if encoding_enabled:
            self.append_log(f"Целевой FPS: {target_fps:.2f}", False, show_time=False)
            self.append_log(f"Кодек: {self.codec_combo.currentText()}, Качество: {self.quality_spin.value()}, "
                            f"Пресет: {self.preset_combo.currentText()}", False, show_time=False)
        else:
            self.append_log("Режим без перекодирования видео", False, show_time=False)

        self.append_log(f"Звук: {'включён' if self.audio_check.isChecked() else 'выключен'}", False, show_time=False)
        self.append_log(f"Сортировка: {'порядковый номер' if sort_mode == 1 else 'по возрастанию'}", False, show_time=False)
        if sort_mode == 1:
            self.append_log(f"Последний обработанный номер: {last_number}", False, show_time=False)
            if end_number:
                self.append_log(f"Конечный обрабатываемый номер: {end_number}", False, show_time=False)
        self.append_log("-----------------------------------------------------", False, show_time=False)

        self._processing = True
        self.action_btn.setText("Отмена")
        self.action_btn.setEnabled(True)
        self.progress_bar.setFormat("")
        self.progress_bar.setValue(0)

        idx = self.codec_combo.currentIndex()
        vcodec = self.available_codec_ids[idx] if self.available_codec_ids else 'hevc_nvenc'
        quality = self.quality_spin.value()
        preset = self.preset_combo.currentText()
        include_audio = self.audio_check.isChecked()

        self._thread = QThread()
        self._worker = EncodeWorker(sources, output_file, speed, target_fps,
                                   vcodec, quality, preset, include_audio, encoding_enabled,
                                   sort_mode, last_number, end_number)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress_collect.connect(self.on_progress_collect)
        self._worker.progress_verify.connect(self.on_progress_verify)
        self._worker.progress.connect(self.on_progress_update)
        self._worker.eta_update.connect(self.on_eta_update)
        self._worker.status.connect(lambda msg: self.log_text.append(f"{time_stamp()} {msg}"))
        self._worker.log.connect(self.append_log)
        self._worker.finished.connect(self.on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.update_last_number.connect(self.on_last_number_updated)
        self._worker.update_last_number_file.connect(self.save_last_number_to_folder)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def on_progress_collect(self, count):
        if self._ignore_worker_signals:
            return
        self.progress_bar.setFormat(f"Найдено файлов: {count}")
        self.progress_bar.setValue(0)   # можно заполнять полосу, но достаточно текста

    def on_progress_update(self, value: float):
        if self._ignore_worker_signals:
            return
        self._last_progress = f"{value:.1f}"
        if not self._show_eta:
            self.progress_bar.setFormat(f"{value:.1f}%")
        self.progress_bar.setValue(int(value))

    def on_eta_update(self, text):
        if self._ignore_worker_signals:
            return
        if text:
            self._show_eta = True
            self.progress_bar.setFormat(f"{self._last_progress}% {text}")
        else:
            self._show_eta = False
            self.progress_bar.setFormat("")

    def on_progress_verify(self, completed, total):
        if self._ignore_worker_signals:
            return
        self._show_eta = False
        if total > 0:
            percent = int(completed / total * 100)
            self.progress_bar.setValue(percent)
            self.progress_bar.setFormat(f"{completed}/{total}")

        else:
            self.progress_bar.setFormat("")
            self.progress_bar.setValue(0)

    def on_last_number_updated(self, new_number):
        self.last_number_edit.setText(new_number)

    def cancel_processing(self):
        if self._worker:
            self._ignore_worker_signals = True   # блокируем сигналы от старого воркера

            self._worker.cancel()
            self.action_btn.setEnabled(False)

    def append_log(self, msg, is_error, show_time=True):
        self.log_text.setTextColor(QColor("red") if is_error else QColor("black"))
        if show_time:
            self.log_text.append(f"{time_stamp()} {msg}")
        else:
            self.log_text.append(msg)
        if is_error:
            self.log_text.setTextColor(QColor("black"))

    def on_finished(self, success, msg):
        self._ignore_worker_signals = True   # блокируем сигналы от старого воркера

        self._processing = False
        self.action_btn.setText("Запустить")
        self.action_btn.setEnabled(True)
        self.progress_bar.setFormat("")
        self.progress_bar.setValue(0)

        if success:
            self._show_auto_close_message("Успех!", msg, QMessageBox.Icon.Information, is_error=False)
        else:
            self._show_auto_close_message("Предупреждение!", msg, QMessageBox.Icon.Warning, is_error=True)

    def _show_auto_close_message(self, title, text, icon, is_error=False):
        self.append_log(f"Сообщение: {text}", is_error)
        msg = QMessageBox(self)
        msg.setWindowTitle(f"{self.app_title} {self.app_ver}")
        msg.setText(text)
        msg.setIcon(icon)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        QTimer.singleShot(4000, msg.close)
        msg.exec()

    def restore_settings(self):
        self.saved_sources = self.settings.value("sources", [])
        self.saved_output = self.settings.value("output_file", "")
        self.saved_encoding_enabled = bool(int(self.settings.value("encoding_enabled", 1)))
        self.saved_speed = float(self.settings.value("speed", 30.0))
        self.saved_fps = float(self.settings.value("fps", 60.0))
        self.saved_codec_id = self.settings.value("codec_id", "hevc_nvenc")
        self.saved_quality = int(self.settings.value("quality", 35))
        self.saved_preset = self.settings.value("preset", "p4")
        self.saved_include_audio = bool(int(self.settings.value("include_audio", 1)))
        self.saved_sort_mode = int(self.settings.value("sort_mode", 0))
        self.saved_last_number = self.settings.value("last_number", "")
        self.saved_end_number = self.settings.value("end_number", "")
        self.saved_dup_to_folder = bool(int(self.settings.value("dup_to_folder", 0)))
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(820, 820)

    def closeEvent(self, event):
        if self._worker and self._processing:
            self._worker.cancel()
            if self._worker._process and self._worker._process.poll() is None:
                try:
                    self._worker._process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._worker._process.kill()
                    self._worker._process.wait()
            if self._thread and self._thread.isRunning():
                self._thread.quit()
                self._thread.wait(3000)
        self.settings.setValue("geometry", self.saveGeometry())
        sources = [self.folder_list.item(i).text() for i in range(self.folder_list.count())]
        self.settings.setValue("sources", sources)
        self.settings.setValue("output_file", self.out_edit.text().strip())
        self.settings.setValue("encoding_enabled", int(self.encoding_group.isChecked()))
        self.settings.setValue("speed", self.speed_spin.value())
        self.settings.setValue("fps", self.fps_spin.value())
        idx = self.codec_combo.currentIndex()
        if 0 <= idx < len(self.available_codec_ids):
            self.settings.setValue("codec_id", self.available_codec_ids[idx])
        self.settings.setValue("quality", self.quality_spin.value())
        self.settings.setValue("preset", self.preset_combo.currentText())
        self.settings.setValue("include_audio", int(self.audio_check.isChecked()))
        self.settings.setValue("sort_mode", self.sort_btn_group.checkedId())
        self.settings.setValue("last_number", self.last_number_edit.text().strip())
        self.settings.setValue("end_number", self.end_number_edit.text().strip())
        self.settings.setValue("dup_to_folder", int(self.dup_to_folder_check.isChecked()))
        super().closeEvent(event)

# ---------- Очистка старого .old файла при запуске ----------
def cleanup_old_backup():
    """Удаляет предыдущий .old файл, оставшийся после обновления."""
    old_path = sys.argv[0] + ".old"
    if not os.path.exists(old_path):
        return
    try:
        if send2trash:
            send2trash.send2trash(old_path)
        else:
            os.remove(old_path)
    except Exception:
        pass

# Колбэк перезапуска после успешного обновления
def do_restart(new_exe_path):
    """Запускает новый исполняемый файл и завершает текущий процесс."""
    bat = create_update_bat(new_exe_path)
    os.startfile(bat)
    QTimer.singleShot(500, lambda: QApplication.quit())

def create_update_bat(new_exe_path):
    """Создаёт bat-файл в TEMP, который запустит новую версию после выхода из текущей."""
    import tempfile
    bat_path = os.path.join(tempfile.gettempdir(), f"{title}_update.bat")
    with open(bat_path, "w") as f:
        f.write(f"""@echo off
timeout /t 2 /nobreak >nul
start "" "{new_exe_path}"
del "%~f0" & exit
""")
    return bat_path

if __name__ == '__main__':

    app = QApplication(sys.argv)

    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Critical)
        msg.setWindowTitle(f"{title} {ver}")
        msg.setText("FFmpeg или FFprobe не найдены.\n\n"
                     "Команда для Power Shell:\n"
                     "\"winget install ffmpeg\"\n\n"
                     "и перезапустите программу.")
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        QTimer.singleShot(10000, msg.close)
        msg.exec()
        sys.exit(1)

    window = MainWindow()
    window.show()

    _update_thread = start_update_check(
        title, ver, window,
        log_callback=window.append_log,   # логирование в главное окно
        on_restart=do_restart             # функция перезапуска
    )

    # Удаляем старый .old файл, оставшийся от предыдущего обновления
    cleanup_old_backup()

    app.exec()