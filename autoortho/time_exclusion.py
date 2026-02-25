#!/usr/bin/env python3

"""
Sun-Position Exclusion Manager for AutoOrtho.

This module provides sun-position-based exclusion functionality that allows
AutoOrtho to automatically disable orthophoto scenery at night and fall back
to X-Plane's default scenery (which includes night lighting).

The exclusion decision is based on the sun's elevation angle (sun pitch),
obtained from X-Plane's sim/graphics/scenery/sun_pitch_degrees dataref.
This approach is robust against time acceleration, manual time changes,
seasons, and latitudes.

Hysteresis is used to prevent rapid toggling during twilight:
- Switch to night (exclusion) when sun drops below night_threshold (-12°)
- Switch to day (ortho) when sun rises above day_threshold (-10°)

The manager ensures safe transitions by:
- Tracking which DSF files are currently in use
- Not redirecting DSFs that are actively being read
- Only applying exclusions to new DSF accesses

Key design principle: NEVER hide DSF files from X-Plane. X-Plane indexes DSF
files at flight load time. If we hide files, X-Plane finds no terrain data.
Instead, we redirect reads to global scenery DSF files.

Decision Preservation During Temporary Disconnections:
------------------------------------------------------
Once a decision has been made based on valid sun pitch data, that decision
is preserved across temporary disconnections (e.g., scenery reload). This
prevents exclusion from being incorrectly re-activated during reloads when
the actual sun position indicated it should be inactive.

Usage:
    from time_exclusion import time_exclusion_manager

    # Check if a DSF should be redirected to global scenery
    redirect_path = time_exclusion_manager.get_redirect_path(path)
    if redirect_path:
        # Use redirect_path instead of original path for all file operations
        pass

    # Register DSF usage (call when DSF is opened)
    time_exclusion_manager.register_dsf_open(path)

    # Unregister DSF usage (call when DSF is closed)
    time_exclusion_manager.register_dsf_close(path)
"""

import os
import re
import threading
import time
import logging

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

log = logging.getLogger(__name__)


