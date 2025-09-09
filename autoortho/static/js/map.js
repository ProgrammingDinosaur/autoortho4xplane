// Leaflet map initialization and Socket.IO integration
(function () {
    const map = L.map('map', 
        {minZoom: 6, maxZoom: 18}
    ).setView([51.505, -0.09], 6);
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
    const GRID_MIN_ZOOM_FOR_CELLS = 6; // require zoom-in for clickable 1x1° cells
    const tileMaptypeCache = new Map(); // key: "lat,lon" -> maptype string
    const availableMaptypes = new Array(); // list of maptypes
    const selectionUI = { container: null, select: null, applyBtn: null };

    let recenter_call = true;

    fetchAvailableMaptypes().then(function (maptypes) {
        maptypes.forEach(function (mt) {
            availableMaptypes.push(mt);
        });
        populateDropdownOptions();
    });

    // Inject minimal styles for grid cell labels
    if (typeof document !== 'undefined' && document.head) {
        const styleEl = document.createElement('style');
        styleEl.textContent = '.grid-label{color:#1d71d1;font:12px/1.2 Arial,Helvetica,sans-serif;font-weight:bold;text-shadow:0 0 2px rgba(255,255,255,0.9),0 0 4px rgba(255,255,255,0.6);transform: translate(-50%,-50%);pointer-events:none;white-space:nowrap;}.maptype-control{background:rgba(255,255,255,0.95);padding:6px 8px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,0.2);}.maptype-select{font:12px Arial,Helvetica,sans-serif;min-width:120px;}';
        document.head.appendChild(styleEl);
    }

    // Create a Leaflet control with dropdown for selected tiles
    const SelectionControl = L.Control.extend({
        onAdd: function () {
            const container = L.DomUtil.create('div', 'maptype-control');
            container.style.display = 'none';
            const select = L.DomUtil.create('select', 'maptype-select', container);
            const btn = L.DomUtil.create('button', '', container);
            btn.type = 'button';
            btn.textContent = 'Change Maptype on selected tiles';
            btn.style.marginLeft = '8px';
            btn.style.font = '12px Arial,Helvetica,sans-serif';
            selectionUI.container = container;
            selectionUI.select = select;
            selectionUI.applyBtn = btn;
            // Prevent map interactions while using the control
            L.DomEvent.disableClickPropagation(container);
            return container;
        }
    });
    const selectionControl = new SelectionControl({ position: 'topright' });
    selectionControl.addTo(map);

    async function fetchTileMaptype(latDeg, lonDeg) {
        const key = cellKey(latDeg, lonDeg);
        if (tileMaptypeCache.has(key)) return tileMaptypeCache.get(key);
        try {
            const resp = await fetch(`/tile_maptype?lat=${latDeg}&lon=${lonDeg}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const mt = (data && data.maptype) ? data.maptype : 'BI';
            tileMaptypeCache.set(key, mt);
            return mt;
        } catch (_e) {
            tileMaptypeCache.set(key, 'BI');
            return 'BI';
        }
    }

    async function fetchAvailableMaptypes() {
        const resp = await fetch('/available_maptypes');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        return data.maptypes;
    }

    function populateDropdownOptions() {
        if (!selectionUI.select) return;
        while (selectionUI.select.firstChild) {
            selectionUI.select.removeChild(selectionUI.select.firstChild);
        }
        availableMaptypes.forEach(function (mt) {
            const opt = document.createElement('option');
            opt.value = mt;
            opt.textContent = mt;
            selectionUI.select.appendChild(opt);
        });
        const idx = availableMaptypes.indexOf('BI');
        if (idx >= 0) selectionUI.select.selectedIndex = idx;
    }

    function updateSelectionUIVisibility() {
        if (!selectionUI.container) return;
        selectionUI.container.style.display = selectedCells.size > 0 ? 'block' : 'none';
    }

    function parseKey(key) {
        const parts = key.split(',');
        return { lat: parseInt(parts[0], 10), lon: parseInt(parts[1], 10) };
    }

    async function applyMaptypeToSelection() {
        if (!selectionUI.select) return;
        const maptype = selectionUI.select.value;
        if (!maptype) return;
        const tiles = Array.from(selectedCells).map(parseKey);
        if (!tiles.length) return;
        try {
            const resp = await fetch('/change_maptypes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ maptype: maptype, tiles: tiles })
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            // Update local cache and labels
            tiles.forEach(function (t) {
                const key = cellKey(t.lat, t.lon);
                tileMaptypeCache.set(key, maptype);
            });
            rebuildGrid();
        } catch (_e) {
            // no-op; could show toast
        }
    }

    // Bind button handler once control exists in DOM
    setTimeout(function () {
        if (selectionUI.applyBtn) {
            selectionUI.applyBtn.addEventListener('click', applyMaptypeToSelection);
        }
    }, 0);

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

    function styleForCell(selected) {
        return {
            color: selected ? '#1d71d1' : '#6da4e3',
            weight: selected ? 2 : 1,
            opacity: 0.9,
            fillOpacity: selected ? 0.2 : 0.05,
            fillColor: selected ? '#1d71d1' : '#6da4e3'
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

        // Draw clickable 1x1° cells if sufficiently zoomed in
        if (map.getZoom() >= GRID_MIN_ZOOM_FOR_CELLS) {
            const northCells = Math.min(north, 89); // cell upper bound limited to < 90
            const eastCells = eastForLines - 1; // we use [lon, lon+1]
            for (let lat = south; lat <= northCells; lat++) {
                for (let lon = west; lon <= eastCells; lon++) {
                    const ll = [lat, normalizeLon(lon)];
                    const ur = [lat + 1, normalizeLon(lon + 1)];
                    // Skip degenerate rectangles possibly created at poles
                    if (ll[0] >= 90 || ur[0] <= -90) continue;
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
                        // Optional: expose selection globally
                        if (typeof window !== 'undefined') {
                            window.SelectedLatLonTiles = Array.from(selectedCells);
                        }
                        updateSelectionUIVisibility();
                    });
                    rect.addTo(gridCellsLayer);

                    // Add centered label for each 1x1° cell (default 'BI' then update)
                    const centerLat = lat + 0.5;
                    const centerLon = normalizeLon(lon + 0.5);
                    const labelMarker = L.marker([centerLat, centerLon], {
                        icon: L.divIcon({ className: 'grid-label', html: 'BI' }),
                        interactive: false
                    }).addTo(gridLabelsLayer);
                    fetchTileMaptype(lat, normalizeLon(lon)).then(function (mt) {
                        labelMarker.setIcon(L.divIcon({ className: 'grid-label', html: mt }));
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


