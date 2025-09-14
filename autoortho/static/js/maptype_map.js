// Leaflet map for Maptype Manager (maptype-only logic)
(function () {
    const map = L.map('map', {minZoom: 5, maxZoom: 18}).setView([51.505, -0.09], 5);
    const BASE_TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    const BASE_TILE_ATTR = '&copy; OpenStreetMap contributors';
    let baseLayer = L.tileLayer(BASE_TILE_URL, { attribution: BASE_TILE_ATTR }).addTo(map);

    // Layers and state
    const gridLinesLayer = L.layerGroup().addTo(map);
    const gridCellsLayer = L.layerGroup().addTo(map);
    const gridLabelsLayer = L.layerGroup().addTo(map);
    const selectedCells = new Set();
    const GRID_MIN_ZOOM_FOR_CELLS = 5;
    const tileMaptypeCache = new Map(); // key: "lat,lon" -> maptype
    const availableMaptypes = new Array();
    const selectionUI = { container: null, select: null };
    const pendingOverrides = new Map(); // key: "lat,lon" -> maptype
    let flushTimer = null;

    let recenter_call = true;
    let availableTilesParsed = [];
    const availableTilesMap = new Map();

    // Styles
    if (typeof document !== 'undefined' && document.head) {
        const styleEl = document.createElement('style');
        styleEl.textContent = '.grid-label{color:#1d71d1;font:12px/1.2 Arial,Helvetica,sans-serif;font-weight:bold;text-shadow:0 0 2px rgba(255,255,255,0.9),0 0 4px rgba(255,255,255,0.6);pointer-events:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;justify-content:center;width:100%;height:100%;padding:0 2px;}.maptype-control{background:rgba(255,255,255,0.95);padding:6px 8px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,0.2);}.maptype-select{font:12px Arial,Helvetica,sans-serif;min-width:220px;}';
        document.head.appendChild(styleEl);
        const styleEl2 = document.createElement('style');
        styleEl2.textContent = '.ao-help-btn{width:34px;height:34px;border-radius:50%;background:#1d71d1;color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 2px 6px rgba(0,0,0,0.25);font:14px/1 Arial,Helvetica,sans-serif;font-weight:bold}.ao-help-btn:hover{background:#5183bd}.ao-help-panel{position:fixed;top:0;right:0;height:100%;width:360px;max-width:85vw;background:#ffffff;color:#111;transform:translateX(100%);transition:transform .25s ease-in-out, box-shadow .25s ease-in-out;z-index:4000;box-shadow:0 0 0 rgba(0,0,0,0)}.ao-help-panel.open{transform:translateX(0);box-shadow:-2px 0 12px rgba(0,0,0,0.25)}.ao-help-panel .ao-help-header{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #e5e5e5;background:#f8f8f8}.ao-help-panel .ao-help-title{margin:0;font:600 16px/1.2 Arial,Helvetica,sans-serif;color:#1d71d1}.ao-help-panel .ao-help-close{border:none;background:transparent;font:700 18px/1 Arial,Helvetica,sans-serif;color:#666;cursor:pointer}.ao-help-panel .ao-help-close:hover{color:#000}.ao-help-panel .ao-help-content{padding:14px;overflow:auto;height:calc(100% - 50px)}.ao-help-panel .ao-help-content h2{font:600 18px/1.2 Arial,Helvetica,sans-serif;color:#333;margin:10px 0}.ao-help-panel .ao-help-content p{margin:8px 0 12px;color:#333}.ao-help-panel .ao-help-content ul{padding-left:18px;margin:8px 0 12px}.ao-help-panel .ao-help-content code{background:#f0f3f7;color:#0b3d91;padding:2px 4px;border-radius:3px;font-family:Menlo,Consolas,monospace;font-size:12px}.ao-help-panel .ao-help-content pre{background:#0b0f14;color:#e3e9f3;padding:10px;border-radius:6px;overflow:auto}';
        document.head.appendChild(styleEl2);
    }

    // Selection control (maptype dropdown)
    const SelectionControl = L.Control.extend({
        onAdd: function () {
            const container = L.DomUtil.create('div', 'maptype-control');
            container.style.display = 'none';
            const select = L.DomUtil.create('select', 'maptype-select', container);
            selectionUI.container = container;
            selectionUI.select = select;
            L.DomEvent.disableClickPropagation(container);
            return container;
        }
    });
    const selectionControl = new SelectionControl({ position: 'topright' });
    selectionControl.addTo(map);

    // Help
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
            '  <h2>Changing the maptype</h2>'+
            '  <p>Select tiles, then choose a maptype from the dropdown. Changes apply when you click Apply in the app.</p>'+
            '</div>';
        document.body.appendChild(helpPanel);
        const closeBtn = helpPanel.querySelector('.ao-help-close');
        if (closeBtn) closeBtn.addEventListener('click', function () { toggleHelp(false); });
        document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') toggleHelp(false); });
    }
    function toggleHelp(open) {
        ensureHelpPanel();
        if (!helpPanel) return;
        if (open === undefined) helpPanel.classList.toggle('open');
        else if (open) helpPanel.classList.add('open');
        else helpPanel.classList.remove('open');
    }
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

    // APIs
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
        selectionUI.container.style.display = selectedCells.size > 0 ? 'block' : 'none';
    }

    // Map interactions
    map.dragging.disable();
    if (map.boxZoom && map.boxZoom.disable) map.boxZoom.disable();
    if (map.doubleClickZoom && map.doubleClickZoom.disable) map.doubleClickZoom.disable();
    if (map.getContainer()) map.getContainer().addEventListener('contextmenu', function (ev) { ev.preventDefault(); });

    let mouseButtonDown = null;
    let rightPanning = false;
    let lastPanPoint = null;
    let selectionStartPoint = null;
    let selectionStartLatLng = null;
    let selectionActive = false;
    let selectionBox = null;
    let selectionSubtract = false;
    const DRAG_THRESHOLD_PX = 5;

    map.on('mousedown', function (e) {
        mouseButtonDown = e.originalEvent && typeof e.originalEvent.button === 'number' ? e.originalEvent.button : null;
        if (mouseButtonDown === 2) {
            rightPanning = true;
            lastPanPoint = e.containerPoint;
            if (e.originalEvent && e.originalEvent.preventDefault) e.originalEvent.preventDefault();
        } else if (mouseButtonDown === 0) {
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
            if (!selectionActive && dist >= DRAG_THRESHOLD_PX) selectionActive = true;
            if (selectionActive) {
                const bounds = L.latLngBounds(selectionStartLatLng, e.latlng);
                if (!selectionBox) {
                    selectionBox = L.rectangle(bounds, { color: '#d69040', weight: 1, dashArray: '4', fillOpacity: 0.05, interactive: false });
                    selectionBox.addTo(map);
                } else selectionBox.setBounds(bounds);
            }
        }
    });
    map.on('mouseup', function (e) {
        if (rightPanning && e.originalEvent && e.originalEvent.button === 2) {
            rightPanning = false; lastPanPoint = null;
        } else if (mouseButtonDown === 0) {
            if (selectionActive && selectionBox) {
                const box = selectionBox.getBounds();
                const south = Math.floor(clamp(box.getSouth(), -90, 90));
                const north = Math.ceil(clamp(box.getNorth(), -90, 90));
                let west = Math.floor(clamp(box.getWest(), -180, 180));
                let east = Math.ceil(clamp(box.getEast(), -180, 180));
                let eastFor = east; if (east < west) eastFor = east + 360;
                for (let lat = south; lat < Math.min(north, 90); lat++) {
                    for (let lon = west; lon < eastFor; lon++) {
                        const normLon = normalizeLon(lon);
                        const key = cellKey(lat, normLon);
                        if (selectionSubtract) selectedCells.delete(key);
                        else selectedCells.add(key);
                    }
                }
                map.removeLayer(selectionBox); selectionBox = null;
                updateSelectionUIVisibility();
                if (typeof window !== 'undefined') window.SelectedLatLonTiles = Array.from(selectedCells);
                rebuildGrid();
            }
            selectionStartPoint = null; selectionStartLatLng = null; selectionActive = false;
        }
        mouseButtonDown = null;
    });

    function parseKey(key) { const parts = key.split(','); return { lat: parseInt(parts[0], 10), lon: parseInt(parts[1], 10) }; }
    function applyLocalMaptypeToSelection(maptype) {
        if (!maptype) return; const tiles = Array.from(selectedCells).map(parseKey); if (!tiles.length) return;
        tiles.forEach(function (t) { const key = cellKey(t.lat, t.lon); pendingOverrides.set(key, maptype); tileMaptypeCache.set(key, maptype); });
        rebuildGrid();
    }
    function bindSelectHandler() {
        if (!selectionUI.select) return; selectionUI.select.onchange = function () {
            const maptype = selectionUI.select.value; if (!maptype) return;
            applyLocalMaptypeToSelection(maptype);
            selectionUI.select.selectedIndex = 0;
        };
    }
    setTimeout(bindSelectHandler, 0);

    if (typeof window !== 'undefined') {
        window.getPendingOverrides = function () {
            const arr = []; pendingOverrides.forEach(function (mt, key) { const parts = key.split(','); arr.push({ lat: parseInt(parts[0], 10), lon: parseInt(parts[1], 10), maptype: mt }); }); return arr;
        };
        window.clearPendingOverrides = function () { pendingOverrides.clear(); };
    }

    function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
    function normalizeLon(lon) { let x = lon; while (x > 180) x -= 360; while (x < -180) x += 360; return x; }
    function cellKey(latDeg, lonDeg) { return latDeg + ',' + lonDeg; }
    function parseCoordTile(coord) { if (typeof coord !== 'string') return null; const m = coord.match(/^([+-]\d{2})([+-]\d{3})$/); if (!m) return null; const lat = parseInt(m[1], 10); const lon = parseInt(m[2], 10); if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null; return [lat, lon]; }
    function styleForCell(selected, isMain) {
        if (isMain) { return { color: '#a65a14', weight: 5, opacity: 0.95, fillOpacity: 0.35, fillColor: '#a65a14' }; }
        return { color: selected ? '#d69040' : '#6da4e3', weight: selected ? 3 : 1, opacity: 0.9, fillOpacity: selected ? 0.2 : 0.05, fillColor: selected ? '#d69040' : '#6da4e3' };
    }

    // Info box (optional)
    const infoBoxUI = { container: null, title: null, maptype: null, cache: null };
    if (typeof document !== 'undefined') {
        const mapContainer = map.getContainer(); const info = document.createElement('div');
        info.className = 'main-info';
        info.innerHTML = ''+
            '<h4 class="mi-title">Tile</h4>'+
            '<div class="row mi-maptype">Maptype: <span class="v">\u2014</span></div>'+
            '<div class="row mi-cache">Cache: <span class="v">\u2014</span></div>';
        mapContainer.appendChild(info);
        infoBoxUI.container = info; infoBoxUI.title = info.querySelector('.mi-title');
        infoBoxUI.maptype = info.querySelector('.mi-maptype .v'); infoBoxUI.cache = info.querySelector('.mi-cache .v');
    }
    function showMainInfo(key, lat, lon) {
        if (!infoBoxUI.container) return; infoBoxUI.title.textContent = 'Tile ' + lat + ',' + lon; infoBoxUI.maptype.textContent = '\u2014'; infoBoxUI.cache.textContent = '\u2014'; infoBoxUI.container.style.display = 'block';
        fetchTileMaptype(lat, lon).then(function (mt) { infoBoxUI.maptype.textContent = mt; });
    }
    function hideMainInfo() { if (!infoBoxUI.container) return; infoBoxUI.container.style.display = 'none'; }

    // Build grid
    function rebuildGrid() {
        gridLinesLayer.clearLayers(); gridCellsLayer.clearLayers(); gridLabelsLayer.clearLayers();
        const b = map.getBounds(); let south = Math.floor(clamp(b.getSouth(), -90, 90)); let north = Math.ceil(clamp(b.getNorth(), -90, 90)); let west = Math.floor(clamp(b.getWest(), -180, 180)); let east = Math.ceil(clamp(b.getEast(), -180, 180));
        let eastForLines = east; if (east < west) eastForLines = east + 360;
        for (let lat = south; lat <= north; lat++) { const p1 = [lat, normalizeLon(west)]; const p2 = [lat, normalizeLon(eastForLines)]; L.polyline([p1, p2], { color: '#888', weight: 1, opacity: 0.5, interactive: false }).addTo(gridLinesLayer); }
        for (let lon = west; lon <= eastForLines; lon++) { const p1 = [clamp(south, -90, 90), normalizeLon(lon)]; const p2 = [clamp(north, -90, 90), normalizeLon(lon)]; L.polyline([p1, p2], { color: '#888', weight: 1, opacity: 0.5, interactive: false }).addTo(gridLinesLayer); }
        if (map.getZoom() >= GRID_MIN_ZOOM_FOR_CELLS && availableTilesParsed.length) {
            const northCells = Math.min(north, 89); const eastCells = eastForLines - 1;
            for (let i = 0; i < availableTilesParsed.length; i++) {
                const lat = availableTilesParsed[i].lat; const lon = availableTilesParsed[i].lon;
                if (lat < south || lat > northCells) continue; let lonCandidate = lon; if (east < west && lon < west) lonCandidate = lon + 360; if (lonCandidate < west || lonCandidate > eastCells) continue;
                const ll = [lat, normalizeLon(lon)]; const ur = [lat + 1, normalizeLon(lon + 1)]; const key = availableTilesParsed[i].key || cellKey(lat, normalizeLon(lon)); const selected = selectedCells.has(key);
                const rect = L.rectangle([ll, ur], styleForCell(selected, false));
                rect.on('click', function () {
                    if (rect._clickTimeout) clearTimeout(rect._clickTimeout);
                    rect._clickTimeout = setTimeout(function () {
                        const wasSelected = selectedCells.has(key);
                        if (wasSelected) selectedCells.delete(key); else selectedCells.add(key);
                        rect.setStyle(styleForCell(!wasSelected, false));
                        if (typeof window !== 'undefined') window.SelectedLatLonTiles = Array.from(selectedCells);
                        updateSelectionUIVisibility(); rect._clickTimeout = null;
                    }, 220);
                });
                rect.on('dblclick', function (ev) {
                    if (rect._clickTimeout) { clearTimeout(rect._clickTimeout); rect._clickTimeout = null; }
                    const thisLat = lat; const thisLon = normalizeLon(lon);
                    showMainInfo(key, thisLat, thisLon);
                    if (ev && ev.originalEvent && ev.originalEvent.preventDefault) ev.originalEvent.preventDefault(); if (ev && ev.originalEvent && ev.originalEvent.stopPropagation) ev.originalEvent.stopPropagation();
                });
                rect.addTo(gridCellsLayer);

                const centerLat = lat + 0.5; const centerLon = normalizeLon(lon + 0.5);
                const pLL = map.latLngToLayerPoint(L.latLng(lat, normalizeLon(lon))); const pUR = map.latLngToLayerPoint(L.latLng(lat + 1, normalizeLon(lon + 1)));
                const wPx = Math.max(1, Math.abs(pUR.x - pLL.x)); const hPx = Math.max(1, Math.abs(pUR.y - pLL.y));
                const labelMarker = L.marker([centerLat, centerLon], { icon: L.divIcon({ className: 'grid-label', html: 'DFLT', iconSize: [wPx, hPx], iconAnchor: [wPx / 2, hPx / 2] }), interactive: false }).addTo(gridLabelsLayer);
                fetchTileMaptype(lat, normalizeLon(lon)).then(function (mt) { labelMarker.setIcon(L.divIcon({ className: 'grid-label', html: mt, iconSize: [wPx, hPx], iconAnchor: [wPx / 2, hPx / 2] })); });
            }
        }
        updateSelectionUIVisibility();
    }

    // Debounce
    let rebuildTimer = null; function scheduleRebuild() { if (rebuildTimer) clearTimeout(rebuildTimer); rebuildTimer = setTimeout(rebuildGrid, 100); }
    map.on('moveend', scheduleRebuild); map.on('zoomend', scheduleRebuild);

    // Load resources
    (function () {
        fetch('/available_tiles')
            .then(function (resp) { if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); })
            .then(function (data) {
                const tilesObj = (data && data.tiles && typeof data.tiles === 'object') ? data.tiles : {};
                availableTilesParsed = []; availableTilesMap.clear();
                try { for (const coord in tilesObj) { if (!Object.prototype.hasOwnProperty.call(tilesObj, coord)) continue; const parsed = parseCoordTile(coord); if (!parsed) continue; const lat = parsed[0]; const lonRaw = parsed[1]; const lon = normalizeLon(lonRaw); const k = cellKey(lat, lon); const info = tilesObj[coord] || {}; availableTilesMap.set(k, info); availableTilesParsed.push({ lat: lat, lon: lon, key: k, info: info }); } } catch (_e) {}
                scheduleRebuild();
            }).catch(function () {});
    })();
    fetch('/available_maptypes').then(function (resp) { if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); }).then(function (data) { (data.maptypes || []).forEach(function (mt) { availableMaptypes.push(mt); }); populateDropdownOptions(); }).catch(function () {});

    // Selection dropdown visibility
    setTimeout(bindSelectHandler, 0);

    // Aircraft tracking

    function startSocket() {
        const base = (typeof window !== 'undefined' && window.SOCKET_URL) ? window.SOCKET_URL : '/'; const socket = io(base);
        socket.on('connect', function () { socket.emit('handle_latlon'); });    }
    startSocket();

    // Memory flush API
    function flushMapMemory() {
        try { gridLinesLayer.clearLayers(); gridCellsLayer.clearLayers(); gridLabelsLayer.clearLayers(); } catch (_e) {}
        try { if (baseLayer && baseLayer._tiles) { Object.keys(baseLayer._tiles).forEach(function (k) { delete baseLayer._tiles[k]; }); } } catch (_e2) {}
        try { tileMaptypeCache.clear(); } catch (_e3) {}
        try { pendingOverrides.clear(); } catch (_e4) {}
    }
    function scheduleFlushIn(ms) { if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; } flushTimer = setTimeout(function () { flushMapMemory(); }, Math.max(0, ms || 60000)); }
    function cancelFlush() { if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; } }
    if (typeof window !== 'undefined') { window.AOMap = window.AOMap || {}; window.AOMap.flushNow = flushMapMemory; window.AOMap.flushIn = scheduleFlushIn; window.AOMap.cancelFlush = cancelFlush; window.AOMap.reloadTiles = function () { location.reload(); }; }

    // Initial build
    rebuildGrid();
})();