def _parse_dsf_coordinates(path):
    """
    Parse a DSF file path to extract the tile coordinates.
    
    DSF files are named like: +40-120.dsf, -12+045.dsf, etc.
    The path structure is: .../Earth nav data/<folder>/<filename>.dsf
    where folder is like +40-120 (10-degree grid) and filename is the 1-degree tile.
    
    Args:
        path: Path to DSF file (can be FUSE virtual path like /Earth nav data/+40-120/+45-118.dsf)
        
    Returns:
        tuple: (folder_name, filename) e.g. ("+40-120", "+45-118.dsf") or (None, None) if parsing fails
    """
    if path is None:
        return None, None
    
    # Normalize path separators
    normalized = path.replace('\\', '/')
    
    # Match pattern: /Earth nav data/<folder>/<filename>.dsf
    # folder is like +40-120, filename is like +45-118.dsf
    match = re.search(r'/Earth nav data/([-+]\d+[-+]\d+)/([-+]\d+[-+]\d+\.dsf)$', normalized, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    
    # Also try matching just the filename pattern for relative paths
    match = re.search(r'([-+]\d+[-+]\d+)/([-+]\d+[-+]\d+\.dsf)$', normalized, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    
    return None, None


def _find_global_scenery_dsf(folder, filename):
    """
    Find the corresponding global scenery DSF file for a given tile.
    
    Searches in order:
    1. X-Plane 12 Global Scenery
    2. X-Plane 12 Demo Areas
    
    Args:
        folder: The Earth nav data folder (e.g., "+40-120")
        filename: The DSF filename (e.g., "+45-118.dsf")
        
    Returns:
        str: Full path to the global scenery DSF, or None if not found
    """
    xplane_path = getattr(CFG.paths, 'xplane_path', '')
    if not xplane_path:
        log.warning("X-Plane path not configured, cannot find global scenery")
        return None
    
    # Primary location: X-Plane 12 Global Scenery
    global_path = os.path.join(
        xplane_path, "Global Scenery", "X-Plane 12 Global Scenery",
        "Earth nav data", folder, filename
    )
    if os.path.exists(global_path):
        log.debug(f"Found global scenery DSF: {global_path}")
        return global_path
    
    # Fallback: X-Plane 12 Demo Areas
    demo_path = os.path.join(
        xplane_path, "Global Scenery", "X-Plane 12 Demo Areas",
        "Earth nav data", folder, filename
    )
    if os.path.exists(demo_path):
        log.debug(f"Found demo area DSF: {demo_path}")
        return demo_path
    
    log.debug(f"No global scenery DSF found for {folder}/{filename}")
    return None


class TimeExclusionManager:
    """
    Manages sun-position-based exclusion of AutoOrtho scenery.
    
    This class monitors the sun's elevation angle via X-Plane's
    sun_pitch_degrees dataref and determines when AutoOrtho's DSF files
    should be redirected to X-Plane's global scenery (night mode).
    
    Hysteresis prevents rapid toggling during twilight transitions.
    """
    
    def __init__(self):
        """Initialize the time exclusion manager."""
        self._lock = threading.RLock()
        
        # Track DSF files currently in use (path -> open count)
        self._active_dsfs = {}
        
        # Cache for exclusion state
        self._exclusion_active = False
        self._last_check_time = 0
        self._check_interval = 1.0  # Check every second
        
        # Sun position mode state
        # True = ortho enabled (day mode), False = exclusion active (night mode)
        self._sun_mode_ortho_enabled = True  # Start in day mode (ortho enabled)
        self._sun_decision_made = False  # True once we've received valid sun pitch
        self._last_sun_pitch = -999.0  # Last sun pitch value (for logging)
        
        # Reference to dataref tracker (set later to avoid circular import)
        self._dataref_tracker = None
        
        # Background monitoring thread
        self._monitor_thread = None
        self._monitor_running = False
        
        log.info("TimeExclusionManager initialized")
    
    def set_dataref_tracker(self, tracker):
        """
        Set the dataref tracker reference and start background monitoring.
        
        Args:
            tracker: DatarefTracker instance
        """
        self._dataref_tracker = tracker
        self._start_monitor()
    
    def _start_monitor(self):
        """Start the background monitoring thread."""
        if self._monitor_running:
            return
        
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="TimeExclusionMonitor"
        )
        self._monitor_thread.start()
        log.debug("Time exclusion monitor thread started")
    
    def _monitor_loop(self):
        """
        Background loop that periodically checks exclusion state.
        
        This ensures the state is updated even when no DSF files are
        being accessed (e.g., when exclusion is active and hiding all DSFs).
        """
        import time as time_module
        
        while self._monitor_running:
            try:
                enabled = getattr(CFG.time_exclusion, 'enabled', False)
                if enabled:
                    # Trigger a state check (this updates the cache and logs changes)
                    self.is_exclusion_active()
            except Exception as e:
                log.debug(f"Time exclusion monitor error: {e}")
            
            # Sleep for the check interval
            time_module.sleep(self._check_interval)
    
    def stop(self):
        """Stop the background monitoring thread."""
        self._monitor_running = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)
        log.debug("Time exclusion monitor stopped")
    
    def _check_exclusion_state(self):
        """
        Check if exclusion should be active based on sun position.
        
        Uses hysteresis to prevent rapid toggling during twilight:
        - Switch to night (exclusion) when sun drops below night_threshold
        - Switch to day (ortho) when sun rises above day_threshold
        
        The sun pitch is the angle of the sun above/below the horizon:
        - Positive: sun above horizon (daytime)
        - Zero: sun at horizon (sunrise/sunset)
        - Negative: sun below horizon (twilight/night)
          - < -6°: civil twilight ends
          - < -12°: nautical twilight ends (default night threshold)
          - < -18°: astronomical twilight ends (full night)
        
        Returns:
            bool: True if exclusion should be active (night mode)
        """
        enabled = getattr(CFG.time_exclusion, 'enabled', False)
        if not enabled:
            return False
        
        default_to_exclusion = getattr(
            CFG.time_exclusion, 'default_to_exclusion', False
        )
        
        if self._dataref_tracker is None:
            return default_to_exclusion
        
        sun_pitch = self._dataref_tracker.get_sun_pitch()
        
        # Check for invalid sun_pitch (sentinel value -999 or out of valid range)
        if sun_pitch < -90 or sun_pitch > 90:
            # Preserve last decision during temporary unavailability
            if self._sun_decision_made:
                log.debug(
                    f"Sun pitch unavailable - preserving last decision: "
                    f"ortho={'enabled' if self._sun_mode_ortho_enabled else 'disabled'} "
                    f"(last sun pitch was {self._last_sun_pitch:.1f}°)"
                )
                return not self._sun_mode_ortho_enabled
            # No previous decision - use default
            return default_to_exclusion
        
        # Get thresholds from config
        night_threshold = getattr(
            CFG.time_exclusion, 'sun_night_threshold', -12.0
        )
        day_threshold = getattr(
            CFG.time_exclusion, 'sun_day_threshold', -10.0
        )
        
        # Hysteresis logic
        if self._sun_mode_ortho_enabled:
            # Currently showing ortho (day mode)
            if sun_pitch < night_threshold:
                self._sun_mode_ortho_enabled = False  # Switch to exclusion (night)
                log.info(
                    f"Sun exclusion ACTIVATED: sun pitch {sun_pitch:.1f}° < "
                    f"{night_threshold}° threshold - "
                    f"DSF reads will redirect to global scenery"
                )
        else:
            # Currently in exclusion (night mode)
            if sun_pitch > day_threshold:
                self._sun_mode_ortho_enabled = True  # Switch to ortho (day)
                log.info(
                    f"Sun exclusion DEACTIVATED: sun pitch {sun_pitch:.1f}° > "
                    f"{day_threshold}° threshold - "
                    f"DSF reads will use AutoOrtho scenery"
                )
        
        self._sun_decision_made = True
        self._last_sun_pitch = sun_pitch
        
        # exclusion_active = NOT ortho_enabled
        return not self._sun_mode_ortho_enabled
    
    def is_exclusion_active(self):
        """
        Check if sun-position exclusion is currently active.
        
        This method caches the result and only rechecks periodically
        for performance.
        
        Returns:
            bool: True if exclusion is active
        """
        current_time = time.time()
        
        # Use cached result if recent
        if current_time - self._last_check_time < self._check_interval:
            return self._exclusion_active
        
        with self._lock:
            # Double-check after acquiring lock
            if current_time - self._last_check_time < self._check_interval:
                return self._exclusion_active
            
            self._exclusion_active = self._check_exclusion_state()
            self._last_check_time = current_time
            
            return self._exclusion_active
    
    def register_dsf_open(self, path):
        """
        Register that a DSF file has been opened.
        
        This is called when X-Plane opens a DSF file. The DSF will not
        be hidden while it's registered as open, even if exclusion becomes
        active.
        
        Args:
            path: Path to the DSF file
        """
        with self._lock:
            normalized_path = self._normalize_path(path)
            if normalized_path in self._active_dsfs:
                self._active_dsfs[normalized_path] += 1
            else:
                self._active_dsfs[normalized_path] = 1
            log.debug(f"DSF opened: {normalized_path} (count: {self._active_dsfs[normalized_path]})")
    
    def register_dsf_close(self, path):
        """
        Register that a DSF file has been closed.
        
        Args:
            path: Path to the DSF file
        """
        with self._lock:
            normalized_path = self._normalize_path(path)
            if normalized_path in self._active_dsfs:
                self._active_dsfs[normalized_path] -= 1
                if self._active_dsfs[normalized_path] <= 0:
                    del self._active_dsfs[normalized_path]
                    log.debug(f"DSF closed (fully): {normalized_path}")
                else:
                    log.debug(f"DSF closed: {normalized_path} (count: {self._active_dsfs[normalized_path]})")
    
    def is_dsf_in_use(self, path):
        """
        Check if a DSF file is currently in use.
        
        Args:
            path: Path to the DSF file
            
        Returns:
            bool: True if the DSF is currently open
        """
        with self._lock:
            normalized_path = self._normalize_path(path)
            return normalized_path in self._active_dsfs
    
    def should_hide_dsf(self, path):
        """
        DEPRECATED: Use get_redirect_path() instead.
        
        This method is kept for backward compatibility but should not be used.
        Hiding DSF files causes X-Plane to have no terrain data because X-Plane
        indexes DSF files at flight load time. Instead, we redirect to global scenery.
        
        Args:
            path: Path to the DSF file
            
        Returns:
            bool: Always returns False now - DSFs should never be hidden
        """
        # NEVER hide DSF files - this causes missing terrain in X-Plane
        # X-Plane indexes DSF files at flight load time. If we return ENOENT,
        # X-Plane has no terrain data for those tiles.
        # Instead, use get_redirect_path() to redirect to global scenery.
        return False
    
    def get_redirect_path(self, path):
        """
        Get the redirect path for a DSF file during sun exclusion.
        
        When exclusion is active (night), DSF files should be served from
        X-Plane's global scenery instead of AutoOrtho's scenery. This ensures:
        1. X-Plane always sees DSF files (proper indexing)
        2. Terrain data is always available
        3. Smooth transitions between ortho and default scenery
        
        Args:
            path: Path to the DSF file (FUSE virtual path)
            
        Returns:
            str: Path to global scenery DSF if redirection is needed, None otherwise
        """
        if not self.is_exclusion_active():
            return None
        
        # Don't redirect DSFs that are currently in use (safe transition)
        if self.is_dsf_in_use(path):
            return None
        
        # Parse the DSF path to get folder and filename
        folder, filename = _parse_dsf_coordinates(path)
        if not folder or not filename:
            log.debug(f"Could not parse DSF path for redirect: {path}")
            return None
        
        # Find the corresponding global scenery DSF
        global_dsf = _find_global_scenery_dsf(folder, filename)
        if global_dsf:
            log.debug(f"Redirecting DSF {path} to global scenery: {global_dsf}")
            return global_dsf
        
        # No global scenery found - don't redirect, serve the original
        log.debug(f"No global scenery available for {path}, serving original")
        return None
    
    def is_redirect_active(self):
        """
        Check if DSF redirection is currently active.
        
        This is a simpler check than get_redirect_path() for cases where
        you just need to know if redirection is happening (e.g., for logging).
        
        Returns:
            bool: True if exclusion is active
        """
        return self.is_exclusion_active()
    
    def _normalize_path(self, path):
        """
        Normalize a path for consistent comparison.
        
        Args:
            path: File path
            
        Returns:
            str: Normalized path
        """
        if path is None:
            return ""
        # Convert to forward slashes and lowercase for consistent comparison
        return str(path).replace('\\', '/').lower()
    
    def get_status(self):
        """
        Get current status information for display.
        
        Returns:
            dict: Status information including enabled state,
                  current exclusion state, active DSF count, and sun position
        """
        with self._lock:
            enabled = getattr(CFG.time_exclusion, 'enabled', False)
            
            # Check if global scenery is available
            xplane_path = getattr(CFG.paths, 'xplane_path', '')
            global_scenery_available = False
            if xplane_path:
                global_path = os.path.join(
                    xplane_path, "Global Scenery", "X-Plane 12 Global Scenery"
                )
                global_scenery_available = os.path.isdir(global_path)
            
            # Get sun position info
            sun_pitch = -999.0
            if self._dataref_tracker:
                sun_pitch = self._dataref_tracker.get_sun_pitch()
            sun_night_threshold = getattr(
                CFG.time_exclusion, 'sun_night_threshold', -12.0
            )
            sun_day_threshold = getattr(
                CFG.time_exclusion, 'sun_day_threshold', -10.0
            )
            
            return {
                'enabled': enabled,
                'exclusion_active': self._exclusion_active,
                'redirect_active': self._exclusion_active,
                'active_dsf_count': len(self._active_dsfs),
                'global_scenery_available': global_scenery_available,
                # Sun position info
                'sun_pitch': sun_pitch if -90 <= sun_pitch <= 90 else None,
                'sun_pitch_str': f"{sun_pitch:.1f}°" if -90 <= sun_pitch <= 90 else "N/A",
                'sun_night_threshold': sun_night_threshold,
                'sun_day_threshold': sun_day_threshold,
                'sun_mode_ortho_enabled': self._sun_mode_ortho_enabled,
                'sun_decision_made': self._sun_decision_made,
            }


# Singleton instance
time_exclusion_manager = TimeExclusionManager()
