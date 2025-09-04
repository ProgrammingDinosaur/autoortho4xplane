import os
import sys
import socket
import subprocess
import threading
import queue

tile_printer_proc = None
TCP_PORT = 50505

_msg_queue = queue.Queue()
_sender_thread = None

def _sender():
    sock = None
    while True:
        msg = _msg_queue.get()
        if msg is None:
            if sock:
                sock.close()
            break
        while True:
            try:
                if sock is None:
                    sock = socket.create_connection(('127.0.0.1', TCP_PORT), timeout=2)
                sock.sendall((msg + "\n").encode())
                break
            except Exception:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None
                import time
                time.sleep(0.5)  # Wait before retrying

def send_tile_msg(msg):
    try:
        _msg_queue.put_nowait(msg)
    except Exception:
        pass

def start_tile_printer():
    global tile_printer_proc, _sender_thread
    if tile_printer_proc is None:
        script_path = os.path.join(os.path.dirname(__file__), "tile_printer_gui.py")
        tile_printer_proc = subprocess.Popen([sys.executable, script_path])
    if _sender_thread is None:
        _sender_thread = threading.Thread(target=_sender, daemon=True)
        _sender_thread.start()

def stop_tile_printer():
    global tile_printer_proc, _sender_thread
    if tile_printer_proc:
        try:
            tile_printer_proc.terminate()
            tile_printer_proc.wait(timeout=2)
        except Exception:
            pass
        tile_printer_proc = None
    if _sender_thread:
        _msg_queue.put(None)
        _sender_thread = None