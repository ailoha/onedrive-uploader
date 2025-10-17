# main.py
import sys
from PyQt6.QtWidgets import QApplication
from ui_main import MainWindow
from uploader import clear_old_sessions

def main():
    clear_old_sessions()  # 清空旧的续传会话
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()