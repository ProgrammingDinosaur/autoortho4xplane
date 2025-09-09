#!/usr/bin/env python3

import os
import time
import json
import socket
import threading
from aoconfig import CFG
import logging
log = logging.getLogger(__name__)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi import Body
from fastapi.templating import Jinja2Templates
import socketio
from fastapi.staticfiles import StaticFiles

from xp_udp import DecodePacket, RequestDataRefs

from aostats import STATS
#STATS = {'count': 71036, 'chunk_hit': 66094, 'mm_counts': {0: 19, 1: 39, 2: 97, 3: 294, 4: 2982}, 'mm_averages': {0: 0.56, 1: 0.14, 2: 0.04, 3: 0.01, 4: 0.0}, 'chunk_miss': 4942, 'bytes_dl': 65977757}
from utils.constants import MAPTYPES
from utils.tile_db_service import tile_db_service


RUNNING=True

# FastAPI app and templates
app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), 'templates'))
# Provide Flask-like url_for in templates
templates.env.globals['url_for'] = lambda name, **params: app.url_path_for(name, **params)

# Socket.IO ASGI server
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')


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
        self.t = threading.Thread(target=self._udp_listen)
        self.t.start()

    def get_info(self):
        RequestDataRefs(self.sock, CFG.flightdata.xplane_udp_port)
        data, addr = self.sock.recvfrom(1024)
        values = DecodePacket(data)
        lat = values[0][0]
        lon = values[1][0]
        alt = values[3][0]
        hdg = values[4][0]
        spd = values[6][0]

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
                STATS['minutes_to_start'] = round(delta/60, 2)

            self.connected = True

            values = DecodePacket(data)
            lat = values[0][0]
            lon = values[1][0]
            alt = values[3][0]
            hdg = values[4][0]
            spd = values[6][0]

            log.debug(f"Lat: {lat}, Lon: {lon}, Alt: {alt}")
            
            self.alt = alt
            self.lat = lat
            self.lon = lon
            self.hdg = hdg
            self.spd = spd


        log.info("UDP listen thread exiting...")

    def stop(self):
        log.info("FlightTracker shutdown requested.")
        self.running=False
        if self.t:
            self.t.join()
        log.info("FlightTracker exiting.")

ft = FlightTracker()

# Socket.IO events (async)
@sio.event
async def connect(sid, environ, auth):
    log.info(f'client connected {sid}')

@sio.event
async def disconnect(sid):
    log.info(f'client disconnected {sid}')

@sio.on('handle_latlon')
async def handle_latlon(sid):
    log.info("Handle lat lon.")
    while True:
        lat = ft.lat
        lon = ft.lon
        log.debug(f"emit: {lat} X {lon}")
        await sio.emit('latlon', {"lat": lat, "lon": lon}, to=sid)
        await sio.sleep(2)

@sio.on("handle_metrics")
async def handle_metrics(sid):
    log.info("Handle metrics.")
    while True:
        await sio.emit('metrics', STATS or {"init": 1}, to=sid)
        await sio.sleep(5)

# HTTP routes via FastAPI
@app.get('/get_latlon', name='get_latlon')
async def get_latlon_route():
    lat = ft.lat
    lon = ft.lon
    log.debug(f"{lat} X {lon}")
    return JSONResponse({"lat": lat, "lon": lon})

@app.get("/tile_maptype", name='tile_maptype')
async def tile_maptype(lat: int, lon: int):
    try:
        maptype = tile_db_service.get_tile_maptype(lat, lon)
        return JSONResponse({"lat": lat, "lon": lon, "maptype": maptype})
    except Exception as e:
        log.exception("tile_maptype error")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/available_maptypes", name='available_maptypes')
async def available_maptypes():
    return JSONResponse({"maptypes": MAPTYPES})

@app.post("/change_maptypes", name='change_maptypes')
async def change_maptypes(payload: dict = Body(...)):
    try:
        maptype = payload.get("maptype")
        tiles = payload.get("tiles") or []
        if not maptype or not isinstance(tiles, list):
            return JSONResponse({"error": "Invalid payload"}, status_code=400)
        changed = 0
        for item in tiles:
            lat = item.get("lat")
            lon = item.get("lon")
            if isinstance(lat, int) and isinstance(lon, int):
                tile_db_service.change_maptype(lat, lon, maptype)
                changed += 1
        return JSONResponse({"changed": changed})
    except Exception as e:
        log.exception("change_maptypes error")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/", response_class=HTMLResponse, name='index')
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/map", response_class=HTMLResponse, name='map')
async def map_view(request: Request):
    return templates.TemplateResponse("map.html", {"request": request, "mapkey": ""})

@app.get("/stats", response_class=HTMLResponse, name='stats')
async def stats_view(request: Request):
    return templates.TemplateResponse("stats.html", {"request": request, "graphs": STATS})

@app.get("/metrics", name='metrics')
async def metrics():
    return JSONResponse(STATS)

# Mount Socket.IO over FastAPI as a single ASGI app
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# Static files (for external JS/CSS)
_static_dir = os.path.join(os.path.dirname(__file__), 'static')
try:
    if os.path.isdir(_static_dir):
        app.mount('/static', StaticFiles(directory=_static_dir), name='static')
except Exception as _e:
    log.debug(f"Static mount skipped: {_e}")

def run():
    log.info("Start flighttracker...")
    import uvicorn
    uvicorn.run(
        asgi_app,
        host='0.0.0.0',
        port=int(CFG.flightdata.webui_port),
        log_level='debug' if getattr(CFG.general, 'debug', False) else 'info',
    )
    log.info("Exiting flighttracker ...") 

def main():
    ft.start()
    try:
        import uvicorn
        uvicorn.run(asgi_app, host='0.0.0.0', port=int(CFG.flightdata.webui_port))
    except KeyboardInterrupt:
        print("Shutdown requested.")
    finally:
        print("App exiting...")
        ft.stop()
    print("Done!")

if __name__ == "__main__":
    main()
