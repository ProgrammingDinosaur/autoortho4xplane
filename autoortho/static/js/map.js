// Leaflet map initialization and Socket.IO integration
(function () {
    const map = L.map('map', 
        {minZoom: 5, maxZoom: 18}
    ).setView([51.505, -0.09], 5);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);
    const marker = L.marker([51.5, -0.09]).addTo(map);
    const line = L.polyline([]).addTo(map);

    // ---- Grid overlay ------------------------------------------------------
    const gridLinesLayer = L.layerGroup().addTo(map);
    const gridCellsLayer = L.layerGroup().addTo(map);
    const gridLabelsLayer = L.layerGroup().addTo(map);
    const selectedCells = new Set();
    const GRID_MIN_ZOOM_FOR_CELLS = 5; // require zoom-in for clickable 1x1° cells
    const tileMaptypeCache = new Map(); // key: "lat,lon" -> maptype string
    const availableMaptypes = new Array(); // list of maptypes
    const selectionUI = { container: null, select: null };
    const pendingOverrides = new Map(); // key: "lat,lon" -> maptype string (user-staged)
    const MODE_MAPTYPE = 'maptype';
    const MODE_CACHE = 'cache';
    let currentMode = MODE_MAPTYPE;
    const modeUI = { container: null, maptypeBtn: null, cacheBtn: null };

    let recenter_call = true;
    let availableTilesParsed = [];


    fetchAvailableMaptypes().then(function (maptypes) {
        maptypes.forEach(function (mt) {
            availableMaptypes.push(mt);
        });
        populateDropdownOptions();
    });
    // Fetch available tiles once and rebuild when ready
    (function () {
        fetch('/available_tiles')
            .then(function (resp) { if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); })
            .then(function (data) {
                const tiles = (data && Array.isArray(data.tiles)) ? data.tiles : [];
                availableTilesParsed = tiles.map(parseCoordTile).filter(Boolean);
                scheduleRebuild();
            })
            .catch(function () { /* ignore */ });
    })();

    // Inject minimal styles for grid cell labels
    if (typeof document !== 'undefined' && document.head) {
        const styleEl = document.createElement('style');
        styleEl.textContent = '.grid-label{color:#1d71d1;font:12px/1.2 Arial,Helvetica,sans-serif;font-weight:bold;text-shadow:0 0 2px rgba(255,255,255,0.9),0 0 4px rgba(255,255,255,0.6);pointer-events:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;justify-content:center;width:100%;height:100%;padding:0 2px;}.maptype-control{background:rgba(255,255,255,0.95);padding:6px 8px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,0.2);}.maptype-select{font:12px Arial,Helvetica,sans-serif;min-width:220px;}';
        document.head.appendChild(styleEl);

        const styleEl2 = document.createElement('style');
        styleEl2.textContent = '.ao-help-btn{width:34px;height:34px;border-radius:50%;background:#1d71d1;color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,0.25);font:14px/1 Arial,Helvetica,sans-serif;font-weight:bold}.ao-help-btn:hover{background:#5183bd}.ao-help-panel{position:fixed;top:0;right:0;height:100%;width:360px;max-width:85vw;background:#ffffff;color:#111;transform:translateX(100%);transition:transform .25s ease-in-out, box-shadow .25s ease-in-out;z-index:4000;box-shadow:0 0 0 rgba(0,0,0,0)}.ao-help-panel.open{transform:translateX(0);box-shadow:-2px 0 12px rgba(0,0,0,0.25)}.ao-help-panel .ao-help-header{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #e5e5e5;background:#f8f8f8}.ao-help-panel .ao-help-title{margin:0;font:600 16px/1.2 Arial,Helvetica,sans-serif;color:#1d71d1}.ao-help-panel .ao-help-close{border:none;background:transparent;font:700 18px/1 Arial,Helvetica,sans-serif;color:#666;cursor:pointer}.ao-help-panel .ao-help-close:hover{color:#000}.ao-help-panel .ao-help-content{padding:14px;overflow:auto;height:calc(100% - 50px)}.ao-help-panel .ao-help-content h2{font:600 18px/1.2 Arial,Helvetica,sans-serif;color:#333;margin:10px 0}.ao-help-panel .ao-help-content p{margin:8px 0 12px;color:#333}.ao-help-panel .ao-help-content ul{padding-left:18px;margin:8px 0 12px}.ao-help-panel .ao-help-content code{background:#f0f3f7;color:#0b3d91;padding:2px 4px;border-radius:3px;font-family:Menlo,Consolas,monospace;font-size:12px}.ao-help-panel .ao-help-content pre{background:#0b0f14;color:#e3e9f3;padding:10px;border-radius:6px;overflow:auto}';
        document.head.appendChild(styleEl2);
        const styleEl3 = document.createElement('style');
        styleEl3.textContent = '.mode-switch{background:rgba(255,255,255,0.95);padding:4px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,0.2);display:inline-flex;gap:4px;align-items:center}.mode-btn{border:1px solid #d0d7de;border-radius:4px;background:#f6f8fa;color:#24292f;cursor:pointer;padding:4px 8px;font:12px Arial,Helvetica,sans-serif}.mode-btn.active{background:#1d71d1;color:#fff;border-color:#1d71d1}.mode-btn:focus{outline:none}.mode-legend{margin-left:8px;font:12px Arial,Helvetica,sans-serif;color:#333}';
        document.head.appendChild(styleEl3);
    }

    // Create a Leaflet control with dropdown for selected tiles
    const SelectionControl = L.Control.extend({
        onAdd: function () {
            const container = L.DomUtil.create('div', 'maptype-control');
            container.style.display = 'none';
            const select = L.DomUtil.create('select', 'maptype-select', container);
            selectionUI.container = container;
            selectionUI.select = select;
            // Prevent map interactions while using the control
            L.DomEvent.disableClickPropagation(container);
            return container;
        }
    });
    const selectionControl = new SelectionControl({ position: 'topright' });
    selectionControl.addTo(map);

    // Create Help panel (slides from right)
    let helpPanel = null;
    function ensureHelpPanel() {
        if (helpPanel || typeof document === 'undefined') return;
        helpPanel = document.createElement('div');
        helpPanel.className = 'ao-help-panel';
        helpPanel.innerHTML = ''+
            '<div class="ao-help-header">'+
            '  <h3 class="ao-help-title">Tile Manager Guide</h3>'+
            '  <button class="ao-help-close" aria-label="Close">×</button>'+
            '</div>'+
            '<div class="ao-help-content">'+
            '  <h2>Moving arond and selecting tiles</h2>'+
            '  <ul>'+
            '    <li>You can move the map by clicking and dragging with the right mouse button.</li>'+
            '    <li>You can select tiles by clicking and dragging with the left mouse button.</li>'+
            '    <li>You can select single tiles by clicking on them with the left mouse button.</li>'+
            '    <li>You can deselect tiles by holding the Shift key while selecting.</li>'+
            '    <li>Selected tiles are highlighted in orange.</li>'+
            '  </ul>'+
            '  <h2>Modes</h2>'+
            '  <ul>'+
            '    <li><b>Maptype</b>: assign imagery providers per tile.</li>'+
            '    <li><b>Cache</b>: visualize/manage cached tiles (coming soon).</li>'+
            '  </ul>'+
            '  <h2>Changing the maptype</h2>'+
            '  <p>Once you have at least one tile selected, you can change the maptype by selecting a new maptype from the dropdown menu.</p>'+
            '  <p>The map will update to show the new maptype for the selected tiles, but the changes will not be applied until you click the "Apply" button.</p>'+
            ' <h2>About the maptypes</h2>'+
            '  <p>The maptypes are as follows:</p>'+
            '  <ul>'+
            '    <li>DFLT: Use the default maptype this tile was built with. Useful for Custom tiles</li>'+
            '    <li>BI: Bing Maps</li>'+
            '    <li>NAIP: NAIP</li>'+
            '    <li>EOX: EOX</li>'+
            '    <li>USGS: USGS</li>'+
            '    <li>FIREFLY: Firefly</li>'+
            '    <li>GO2: Google Maps</li>'+
            '    <li>ARC: ArcGIS</li>'+
            '    <li>YNDX: Yandex Maps</li>'+
            '    <li>APPLE: Apple Maps</li>'+
            '  </ul>'+
            '  <h2>Applying the changes</h2>'+
            '  <p>You can apply the changes by clicking the "Apply" button. This will save the overrides and AO will use them next time the tile is used.</p>'+
            '</div>';
        document.body.appendChild(helpPanel);
        const closeBtn = helpPanel.querySelector('.ao-help-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', function () { toggleHelp(false); });
        }
        // Close on Escape
        document.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape') toggleHelp(false);
        });
    }

    function toggleHelp(open) {
        ensureHelpPanel();
        if (!helpPanel) return;
        if (open === undefined) {
            helpPanel.classList.toggle('open');
        } else if (open) {
            helpPanel.classList.add('open');
        } else {
            helpPanel.classList.remove('open');
        }
    }

    // Add circular "?" button as a Leaflet control (top-right)
    const HelpControl = L.Control.extend({
        onAdd: function () {
            const container = L.DomUtil.create('div', '');
            const btn = L.DomUtil.create('div', 'ao-help-btn', container);
            btn.setAttribute('title', 'Open Guide');
            btn.textContent = '?';
            L.DomEvent.disableClickPropagation(container);
            btn.addEventListener('click', function (ev) { ev.preventDefault(); toggleHelp(); });
            return container;
        }
    });
    (new HelpControl({ position: 'topright' })).addTo(map);

    // Mode switch control (Maptype | Cache)
    function setMode(mode) {
        if (mode !== MODE_MAPTYPE && mode !== MODE_CACHE) return;
        currentMode = mode;
        if (modeUI.maptypeBtn && modeUI.cacheBtn) {
            if (mode === MODE_MAPTYPE) {
                modeUI.maptypeBtn.classList.add('active');
                modeUI.cacheBtn.classList.remove('active');
            } else {
                modeUI.cacheBtn.classList.add('active');
                modeUI.maptypeBtn.classList.remove('active');
            }
        }
        updateSelectionUIVisibility();
        scheduleRebuild();
    }

    const ModeControl = L.Control.extend({
        onAdd: function () {
            const container = L.DomUtil.create('div', 'mode-switch');
            const btnMap = L.DomUtil.create('button', 'mode-btn active', container);
            btnMap.type = 'button';
            btnMap.textContent = 'Maptype';
            const btnCache = L.DomUtil.create('button', 'mode-btn', container);
            btnCache.type = 'button';
            btnCache.textContent = 'Cache';
            modeUI.container = container;
            modeUI.maptypeBtn = btnMap;
            modeUI.cacheBtn = btnCache;
            L.DomEvent.disableClickPropagation(container);
            btnMap.addEventListener('click', function (ev) { ev.preventDefault(); setMode(MODE_MAPTYPE); });
            btnCache.addEventListener('click', function (ev) { ev.preventDefault(); setMode(MODE_CACHE); });
            return container;
        }
    });
    (new ModeControl({ position: 'topright' })).addTo(map);

    async function fetchTileMaptype(latDeg, lonDeg) {
        const key = cellKey(latDeg, lonDeg);
        if (pendingOverrides.has(key)) return pendingOverrides.get(key);
        if (tileMaptypeCache.has(key)) return tileMaptypeCache.get(key);
        try {
            const resp = await fetch(`/tile_maptype?lat=${latDeg}&lon=${lonDeg}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const mt = (data && data.maptype) ? data.maptype : 'DFLT';
            tileMaptypeCache.set(key, mt);
            return mt;
        } catch (_e) {
            tileMaptypeCache.set(key, 'DFLT');
            return 'DFLT';
        }
    }

    async function fetchAvailableMaptypes() {
        const resp = await fetch('/available_maptypes');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        return data.maptypes;
    }

    // Placeholder for future cache status API
    async function fetchTileCacheStatus(latDeg, lonDeg) {
        // TODO: replace with backend call when available
        return '\u2014'; // em dash placeholder
    }

    function populateDropdownOptions() {
        if (!selectionUI.select) return;
        while (selectionUI.select.firstChild) selectionUI.select.removeChild(selectionUI.select.firstChild);
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'Select new maptype for selected tiles';
        placeholder.disabled = true;
        placeholder.selected = true;
        selectionUI.select.appendChild(placeholder);
        availableMaptypes.forEach(function (mt) {
            const opt = document.createElement('option');
            opt.value = mt;
            opt.textContent = mt;
            selectionUI.select.appendChild(opt);
        });
    }

    function updateSelectionUIVisibility() {
        if (!selectionUI.container) return;
        selectionUI.container.style.display = (selectedCells.size > 0 && currentMode === MODE_MAPTYPE) ? 'block' : 'none';
    }

    // ---- Mouse interaction overrides ---------------------------------------
    // Disable default left-button drag panning; we'll implement right-button panning
    map.dragging.disable();
    // Disable default Shift+drag box-zoom so we can use Shift for unselect
    if (map.boxZoom && map.boxZoom.disable) {
        map.boxZoom.disable();
    }
    // Prevent browser context menu from interfering with right-drag panning
    if (map.getContainer()) {
        map.getContainer().addEventListener('contextmenu', function (ev) { ev.preventDefault(); });
    }

    let mouseButtonDown = null;
    let rightPanning = false;
    let lastPanPoint = null;
    let selectionStartPoint = null;
    let selectionStartLatLng = null;
    let selectionActive = false;
    let selectionBox = null;
    let selectionSubtract = false; // true when Shift is held on drag start
    const DRAG_THRESHOLD_PX = 5;

    map.on('mousedown', function (e) {
        mouseButtonDown = e.originalEvent && typeof e.originalEvent.button === 'number' ? e.originalEvent.button : null;
        if (mouseButtonDown === 2) {
            // Right button → start panning
            rightPanning = true;
            lastPanPoint = e.containerPoint;
            if (e.originalEvent && e.originalEvent.preventDefault) e.originalEvent.preventDefault();
        } else if (mouseButtonDown === 0) {
            // Left button → potential selection (activate after threshold)
            selectionStartPoint = e.containerPoint;
            selectionStartLatLng = e.latlng;
            selectionActive = false;
            selectionSubtract = !!(e.originalEvent && e.originalEvent.shiftKey);
        }
    });

    map.on('mousemove', function (e) {
        if (rightPanning && lastPanPoint) {
            const dx = lastPanPoint.x - e.containerPoint.x;
            const dy = lastPanPoint.y - e.containerPoint.y;
            if (dx !== 0 || dy !== 0) {
                map.panBy([dx, dy], { animate: false });
                lastPanPoint = e.containerPoint;
            }
        } else if (mouseButtonDown === 0 && selectionStartPoint) {
            const dist = Math.max(
                Math.abs(e.containerPoint.x - selectionStartPoint.x),
                Math.abs(e.containerPoint.y - selectionStartPoint.y)
            );
            if (!selectionActive && dist >= DRAG_THRESHOLD_PX) {
                selectionActive = true;
            }
            if (selectionActive) {
                const bounds = L.latLngBounds(selectionStartLatLng, e.latlng);
                if (!selectionBox) {
                    selectionBox = L.rectangle(bounds, { color: '#d69040', weight: 1, dashArray: '4', fillOpacity: 0.05, interactive: false });
                    selectionBox.addTo(map);
                } else {
                    selectionBox.setBounds(bounds);
                }
            }
        }
    });

    map.on('mouseup', function (e) {
        if (rightPanning && e.originalEvent && e.originalEvent.button === 2) {
            rightPanning = false;
            lastPanPoint = null;
        } else if (mouseButtonDown === 0) {
            if (selectionActive && selectionBox) {
                const box = selectionBox.getBounds();
                // Compute integer degree ranges covered by selection box
                const south = Math.floor(clamp(box.getSouth(), -90, 90));
                const north = Math.ceil(clamp(box.getNorth(), -90, 90));
                let west = Math.floor(clamp(box.getWest(), -180, 180));
                let east = Math.ceil(clamp(box.getEast(), -180, 180));
                let eastFor = east;
                if (east < west) {
                    eastFor = east + 360; // handle anti-meridian
                }
                for (let lat = south; lat < Math.min(north, 90); lat++) {
                    for (let lon = west; lon < eastFor; lon++) {
                        const normLon = normalizeLon(lon);
                        const key = cellKey(lat, normLon);
                        if (selectionSubtract) {
                            selectedCells.delete(key);
                        } else {
                            selectedCells.add(key);
                        }
                    }
                }
                // Clean up box
                map.removeLayer(selectionBox);
                selectionBox = null;
                updateSelectionUIVisibility();
                // Optional: expose selection globally
                if (typeof window !== 'undefined') {
                    window.SelectedLatLonTiles = Array.from(selectedCells);
                }
                // Refresh cells to reflect selection state
                rebuildGrid();
            }
            // Reset selection state
            selectionStartPoint = null;
            selectionStartLatLng = null;
            selectionActive = false;
        }
        mouseButtonDown = null;
    });

    function parseKey(key) {
        const parts = key.split(',');
        return { lat: parseInt(parts[0], 10), lon: parseInt(parts[1], 10) };
    }

    function applyLocalMaptypeToSelection(maptype) {
        if (!maptype) return;
        const tiles = Array.from(selectedCells).map(parseKey);
        if (!tiles.length) return;
        tiles.forEach(function (t) {
            const key = cellKey(t.lat, t.lon);
            pendingOverrides.set(key, maptype);
            tileMaptypeCache.set(key, maptype);
        });
        rebuildGrid();
    }

    // Handle dropdown changes: apply locally only
    function bindSelectHandler() {
        if (!selectionUI.select) return;
        selectionUI.select.onchange = function () {
            const maptype = selectionUI.select.value;
            if (!maptype) return;
            applyLocalMaptypeToSelection(maptype);
            selectionUI.select.selectedIndex = 0; // reset to placeholder
        };
    }
    setTimeout(bindSelectHandler, 0);

    // Expose pending overrides for host integration (Qt)
    if (typeof window !== 'undefined') {
        window.getPendingOverrides = function () {
            const arr = [];
            pendingOverrides.forEach(function (mt, key) {
                const parts = key.split(',');
                arr.push({ lat: parseInt(parts[0], 10), lon: parseInt(parts[1], 10), maptype: mt });
            });
            return arr;
        };
        window.clearPendingOverrides = function () {
            pendingOverrides.clear();
        };
    }

    function clamp(value, min, max) {
        return Math.max(min, Math.min(max, value));
    }

    function normalizeLon(lon) {
        // Keep lon within [-180, 180]
        let x = lon;
        while (x > 180) x -= 360;
        while (x < -180) x += 360;
        return x;
    }

    function cellKey(latDeg, lonDeg) {
        return latDeg + ',' + lonDeg;
    }

    function parseCoordTile(coord) {
        // Expect strings like '+12-123' or '-08+170'
        if (typeof coord !== 'string') return null;
        const m = coord.match(/^([+-]\d{2})([+-]\d{3})$/);
        if (!m) return null;
        const lat = parseInt(m[1], 10);
        const lon = parseInt(m[2], 10);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
        return [lat, lon];
    }

    function styleForCell(selected) {
        return {
            color: selected ? '#d69040' : '#6da4e3',
            weight: selected ? 3 : 1,
            opacity: 0.9,
            fillOpacity: selected ? 0.2 : 0.05,
            fillColor: selected ? '#d69040' : '#6da4e3'
        };
    }

    

    function rebuildGrid() {
        gridLinesLayer.clearLayers();
        gridCellsLayer.clearLayers();
        gridLabelsLayer.clearLayers();

        const b = map.getBounds();
        let south = Math.floor(clamp(b.getSouth(), -90, 90));
        let north = Math.ceil(clamp(b.getNorth(), -90, 90));
        let west = Math.floor(clamp(b.getWest(), -180, 180));
        let east = Math.ceil(clamp(b.getEast(), -180, 180));

        // Handle anti-meridian view (east < west)
        let eastForLines = east;
        if (east < west) {
            eastForLines = east + 360;
        }

        

        // Draw latitude lines (every integer degree)
        for (let lat = south; lat <= north; lat++) {
            const p1 = [lat, normalizeLon(west)];
            const p2 = [lat, normalizeLon(eastForLines)];
            L.polyline([p1, p2], { color: '#888', weight: 1, opacity: 0.5, interactive: false }).addTo(gridLinesLayer);
        }
        // Draw longitude lines
        for (let lon = west; lon <= eastForLines; lon++) {
            const p1 = [clamp(south, -90, 90), normalizeLon(lon)];
            const p2 = [clamp(north, -90, 90), normalizeLon(lon)];
            L.polyline([p1, p2], { color: '#888', weight: 1, opacity: 0.5, interactive: false }).addTo(gridLinesLayer);
        }

        // Draw clickable 1x1° cells for available tiles only
        if (map.getZoom() >= GRID_MIN_ZOOM_FOR_CELLS && availableTilesParsed.length) {
            const northCells = Math.min(north, 89); // cell upper bound limited to < 90
            const eastCells = eastForLines - 1; // [lon, lon+1]
            for (let i = 0; i < availableTilesParsed.length; i++) {
                const lat = availableTilesParsed[i][0];
                const lon = availableTilesParsed[i][1];
                if (lat < south || lat > northCells) continue;
                let lonCandidate = lon;
                if (east < west && lon < west) lonCandidate = lon + 360; // anti-meridian
                if (lonCandidate < west || lonCandidate > eastCells) continue;

                const ll = [lat, normalizeLon(lon)];
                const ur = [lat + 1, normalizeLon(lon + 1)];
                const key = cellKey(lat, normalizeLon(lon));
                const selected = selectedCells.has(key);
                const rect = L.rectangle([ll, ur], styleForCell(selected));
                rect.on('click', function () {
                    const wasSelected = selectedCells.has(key);
                    if (wasSelected) {
                        selectedCells.delete(key);
                    } else {
                        selectedCells.add(key);
                    }
                    rect.setStyle(styleForCell(!wasSelected));
                    if (typeof window !== 'undefined') {
                        window.SelectedLatLonTiles = Array.from(selectedCells);
                    }
                    updateSelectionUIVisibility();
                });
                rect.addTo(gridCellsLayer);

                // Add centered label for each 1x1° cell; text depends on mode
                const centerLat = lat + 0.5;
                const centerLon = normalizeLon(lon + 0.5);
                const pLL = map.latLngToLayerPoint(L.latLng(lat, normalizeLon(lon)));
                const pUR = map.latLngToLayerPoint(L.latLng(lat + 1, normalizeLon(lon + 1)));
                const wPx = Math.max(1, Math.abs(pUR.x - pLL.x));
                const hPx = Math.max(1, Math.abs(pUR.y - pLL.y));
                const defaultText = (currentMode === MODE_MAPTYPE) ? 'DFLT' : '\u2014';
                const labelMarker = L.marker([centerLat, centerLon], {
                    icon: L.divIcon({ className: 'grid-label', html: defaultText, iconSize: [wPx, hPx], iconAnchor: [wPx / 2, hPx / 2] }),
                    interactive: false
                }).addTo(gridLabelsLayer);
                if (currentMode === MODE_MAPTYPE) {
                    fetchTileMaptype(lat, normalizeLon(lon)).then(function (mt) {
                        labelMarker.setIcon(L.divIcon({ className: 'grid-label', html: mt, iconSize: [wPx, hPx], iconAnchor: [wPx / 2, hPx / 2] }));
                    });
                } else if (currentMode === MODE_CACHE) {
                    fetchTileCacheStatus(lat, normalizeLon(lon)).then(function (st) {
                        labelMarker.setIcon(L.divIcon({ className: 'grid-label', html: st, iconSize: [wPx, hPx], iconAnchor: [wPx / 2, hPx / 2] }));
                    });
                }
            }
        }
        updateSelectionUIVisibility();
    }

    // Debounce rebuilds
    let rebuildTimer = null;
    function scheduleRebuild() {
        if (rebuildTimer) {
            clearTimeout(rebuildTimer);
        }
        rebuildTimer = setTimeout(rebuildGrid, 100);
    }

    map.on('moveend', scheduleRebuild);
    map.on('zoomend', scheduleRebuild);
    rebuildGrid();

    // ---- Aircraft tracking --------------------------------------------------
    function track_latlon(lat, lon) {
        line.addLatLng([lat, lon]);
        marker.setLatLng([lat, lon]);
        if (recenter_call) {
            map.setView([lat, lon], map.getZoom());
            recenter_call = false;
        }
    }

    function startSocket() {
        const base = (typeof window !== 'undefined' && window.SOCKET_URL) ? window.SOCKET_URL : '/';
        const socket = io(base);

        socket.on('connect', function () {
            socket.emit('handle_latlon');
        });

        socket.on('latlon', function (data) {
            if (!data) return;
            const lat = data.lat;
            const lon = data.lon;
            if (typeof lat === 'number' && typeof lon === 'number') {
                track_latlon(lat, lon);
            }
        });
    }

    startSocket();
})();


