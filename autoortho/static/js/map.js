// Leaflet map initialization and Socket.IO integration
(function () {
    const map = L.map('map').setView([51.505, -0.09], 5);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors'
    }).addTo(map);

    const marker = L.marker([51.5, -0.09]).addTo(map);
    const line = L.polyline([]).addTo(map);

    // ---- Grid overlay ------------------------------------------------------
    const gridLinesLayer = L.layerGroup().addTo(map);
    const gridCellsLayer = L.layerGroup().addTo(map);
    const selectedCells = new Set();
    const GRID_MIN_ZOOM_FOR_CELLS = 6; // require zoom-in for clickable 1x1° cells

    let recenter_call = true;

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
                    });
                    rect.addTo(gridCellsLayer);
                }
            }
        }
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


