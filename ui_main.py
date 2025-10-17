# ui_main.py
import sys, os
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QListWidget, QFileDialog, QProgressBar, QTextEdit, QMessageBox
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from auth import list_accounts, acquire_token_interactive, remove_account
from uploader import upload_items

class UploadWorker(QThread):
    progress = pyqtSignal(int, int, float, float)
    log = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(self, selected_paths, base_dir, account_home_id):
        super().__init__()
        self.selected_paths = selected_paths
        self.base_dir = base_dir
        self.account_home_id = account_home_id
        self._stop = False

    def run(self):
        try:
            def log_cb(s):
                self.log.emit(s)
            def progress_cb(uploaded, total, speed=None, eta=None):
                self.progress.emit(int(uploaded), int(total), float(speed or 0), float(eta or 0))
            upload_items(self.selected_paths, base_dir=self.base_dir, account_home_id=self.account_home_id, progress_cb=progress_cb, log_cb=log_cb)
            self.finished.emit(True)
        except Exception as e:
            self.log.emit("ERROR: " + str(e))
            self.finished.emit(False)

class MainWindow(QWidget):
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
        self.lbl_folder = QLabel("No folder selected")
        self.btn_choose_files = QPushButton("Choose Files")
        self.btn_choose_folder = QPushButton("Choose Folder")
        folder_layout.addWidget(self.lbl_folder, 3)
        folder_layout.addWidget(self.btn_choose_files, 1)
        folder_layout.addWidget(self.btn_choose_folder, 1)
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
        self.layout.addLayout(acct_layout)
        self.layout.addLayout(folder_layout)
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
        # remove old btn_choose connections, add new ones for files/folder
        try:
            self.btn_choose.clicked.disconnect()
        except Exception:
            pass
        self.btn_choose_files.clicked.connect(self.choose_files)
        self.btn_choose_folder.clicked.connect(self.choose_folder)

    def shorten_path(self, path: str, max_len: int = 50) -> str:
        """中间省略显示长路径，保留头尾"""
        if not path or len(path) <= max_len:
            return path
        head_len = (max_len - 3) // 2
        tail_len = max_len - 3 - head_len
        return f"{path[:head_len]}...{path[-tail_len:]}"

    def refresh_accounts(self):
        self.acct_list.clear()
        accts = list_accounts()
        for a in accts:
            self.acct_list.addItem(f"{a.get('username')}  [{a.get('home_account_id')}]")
        if accts:
            self.acct_list.setCurrentRow(0)


    def choose_files(self):
        dialog = QFileDialog(self, "Select files to upload")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, False)
        if not dialog.exec():
            return
        selected = dialog.selectedFiles()
        if not selected:
            return

        # 过滤掉隐藏文件
        self.selected_paths = [f for f in selected if not os.path.basename(f).startswith('.')]
        common_dir = os.path.dirname(os.path.commonpath(self.selected_paths))
        self.base_dir = common_dir

        # 构造显示文本
        count = len(self.selected_paths)
        if count == 1:
            display_names = os.path.basename(self.selected_paths[0])
        else:
            display_names = ", ".join(os.path.basename(f) for f in self.selected_paths)
            # 如果名称过长则压缩显示
            if len(display_names) > 60:
                display_names = display_names[:30] + "..." + display_names[-20:]

        label_text = f"Files ({count}): {display_names}"
        self.lbl_folder.setText(self.shorten_path(label_text))

        # 计算总字节数
        total_bytes = sum(os.path.getsize(p) for p in self.selected_paths if os.path.isfile(p))
        self.total_bytes = total_bytes
        self.log.append(f"Total upload size: {total_bytes / (1024*1024*1024):.2f} GB")

    def choose_folder(self):
        dialog = QFileDialog(self, "Select folder to upload")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, False)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        if not dialog.exec():
            return
        selected = dialog.selectedFiles()
        if not selected:
            return
        self.base_dir = selected[0]
        self.selected_paths = []
        for root, dirs, files in os.walk(self.base_dir):
            for f in files:
                path = os.path.join(root, f)
                name = os.path.basename(path)
                if name.startswith('.') or name.startswith('._') or name == 'Icon\r':
                    continue
                self.selected_paths.append(path)
        self.lbl_folder.setText(f"Folder: {self.shorten_path(self.base_dir)}")
        total_bytes = sum(os.path.getsize(p) for p in self.selected_paths if os.path.isfile(p))
        self.total_bytes = total_bytes
        self.log.append(f"Total upload size: {total_bytes / (1024*1024*1024):.2f} GB")

    def add_account(self):
        try:
            token, acc = acquire_token_interactive()
            QMessageBox.information(self, "Signed In", f"Signed in as {acc.get('username')}")
            self.log.append(f"Signed in: {acc.get('username')}")
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
                self.log.append(f"Removed account {hid}")
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
        text = self.acct_list.currentItem().text()
        hid = text.split("[")[-1].split("]")[0]

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # 传 selected_paths 和 base_dir 给上传线程
        self.worker = UploadWorker(self.selected_paths, self.base_dir, account_home_id=hid)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(lambda s: self.log.append(s))
        self.worker.finished.connect(lambda ok: self.on_finished(ok))
        self.worker.start()
        self.log.append("Upload started")

    def stop_upload(self):
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(2000)
            self.log.append("Upload stopped by user")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_progress(self, uploaded, _ignored_total, speed=0, eta=0):
        QApplication.processEvents()
        total = getattr(self, "total_bytes", _ignored_total) or 0
        if total > 0:
            pct = int(uploaded * 100 / total)
            self.progress.setValue(pct)

            # ---- 动态单位换算 ----
            def format_size(bytes_value):
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
            mbps = speed / (1024 * 1024)
            eta_str = format_time(eta)

            self.lbl_status.setText(
                f"{uploaded_str} / {total_str} | {mbps:.2f} MB/s | ETA: {eta_str}"
            )
        else:
            self.progress.setValue(0)
            self.lbl_status.setText("Speed: 0 MB/s | 0 / 0 | ETA: 0 s")

    def on_finished(self, ok):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log.append("Upload finished" if ok else "Upload ended with errors")
        self.refresh_accounts()