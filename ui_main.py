# ui_main.py
import sys, os
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QListWidget, QFileDialog, QProgressBar, QTextEdit, QMessageBox
from PyQt6.QtCore import QThread, pyqtSignal
from auth import list_accounts, acquire_token_interactive, remove_account
from uploader import upload_items

class UploadWorker(QThread):
    progress = pyqtSignal(int, int)
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
            def progress_cb(uploaded, total):
                self.progress.emit(int(uploaded), int(total))
            upload_items(self.selected_paths, base_dir=self.base_dir, account_home_id=self.account_home_id, progress_cb=progress_cb, log_cb=log_cb)
            self.finished.emit(True)
        except Exception as e:
            self.log.emit("ERROR: " + str(e))
            self.finished.emit(False)

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OneDrive Folder Uploader")
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
        self.btn_choose = QPushButton("Choose Folder")
        folder_layout.addWidget(self.lbl_folder, 4)
        folder_layout.addWidget(self.btn_choose, 1)
        # controls
        ctrl_layout = QHBoxLayout()
        self.btn_start = QPushButton("Start Upload")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        ctrl_layout.addWidget(self.btn_start)
        ctrl_layout.addWidget(self.btn_stop)
        # progress & log
        self.progress = QProgressBar()
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        # assemble
        self.layout.addLayout(acct_layout)
        self.layout.addLayout(folder_layout)
        self.layout.addLayout(ctrl_layout)
        self.layout.addWidget(self.progress)
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
        try:
            self.btn_choose.clicked.disconnect()
        except TypeError:
            pass
        self.btn_choose.clicked.connect(self.choose_folder_or_file)

    def refresh_accounts(self):
        self.acct_list.clear()
        accts = list_accounts()
        for a in accts:
            self.acct_list.addItem(f"{a.get('username')}  [{a.get('home_account_id')}]")
        if accts:
            self.acct_list.setCurrentRow(0)

    def choose_folder_or_file(self):
        dlg = QFileDialog(self)
        dlg.setFileMode(QFileDialog.FileMode.Directory)  # 只选择文件夹
        dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
        if dlg.exec():
            paths = dlg.selectedFiles()
            if not paths:
                return
            self.base_dir = paths[0]
            self.selected_paths = []
            for root, dirs, files in os.walk(self.base_dir):
                for f in files:
                    self.selected_paths.append(os.path.join(root, f))
            self.lbl_folder.setText(f"Folder: {self.base_dir}")

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

    def on_progress(self, uploaded, total):
        if total > 0:
            pct = int(uploaded * 100 / total)
            self.progress.setValue(pct)
        else:
            self.progress.setValue(0)

    def on_finished(self, ok):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log.append("Upload finished" if ok else "Upload ended with errors")
        self.refresh_accounts()