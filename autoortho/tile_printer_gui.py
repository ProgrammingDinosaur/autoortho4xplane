import sys
import re
import socket
import platform
import threading
import utils.resources_rc
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QTextEdit, QPushButton
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QTextCursor, QIcon

TCP_PORT = 50505

class Communicate(QObject):
    new_msg = Signal(str)

class TilePrinterGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Loaded Tiles Info")
        self.resize(420, 240)

        self.system = platform.system().lower()
        if self.system == 'windows':
            icon_path = ":/imgs/ao-icon.ico"
        else:
            icon_path = ":/imgs/ao-icon.png"
        self.setWindowIcon(QIcon(icon_path))

        self.downloaded_size = 0.0
        self.downloaded_count = 0

        self.retrieved_size = 0.0
        self.retrieved_count = 0

        self.total_mb = 0.0
        self.total_count = 0
        self.total_error = 0

        self.paused = False
        self.paused_queue = []

        label_grid = QGridLayout()
        self.total_downloaded_count_label = QLabel("Downloaded Tiles: 0")
        self.total_downloaded_count_label.setAlignment(Qt.AlignLeft)
        label_grid.addWidget(self.total_downloaded_count_label, 0, 0)

        self.total_downloaded_size_label = QLabel("Downloaded Tiles Size: 0 MB")
        self.total_downloaded_size_label.setAlignment(Qt.AlignRight)
        label_grid.addWidget(self.total_downloaded_size_label, 0, 1)

        self.total_retrieved_count_label = QLabel("Retrieved Tiles: 0")
        self.total_retrieved_count_label.setAlignment(Qt.AlignLeft)
        label_grid.addWidget(self.total_retrieved_count_label, 1, 0)

        self.total_retrieved_size_label = QLabel("Retrieved Tiles Size: 0 MB")
        self.total_retrieved_size_label.setAlignment(Qt.AlignRight)
        label_grid.addWidget(self.total_retrieved_size_label, 1, 1)

        self.total_error_label = QLabel("Errors Found: 0 Error(s)")
        self.total_error_label.setAlignment(Qt.AlignLeft)
        label_grid.addWidget(self.total_error_label, 2, 0)

        self.total_tiles_label = QLabel("Total Tiles Count/Size: 0/0 MB")
        self.total_tiles_label.setAlignment(Qt.AlignRight)
        label_grid.addWidget(self.total_tiles_label, 2, 1)

        layout = QVBoxLayout()
        layout.addLayout(label_grid)

        button_layout = QHBoxLayout()
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_all)
        button_layout.addWidget(self.clear_button)

        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self.toggle_pause)
        button_layout.addWidget(self.pause_button)

        layout.addLayout(button_layout)

        self.text_area = QTextEdit()
        self.text_area.setReadOnly(True)
        layout.addWidget(self.text_area)

        self.setLayout(layout)

        self.comm = Communicate()
        self.comm.new_msg.connect(self.handle_msg)

        self.server_thread = threading.Thread(target=self.tcp_server, daemon=True)
        self.server_thread.start()

    def clear_all(self):
        self.text_area.clear()
        self.downloaded_size = 0.0
        self.downloaded_count = 0
        self.retrieved_size = 0.0
        self.retrieved_count = 0
        self.total_mb = 0.0
        self.total_count = 0
        self.total_error = 0

        self.total_downloaded_count_label.setText("Downloaded Tiles: 0")
        self.total_downloaded_size_label.setText("Downloaded Tiles Size: 0 MB")
        self.total_retrieved_count_label.setText("Retrieved Tiles: 0")
        self.total_retrieved_size_label.setText("Retrieved Tiles Size: 0 MB")
        self.total_error_label.setText("Errors Found: 0 Error(s)")
        self.total_tiles_label.setText("Total Tiles Count/Size: 0/0 MB")

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.pause_button.setText("Resume")
            self.setWindowTitle("Loaded Tiles Info (Paused)")
        else:
            self.pause_button.setText("Pause")
            self.setWindowTitle("Loaded Tiles Info")
            for msg in self.paused_queue:
                self.handle_msg(msg)
            self.paused_queue.clear()

    def handle_msg(self, line):
        if self.paused:
            self.paused_queue.append(line)
            return
        self.text_area.append(line.rstrip())
        match = re.search(r'\(([\d.]+) MB\)', line)
        if "Error" in line:
            self.total_error+=1
            self.total_error_label.setText(f"Errors Found: {self.total_error} Error(s)")
        else:
            if match:
                if "Downloaded" in line:
                    self.downloaded_count += 1
                    self.downloaded_size += float(match.group(1))
                    self.total_downloaded_count_label.setText(f"Downloaded Tiles: {self.downloaded_count}")
                    self.total_downloaded_size_label.setText(f"Downloaded Tiles Size: {self.downloaded_size:.2f} MB")
                elif "Retrieved" in line:
                    self.retrieved_count += 1
                    self.retrieved_size += float(match.group(1))
                    self.total_retrieved_count_label.setText(f"Retrieved Tiles: {self.retrieved_count}")
                    self.total_retrieved_size_label.setText(f"Retrieved Tiles Size: {self.retrieved_size:.2f} MB")
                self.total_count = self.downloaded_count + self.retrieved_count
                self.total_mb = self.downloaded_size + self.retrieved_size
                self.total_tiles_label.setText(f"Total Tiles Count/Size: {self.total_count}/{self.total_mb:.2f} MB")
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