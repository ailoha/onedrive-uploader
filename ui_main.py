# ui_main.py
import sys, os
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QListWidget, QFileDialog, QProgressBar, QTextEdit, QMessageBox, QSizePolicy
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor
from auth import list_accounts, acquire_token_interactive, remove_account
from uploader import upload_items
from Cocoa import NSOpenPanel

class UploadWorker(QThread):
    # progress: (uploaded_bytes, total_bytes, speed_bytes_per_sec, eta_seconds)
    progress = pyqtSignal(float, float, float, float)
    log = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(self, selected_paths, base_dir, account_home_id):
        super().__init__()
        self.selected_paths = selected_paths
        self.base_dir = base_dir
        self.account_home_id = account_home_id
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        """
        Worker thread main upload logic.
        Callback naming:
        - log_cb: emits log messages.
        - progress_cb: emits upload progress (bytes).
        """
        try:
            def log_cb(message):
                # Emit log message to main thread
                self.log.emit(message)
            def progress_cb(uploaded_bytes, total_bytes, speed_bytes_per_sec=None, eta_seconds=None):
                # Emit upload progress (all in bytes, seconds)
                self.progress.emit(
                    float(uploaded_bytes),
                    float(total_bytes),
                    float(speed_bytes_per_sec or 0),
                    float(eta_seconds or 0)
                )
            upload_items(
                self.selected_paths,
                base_dir=self.base_dir,
                account_home_id=self.account_home_id,
                progress_cb=progress_cb,
                log_cb=log_cb,
                should_stop=lambda: self._stop
            )
            self.finished.emit(True)
        except Exception:
            import traceback
            self.log.emit("ERROR:\n" + traceback.format_exc())
            self.finished.emit(False)

