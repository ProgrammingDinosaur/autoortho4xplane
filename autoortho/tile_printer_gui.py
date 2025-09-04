import sys
import re
import socket
import platform
import threading
import utils.resources_rc
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QTextEdit, QPushButton
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QTextCursor, QIcon

TCP_PORT = 50505

class Communicate(QObject):
    new_msg = Signal(str)

class TilePrinterGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tiles Download Info")
        self.resize(320, 160)

        self.system = platform.system().lower()
        if self.system == 'windows':
            icon_path = ":/imgs/ao-icon.ico"
        else:
            icon_path = ":/imgs/ao-icon.png"
        self.setWindowIcon(QIcon(icon_path))

        self.total_mb = 0.0

        layout = QVBoxLayout()
        self.total_label = QLabel("Total Downloaded Size: 0.00 MB")
        self.total_label.setAlignment(Qt.AlignLeft)
        layout.addWidget(self.total_label)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_all)
        layout.addWidget(self.clear_button)

        self.text_area = QTextEdit()
        self.text_area.setReadOnly(True)
        layout.addWidget(self.text_area)

        self.setLayout(layout)

        # Signal for thread-safe GUI update
        self.comm = Communicate()
        self.comm.new_msg.connect(self.handle_msg)

        # Start TCP server in a thread
        self.server_thread = threading.Thread(target=self.tcp_server, daemon=True)
        self.server_thread.start()

    def clear_all(self):
        self.text_area.clear()
        self.total_mb = 0.0
        self.total_label.setText("Total Downloaded Size: 0.00 MB")

    def handle_msg(self, line):
        if line == "MINIMIZE_WINDOW":
            self.showMinimized()
            return
        self.text_area.append(line.rstrip())
        match = re.search(r'\(([\d.]+) MB\)', line)
        if match:
            self.total_mb += float(match.group(1))
            self.total_label.setText(f"Total Downloaded Size: {self.total_mb:.2f} MB")
        self.text_area.moveCursor(QTextCursor.End)

    def tcp_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', TCP_PORT))
            s.listen(1)
            while True:
                conn, _ = s.accept()
                with conn:
                    try:
                        while True:
                            data = conn.recv(1024)
                            if not data:
                                break
                            for line in data.decode(errors="ignore").splitlines():
                                self.comm.new_msg.emit(line)
                    except ConnectionResetError:
                        pass

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TilePrinterGUI()
    window.show()
    sys.exit(app.exec())