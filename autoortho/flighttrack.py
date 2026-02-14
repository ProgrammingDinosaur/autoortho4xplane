#!/usr/bin/env python3

import os
import sys
import time
import json
import socket
import threading

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

import logging
log = logging.getLogger(__name__)

from flask import Flask, render_template, url_for, request, jsonify
from flask_socketio import SocketIO, send, emit

try:
    from autoortho.xp_udp import DecodePacket, RequestDataRefs
except ImportError:
    from xp_udp import DecodePacket, RequestDataRefs

try:
    from autoortho.aostats import STATS
except ImportError:
    from aostats import STATS
#STATS = {'count': 71036, 'chunk_hit': 66094, 'mm_counts': {0: 19, 1: 39, 2: 97, 3: 294, 4: 2982}, 'mm_averages': {0: 0.56, 1: 0.14, 2: 0.04, 3: 0.01, 4: 0.0}, 'chunk_miss': 4942, 'bytes_dl': 65977757}

RUNNING=True

# Shutdown flag for the Flask-SocketIO server
_server_shutdown_requested = threading.Event()

# Determine template folder for frozen vs dev mode
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    # PyInstaller: templates are in _MEIPASS/autoortho/templates
    template_folder = os.path.join(sys._MEIPASS, 'autoortho', 'templates')
else:
    # Development: templates are relative to this file
    template_folder = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'templates')

app = Flask(__name__, template_folder=template_folder)
app.config['SECRET_KEY'] = 'secret!'
# Explicitly set async_mode='threading' for compatibility with PyInstaller frozen apps
# Auto-detection fails in frozen environments
socketio = SocketIO(app, async_mode='threading')


class FlightTracker(object):
    
    lat = -1
    lon = -1
    alt = -1
    hdg = -1
    spd = -1
    t = None

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, # Internet
                            socket.SOCK_DGRAM) # UDP

        self.sock.settimeout(5.0)
        self.connected = False
        self.running = False
        self.num_failures = 0

    def start(self):
        self.running = True
        self.start_time = time.time()
        self.t = threading.Thread(target=self._udp_listen, daemon=True)
        self.t.start()

    def get_info(self):
        RequestDataRefs(self.sock, CFG.flightdata.xplane_udp_port)
        data, addr = self.sock.recvfrom(1024)
        values = DecodePacket(data)
        
        # Safely extract values - packet may not contain all expected datarefs
        try:
            lat = values[0][0]
            lon = values[1][0]
            alt = values[3][0]
            hdg = values[4][0]
            spd = values[6][0]
        except KeyError as e:
            log.debug(f"Incomplete packet in get_info, missing dataref index {e}")
            return (self.lat, self.lon, self.alt, self.hdg, self.spd)  # Return last known values

        return (lat, lon, alt, hdg, spd)

    def _udp_listen(self):
        log.debug("Listen!")
        RequestDataRefs(self.sock, CFG.flightdata.xplane_udp_port)
        while self.running:
            time.sleep(0.1)
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:

                if self.connected:
                    # We were connected but lost a packet.  First just log
                    # this
                    self.num_failures += 1
                    log.debug("We are connected but a packet timed out.  NBD.")

                if self.num_failures > 3:
                    # We are transitioning states
                    log.info("FT: Flight disconnected.")
                    self.start_time = time.time()
                    self.connected = False
                    self.running = False
                    self.num_failures = 0

                    #log.debug("Socket timeout.  Reset.")
                    #RequestDataRefs(self.sock, CFG.flightdata.xplane_udp_port)
                time.sleep(1)
                continue
            except ConnectionResetError: 
                log.debug("Connection reset.")
                time.sleep(1)
                continue


            self.num_failures = 0
            if not self.connected:
                # We are transitioning states
                log.info("FT: Flight is starting.")
                delta = time.time() - self.start_time
                log.info(f"FT: Time to start was {round(delta/60, 2)} minutes.")

            self.connected = True

            values = DecodePacket(data)
            
            # Safely extract values - packet may not contain all expected datarefs
            try:
                lat = values[0][0]
                lon = values[1][0]
                alt = values[3][0]
                hdg = values[4][0]
                spd = values[6][0]
            except KeyError as e:
                # Incomplete packet - missing expected dataref indices
                log.debug(f"Incomplete packet, missing dataref index {e}. Waiting for complete data...")
                continue

            log.debug(f"Lat: {lat}, Lon: {lon}, Alt: {alt}")
            
            self.alt = alt
            self.lat = lat
            self.lon = lon
            self.hdg = hdg
            self.spd = spd


        log.info("UDP listen thread exiting...")

    def stop(self):
        log.info("FlightTracker shutdown requested.")
        self.running = False
        # Close socket to unblock any pending recv operations
        try:
            self.sock.close()
        except Exception:
            pass
        if self.t and self.t.is_alive():
            self.t.join(timeout=5.0)
            if self.t.is_alive():
                log.warning("FlightTracker UDP thread did not exit within timeout")
        log.info("FlightTracker exiting.")

ft = FlightTracker()

@socketio.on('connect')
def connect():
    log.info(f'client connected {request.sid}')

@socketio.on('disconnect')
def disconnect():
    log.info(f'client disconnected {request.sid}')

@socketio.on('handle_latlon')
def handle_latlon():
    log.info("Handle lat lon.")
    while True:
        lat = ft.lat
        lon = ft.lon
        #lat, lon, alt, hdg, spd = ft.get_info()
        log.debug(f"emit: {lat} X {lon}")
        socketio.emit('latlon', {"lat":lat,"lon":lon})
        socketio.sleep(2)

@socketio.on("handle_metrics")
def handle_metrics():
    log.info("Handle metrics.")
    while True:
        socketio.emit('metrics', STATS or {"init": 1})
        socketio.sleep(5)

@app.route('/get_latlon')
def get_latlon():
    lat = ft.lat
    lon = ft.lon
    #lat, lon, alt, hdg, spd = ft.get_info()
    log.debug(f"{lat} X {lon}")
    return jsonify({"lat":lat,"lon":lon})

@app.route("/")
def index():
    return render_template(
        "index.html"
    )

@app.route("/map")
def map():
    return render_template(
        "map.html",
        mapkey = ""
    )

@app.route("/stats")
def stats():
    #graphs = [ x for x in STATS.keys() ]
    return render_template(
        "stats.html",
        graphs=STATS
    )

@app.route("/metrics")
def metrics():
    return STATS

def shutdown_server():
    """
    Shutdown the Flask-SocketIO server gracefully.
    
    Call this function before ft.stop() to ensure clean exit.
    """
    global _server_shutdown_requested
    log.info("FlightTracker server shutdown requested.")
    _server_shutdown_requested.set()
    try:
        # socketio.stop() signals the server to stop accepting new connections
        # and finish processing existing ones
        socketio.stop()
    except Exception as e:
        log.debug(f"socketio.stop() error (may be expected): {e}")


def run():
    #app.run(host='0.0.0.0', port=CFG.flightdata.webui_port, debug=CFG.general.debug, threaded=True, use_reloader=False)
    log.info("Start flighttracker...")
    try:
        socketio.run(app, host='0.0.0.0', port=int(CFG.flightdata.webui_port))
    except Exception as e:
        if not _server_shutdown_requested.is_set():
            log.error(f"FlightTracker server error: {e}")
    log.info("Exiting flighttracker ...") 

def main():
    ft.start()
    try:
        app.run(host='0.0.0.0', debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("Shutdown requested.")
    finally:
        print("App exiting...")
        ft.stop()
    print("Done!")

if __name__ == "__main__":
    main()