class MainWindow(QWidget):
    MAX_LOG_LINES = 1000
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OneDrive Uploader")
        self.resize(800, 600)
        self.layout = QVBoxLayout()
        # accounts area
        acct_layout = QHBoxLayout()
        self.acct_list = QListWidget()
        acct_btn_layout = QVBoxLayout()
        self.btn_add_acct = QPushButton("Add Account")
        self.btn_remove_acct = QPushButton("Remove Account")
        acct_btn_layout.addWidget(self.btn_add_acct)
        acct_btn_layout.addWidget(self.btn_remove_acct)
        acct_layout.addWidget(self.acct_list, 2)
        acct_layout.addLayout(acct_btn_layout, 1)
        # folder area
        folder_layout = QHBoxLayout()
        folder_btn_layout = QVBoxLayout()
        self.lbl_folder = QLabel("No folder selected")
        self.lbl_folder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        folder_layout.setContentsMargins(0,0,0,0)
        self.btn_choose = QPushButton("Choose Files / Folders")
        folder_btn_layout.addWidget(self.btn_choose)
        folder_layout.addWidget(self.lbl_folder, 2)
        folder_layout.addLayout(folder_btn_layout, 1)
        # controls
        ctrl_layout = QHBoxLayout()
        self.btn_start = QPushButton("Start Upload")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        ctrl_layout.addWidget(self.btn_start)
        ctrl_layout.addWidget(self.btn_stop)
        # progress & log
        self.progress = QProgressBar()
        self.lbl_status = QLabel("Speed: 0 MB/s | 0 / 0 MB | ETA: 0 s")
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        # assemble
        self.layout.addLayout(acct_layout, 1)
        self.layout.addLayout(folder_layout, 1)
        self.layout.addLayout(ctrl_layout)
        self.layout.addWidget(self.progress)
        self.layout.addWidget(self.lbl_status)
        self.layout.addWidget(self.log)
        self.setLayout(self.layout)
        # signals
        self.btn_add_acct.clicked.connect(self.add_account)
        self.btn_remove_acct.clicked.connect(self.remove_selected_account)
        self.btn_start.clicked.connect(self.start_upload)
        self.btn_stop.clicked.connect(self.stop_upload)
        self.worker = None
        self.folder = None
        self.remote_base = ""  # can modify to let user choose remote base
        self.refresh_accounts()
        # Connect the unified choose button
        self.btn_choose.clicked.connect(self.choose_files_and_folders)

    def _append_log(self, message: str):
        """Append formatted log efficiently and trim to last MAX_LOG_LINES lines."""
        # format any raw byte values first
        formatted = self._format_log_message(message)
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(formatted + "\n")
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()
        # trim lines if too many
        doc = self.log.document()
        if doc.blockCount() > self.MAX_LOG_LINES:
            # remove earliest extra lines
            extra = doc.blockCount() - self.MAX_LOG_LINES
            b = doc.begin()
            rm = 0
            cur = self.log.textCursor()
            cur.beginEditBlock()
            while extra > 0 and b.isValid():
                nxt = b.next()
                cur.setPosition(b.position())
                cur.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                cur.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)  # include newline
                cur.removeSelectedText()
                rm += 1
                extra -= 1
                b = nxt
            cur.endEditBlock()

    def refresh_accounts(self):
        self.acct_list.clear()
        accts = list_accounts()
        for a in accts:
            self.acct_list.addItem(f"{a.get('username')}  [{a.get('home_account_id')}]")
        if accts:
            self.acct_list.setCurrentRow(0)

    # Unified file/folder selection using NSOpenPanel
    def choose_files_and_folders(self):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(True)
        if not panel.runModal():
            return
        selected = [str(url.path()) for url in panel.URLs()]
        if not selected:
            return
        # --- Hidden file filtering ---
        filtered_files = []
        filtered_dirs = []
        for path in selected:
            name = os.path.basename(path)
            if name.startswith('.') or name.startswith('._') or name == 'Icon\r':
                continue
            if os.path.isdir(path):
                filtered_dirs.append(path)
            else:
                filtered_files.append(path)
        # --- Expand directories to file list (skip hidden) ---
        all_files = []
        for f in filtered_files:
            all_files.append(f)
        for d in filtered_dirs:
            for root, dirs, files in os.walk(d):
                for fname in files:
                    base = os.path.basename(fname)
                    if base.startswith('.') or base.startswith('._') or base == 'Icon\r':
                        continue
                    all_files.append(os.path.join(root, fname))
        self.selected_paths = all_files
        if not self.selected_paths:
            self.lbl_folder.setText("No files selected")
            return
        # --- Determine base_dir ---
        if len(selected) == 1:
            if os.path.isdir(selected[0]):
                self.base_dir = selected[0]
            else:
                self.base_dir = os.path.dirname(selected[0])
        else:
            # Use common parent path of all selected (original, not expanded)
            self.base_dir = os.path.dirname(os.path.commonpath(selected))

        file_count = len(filtered_files)
        dir_count = len(filtered_dirs)
        file_names = [os.path.basename(f) for f in filtered_files]
        dir_names = [os.path.basename(d) + "/" for d in filtered_dirs]

        if file_count > 0 and dir_count == 0:
            label_prefix = f"Files ({file_count}): "
            full_list = file_names
        elif dir_count > 0 and file_count == 0:
            label_prefix = f"Folders ({dir_count}): "
            full_list = dir_names
        else:
            label_prefix = f"Files ({file_count}) & Folders ({dir_count}): "
            full_list = file_names + dir_names

        self._full_list = full_list
        self._label_prefix = label_prefix
        self.update_folder_label()

        # --- Calculate total upload size (bytes) ---
        total_bytes = sum(os.path.getsize(p) for p in self.selected_paths if os.path.isfile(p))
        self.total_bytes = total_bytes

        # --- Log total size with dynamic unit for readability ---
        def format_size(bytes_value):
            # --- Dynamic unit conversion for logging ---
            if bytes_value < 1024:
                return f"{bytes_value:.1f} B"
            kb = bytes_value / 1024
            if kb < 1024:
                return f"{kb:.1f} KB"
            mb = kb / 1024
            if mb < 1024:
                return f"{mb:.1f} MB"
            gb = mb / 1024
            return f"{gb:.2f} GB"
        self._append_log(f"Total upload size: {format_size(total_bytes)}")

    def update_folder_label(self):
        full_list = getattr(self, '_full_list', [])
        prefix = getattr(self, '_label_prefix', '')
        available_width = self.lbl_folder.width()
        if available_width <= 0:
            available_width = self.lbl_folder.sizeHint().width()
        metrics = self.lbl_folder.fontMetrics()
        # build full text
        text = prefix + ", ".join(full_list)
        if metrics.horizontalAdvance(text) <= available_width:
            self.lbl_folder.setText(text)
            return
        # If too long, build truncated
        left = 0; right = len(full_list)
        # Reserve space for "..."
        ellipsis = "..."
        # Determine how many items from start and end
        left_items = []
        cur_text = prefix
        for item in full_list:
            test = prefix + ", ".join(left_items + [item]) + ", " + ellipsis
            if metrics.horizontalAdvance(test) < available_width / 2:
                left_items.append(item)
            else:
                break
        right_items = []
        for item in reversed(full_list[len(left_items):]):
            test_list = left_items + [ellipsis] + right_items
            test_text = prefix + ", ".join(test_list + [item])
            if metrics.horizontalAdvance(test_text) <= available_width:
                right_items.insert(0, item)
            else:
                break
        if not left_items and not right_items:
            # fallback show prefix only
            self.lbl_folder.setText(prefix.rstrip(": "))
            return
        display_list = left_items + [ellipsis] + right_items
        final = prefix + ", ".join(display_list)
        self.lbl_folder.setText(final)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_full_list'):
            self.update_folder_label()

    def add_account(self):
        try:
            token, acc = acquire_token_interactive()
            QMessageBox.information(self, "Signed In", f"Signed in as {acc.get('username')}")
            self._append_log(f"Signed in: {acc.get('username')}")
            self.refresh_accounts()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def remove_selected_account(self):
        row = self.acct_list.currentRow()
        if row < 0:
            return
        text = self.acct_list.currentItem().text()
        # parse home_account_id from display
        if "[" in text and "]" in text:
            hid = text.split("[")[-1].split("]")[0]
            ok = remove_account(hid)
            if ok:
                self._append_log(f"Removed account {hid}")
                self.refresh_accounts()
            else:
                QMessageBox.warning(self, "Remove", "Failed to remove account")

    def start_upload(self):
        if not hasattr(self, "selected_paths") or not self.selected_paths:
            QMessageBox.warning(self, "No selection", "Choose a file or folder first")
            return
        row = self.acct_list.currentRow()
        if row < 0:
            QMessageBox.warning(self, "No account", "Add and select an account")
            return
        # Thread lifecycle management: prevent multiple uploads
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Busy", "An upload task is still running.")
            return
        text = self.acct_list.currentItem().text()
        hid = text.split("[")[-1].split("]")[0]

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # 传 selected_paths 和 base_dir 给上传线程
        self.worker = UploadWorker(self.selected_paths, self.base_dir, account_home_id=hid)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self._append_log)
        self.worker.finished.connect(lambda ok: self.on_finished(ok))
        self.worker.start()
        self._append_log("Upload started")

    def stop_upload(self):
        if self.worker and self.worker.isRunning():
            # signal worker to stop gracefully
            self.worker.request_stop()
            self._append_log("Stopping... waiting for current chunk to finish")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_progress(self, uploaded, _ignored_total, speed=0, eta=0):
        QApplication.processEvents()
        # --- 进度计算 ---
        total = getattr(self, "total_bytes", _ignored_total) or 0
        if total > 0:
            pct = int(uploaded * 100 / total)
            if pct != self.progress.value():
                self.progress.setValue(pct)

            # --- 单位换算（动态单位显示） ---
            def format_size(bytes_value):
                # --- Dynamic unit conversion ---
                if bytes_value < 1024:
                    return f"{bytes_value:.1f} B"
                kb = bytes_value / 1024
                if kb < 1024:
                    return f"{kb:.1f} KB"
                mb = kb / 1024
                if mb < 1024:
                    return f"{mb:.1f} MB"
                gb = mb / 1024
                return f"{gb:.2f} GB"

            def format_time(seconds):
                seconds = int(seconds)
                if seconds < 60:
                    return f"{seconds} s"
                minutes = seconds // 60
                if minutes < 60:
                    return f"{minutes} min {seconds % 60} s"
                hours = minutes // 60
                minutes = minutes % 60
                secs = seconds % 60
                return f"{hours} h {minutes} m {secs} s"

            uploaded_str = format_size(uploaded)
            total_str = format_size(total)
            mbps = (speed or 0) / (1024 * 1024)
            eta_str = format_time(eta) if eta and eta > 0 else "\u221e"

            # --- 界面更新（进度条和状态标签） ---
            self.lbl_status.setText(
                f"{uploaded_str} / {total_str} | {mbps:.2f} MB/s | ETA: {eta_str if eta else '--'}"
            )
        else:
            self.progress.setValue(0)
            self.lbl_status.setText("Speed: 0 MB/s | 0 / 0 | ETA: 0 s")

    def on_finished(self, ok):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._append_log("Upload finished" if ok else "Upload ended with errors")
        self.refresh_accounts()
        if self.worker:
            try:
                self.worker.deleteLater()
            except Exception:
                pass
            self.worker = None
    def _format_log_message(self, message: str) -> str:
        """Convert raw byte values in uploader logs to human-readable units."""
        import re
        def format_bytes(match):
            try:
                num = int(match.group(1))
            except Exception:
                return match.group(0)
            if num < 1024:
                return f"{num} B"
            elif num < 1024 ** 2:
                return f"{num / 1024:.1f} KB"
            elif num < 1024 ** 3:
                return f"{num / (1024 ** 2):.1f} MB"
            else:
                return f"{num / (1024 ** 3):.2f} GB"
        return re.sub(r"(\d+) B", format_bytes, message)