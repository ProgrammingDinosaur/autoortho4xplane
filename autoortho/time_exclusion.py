#!/usr/bin/env python3

"""
Time Exclusion Manager for AutoOrtho.

This module provides time-based exclusion functionality that allows users to
disable AutoOrtho's scenery during specific time ranges in the simulator.
When active, AutoOrtho's DSF files are redirected to X-Plane's global default
scenery, ensuring terrain data is always available.

The manager ensures safe transitions by:
- Tracking which DSF files are currently in use
- Not redirecting DSFs that are actively being read
- Only applying exclusions to new DSF accesses

Key design principle: NEVER hide DSF files from X-Plane. X-Plane indexes DSF
files at flight load time. If we hide files, X-Plane finds no terrain data.
Instead, we redirect reads to global scenery DSF files.

Decision Preservation During Temporary Disconnections:
------------------------------------------------------
When the simulator time becomes available, the exclusion state is determined
based on actual sim time rather than the default_to_exclusion setting. This
decision is preserved across temporary disconnections (e.g., scenery reload).

This prevents a common issue where:
1. User starts with "default to exclusion" enabled
2. Time exclusion is initially active
3. Sim time becomes available and shows daytime → exclusion deactivates
4. User triggers "Reload Scenery" in X-Plane
5. During reload, sim time is temporarily unavailable
6. WITHOUT preservation: exclusion would re-activate (using default)
7. WITH preservation: exclusion stays inactive (using preserved decision)

IMPORTANT LIMITATIONS:
- The preserved decision persists until AutoOrtho is fully restarted
- To reset to the default_to_exclusion behavior, quit and restart AutoOrtho
- The preserved decision is updated whenever new sim time is received
- If sim time indicates a change in exclusion state (e.g., crossing into
  night time), the state will update accordingly when time becomes available

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


def parse_time_string(time_str):
    """
    Parse a time string in HH:MM format to seconds since midnight.
    
    Args:
        time_str: Time string in HH:MM format (e.g., "22:00", "06:30")
        
    Returns:
        int: Seconds since midnight, or None if parsing fails
    """
    try:
        time_str = str(time_str).strip()
        if ':' in time_str:
            parts = time_str.split(':')
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
            if 0 <= hours <= 23 and 0 <= minutes <= 59:
                return hours * 3600 + minutes * 60
        else:
            # Try parsing as raw seconds
            return int(float(time_str))
    except (ValueError, TypeError) as e:
        log.warning(f"Failed to parse time string '{time_str}': {e}")
        return None


def format_time_from_seconds(seconds):
    """
    Format seconds since midnight to HH:MM string.
    
    Args:
        seconds: Seconds since midnight
        
    Returns:
        str: Time string in HH:MM format
    """
    if seconds < 0:
        return "??:??"
    hours = int(seconds // 3600) % 24
    minutes = int((seconds % 3600) // 60)
    return f"{hours:02d}:{minutes:02d}"


class TimeExclusionManager:
    """
    Manages time-based exclusion of AutoOrtho scenery.
    
    This class monitors the simulator's local time and determines when
    AutoOrtho's DSF files should be hidden from X-Plane. It safely handles
    transitions by tracking which DSFs are currently in use.
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
        
        # Track if sim time has become available (for one-time logging)
        self._sim_time_was_available = False
        
        # Preserve the last exclusion decision made based on actual sim time.
        # This allows us to maintain the correct state during temporary disconnections
        # (e.g., scenery reload) without falling back to the default_to_exclusion setting.
        # Once we've made a decision based on sim time, we preserve it until:
        # - A new sim time value causes a different decision
        # - AutoOrtho is restarted
        self._sim_time_decision_made = False  # True once we've used real sim time
        self._last_sim_time_decision = False  # The last exclusion decision from sim time
        self._last_sim_time_value = -1.0  # The last sim time we saw (for logging)
        
        # Sun position mode state (for sun-based exclusion)
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
                # Check if time exclusion is enabled
                enabled, _, _, _ = self._get_config()
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
    
    def _get_config(self):
        """
        Get current time exclusion configuration.
        
        Returns:
            tuple: (enabled, start_seconds, end_seconds, default_to_exclusion) 
                   or (False, None, None, False) if disabled/invalid
        """
        try:
            enabled = getattr(CFG.time_exclusion, 'enabled', False)
            if not enabled:
                return (False, None, None, False)
            
            start_str = getattr(CFG.time_exclusion, 'start_time', '22:00')
            end_str = getattr(CFG.time_exclusion, 'end_time', '06:00')
            default_to_exclusion = getattr(CFG.time_exclusion, 'default_to_exclusion', False)
            
            start_sec = parse_time_string(start_str)
            end_sec = parse_time_string(end_str)
            
            if start_sec is None or end_sec is None:
                log.warning(f"Invalid time exclusion config: start={start_str}, end={end_str}")
                return (False, None, None, False)
            
            return (True, start_sec, end_sec, default_to_exclusion)
        except Exception as e:
            log.debug(f"Error reading time exclusion config: {e}")
            return (False, None, None, False)
    
    def _is_time_in_range(self, current_time_sec, start_sec, end_sec):
        """
        Check if current time falls within the exclusion range.
        
        Handles overnight ranges (e.g., 22:00 to 06:00).
        
        Args:
            current_time_sec: Current time in seconds since midnight
            start_sec: Start of exclusion range (seconds since midnight)
            end_sec: End of exclusion range (seconds since midnight)
            
        Returns:
            bool: True if current time is within the exclusion range
        """
        if start_sec <= end_sec:
            # Normal range (e.g., 09:00 to 17:00)
            return start_sec <= current_time_sec < end_sec
        else:
            # Overnight range (e.g., 22:00 to 06:00)
            # Time is in range if it's >= start OR < end
            return current_time_sec >= start_sec or current_time_sec < end_sec
    
    def _check_sun_exclusion_state(self):
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
        _, _, _, default_to_exclusion = self._get_config()
        
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
        night_threshold = getattr(CFG.time_exclusion, 'sun_night_threshold', -12.0)
        day_threshold = getattr(CFG.time_exclusion, 'sun_day_threshold', -10.0)
        
        # Hysteresis logic
        if self._sun_mode_ortho_enabled:
            # Currently showing ortho (day mode)
            if sun_pitch < night_threshold:
                self._sun_mode_ortho_enabled = False  # Switch to exclusion (night)
                log.info(
                    f"Sun exclusion ACTIVATED: sun pitch {sun_pitch:.1f}° < "
                    f"{night_threshold}° threshold"
                )
        else:
            # Currently in exclusion (night mode)
            if sun_pitch > day_threshold:
                self._sun_mode_ortho_enabled = True  # Switch to ortho (day)
                log.info(
                    f"Sun exclusion DEACTIVATED: sun pitch {sun_pitch:.1f}° > "
                    f"{day_threshold}° threshold"
                )
        
        self._sun_decision_made = True
        self._last_sun_pitch = sun_pitch
        
        # exclusion_active = NOT ortho_enabled
        return not self._sun_mode_ortho_enabled
    
    def _check_exclusion_state(self):
        """
        Check if time exclusion should be active based on current sim time.
        
        Decision Preservation Logic:
        - When sim time first becomes available, we make a decision based on actual time
        - If sim time becomes temporarily unavailable (e.g., during scenery reload),
          we preserve the last sim-time-based decision rather than falling back to the
          default_to_exclusion setting
        - This prevents exclusion from being incorrectly re-activated during reloads
          when the actual sim time indicated it should be inactive
        - The preserved decision persists until AutoOrtho is restarted or new sim time
          is received that would change the decision
        
        Returns:
            bool: True if exclusion should be active
        """
        enabled, start_sec, end_sec, default_to_exclusion = self._get_config()
        
        if not enabled:
            return False
        
        # Check if sun-position mode is enabled
        use_sun = getattr(CFG.time_exclusion, 'use_sun_position', False)
        if use_sun:
            return self._check_sun_exclusion_state()
        
        # Otherwise use time-based logic below
        
        # Get current sim time from dataref tracker
        if self._dataref_tracker is None:
            # No dataref tracker available - use default behavior
            return default_to_exclusion
        
        current_time = self._dataref_tracker.get_local_time_sec()
        
        if current_time < 0:
            # No valid sim time available
            if self._sim_time_was_available:
                self._sim_time_was_available = False
                log.debug("Sim time no longer available (temporary disconnection)")
            
            # KEY CHANGE: If we previously made a decision based on actual sim time,
            # preserve that decision during temporary disconnections (e.g., scenery reload).
            # This prevents the issue where exclusion gets re-activated during reload
            # when actual sim time had already determined it should be inactive.
            if self._sim_time_decision_made:
                log.debug(
                    f"Sim time unavailable - preserving last sim-time decision: "
                    f"exclusion={'active' if self._last_sim_time_decision else 'inactive'} "
                    f"(last sim time was {format_time_from_seconds(self._last_sim_time_value)})"
                )
                return self._last_sim_time_decision
            
            # No previous sim-time decision - use configured default
            if default_to_exclusion:
                log.debug("Sim time not available - defaulting to exclusion active")
            return default_to_exclusion
        
        # Log once when sim time first becomes available
        if not self._sim_time_was_available:
            self._sim_time_was_available = True
            log.info(f"Sim time now available: {format_time_from_seconds(current_time)}")
        
        # Calculate exclusion state based on actual sim time
        should_exclude = self._is_time_in_range(current_time, start_sec, end_sec)
        
        # Store this decision for use during temporary disconnections
        self._sim_time_decision_made = True
        self._last_sim_time_decision = should_exclude
        self._last_sim_time_value = current_time
        
        return should_exclude
    
    def is_exclusion_active(self):
        """
        Check if time exclusion is currently active.
        
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
            
            old_state = self._exclusion_active
            self._exclusion_active = self._check_exclusion_state()
            self._last_check_time = current_time
            
            # Log state changes
            if old_state != self._exclusion_active:
                if self._exclusion_active:
                    enabled, start_sec, end_sec, default_to_excl = self._get_config()
                    current_time_sec = self._dataref_tracker.get_local_time_sec() if self._dataref_tracker else -1
                    if current_time_sec < 0:
                        # Sim time not available - check why exclusion was activated
                        if self._sim_time_decision_made and self._last_sim_time_decision:
                            # Preserved decision from previous sim time (shouldn't normally happen
                            # for activation since we preserve the last decision, but handle it)
                            log.info(
                                f"Time exclusion ACTIVATED - DSF reads will redirect to global scenery "
                                f"(using preserved decision from sim time {format_time_from_seconds(self._last_sim_time_value)}) "
                                f"(exclusion range: {format_time_from_seconds(start_sec)} - {format_time_from_seconds(end_sec)})"
                            )
                        else:
                            # Using default setting (no sim time ever received)
                            log.info(
                                f"Time exclusion ACTIVATED - DSF reads will redirect to global scenery "
                                f"(sim time not yet available, using default) "
                                f"(exclusion range: {format_time_from_seconds(start_sec)} - {format_time_from_seconds(end_sec)})"
                            )
                    else:
                        log.info(
                            f"Time exclusion ACTIVATED at sim time {format_time_from_seconds(current_time_sec)} "
                            f"- DSF reads will redirect to global scenery "
                            f"(exclusion range: {format_time_from_seconds(start_sec)} - {format_time_from_seconds(end_sec)})"
                        )
                else:
                    current_time_sec = self._dataref_tracker.get_local_time_sec() if self._dataref_tracker else -1
                    if current_time_sec < 0 and self._sim_time_decision_made:
                        # Deactivated due to preserved decision
                        log.info(
                            f"Time exclusion DEACTIVATED - DSF reads will use AutoOrtho scenery "
                            f"(preserving decision from sim time {format_time_from_seconds(self._last_sim_time_value)})"
                        )
                    else:
                        log.info("Time exclusion DEACTIVATED - DSF reads will use AutoOrtho scenery")
            
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
        Get the redirect path for a DSF file during time exclusion.
        
        When time exclusion is active, DSF files should be served from
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
            bool: True if time exclusion is active
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
            dict: Status information including enabled state, time range,
                  current exclusion state, active DSF count, and redirect status
        """
        with self._lock:
            enabled, start_sec, end_sec, default_to_excl = self._get_config()
            current_time = -1
            if self._dataref_tracker:
                current_time = self._dataref_tracker.get_local_time_sec()
            
            # Check if global scenery is available
            xplane_path = getattr(CFG.paths, 'xplane_path', '')
            global_scenery_available = False
            if xplane_path:
                global_path = os.path.join(
                    xplane_path, "Global Scenery", "X-Plane 12 Global Scenery"
                )
                global_scenery_available = os.path.isdir(global_path)
            
            # Get sun position info
            use_sun = getattr(CFG.time_exclusion, 'use_sun_position', False)
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
                'start_time': format_time_from_seconds(start_sec) if start_sec is not None else "N/A",
                'end_time': format_time_from_seconds(end_sec) if end_sec is not None else "N/A",
                'default_to_exclusion': default_to_excl,
                'exclusion_active': self._exclusion_active,
                'redirect_active': self._exclusion_active,  # Redirect is now the mechanism
                'current_sim_time': format_time_from_seconds(current_time) if current_time >= 0 else "N/A",
                'sim_time_available': current_time >= 0,
                'active_dsf_count': len(self._active_dsfs),
                'global_scenery_available': global_scenery_available,
                # Preserved decision info (for debugging and status display)
                'sim_time_decision_made': self._sim_time_decision_made,
                'preserved_decision': self._last_sim_time_decision if self._sim_time_decision_made else None,
                'preserved_from_time': format_time_from_seconds(self._last_sim_time_value) if self._sim_time_decision_made else "N/A",
                # Sun position mode info
                'use_sun_position': use_sun,
                'sun_pitch': sun_pitch if -90 <= sun_pitch <= 90 else None,
                'sun_pitch_str': f"{sun_pitch:.1f}°" if -90 <= sun_pitch <= 90 else "N/A",
                'sun_night_threshold': sun_night_threshold,
                'sun_day_threshold': sun_day_threshold,
                'sun_mode_ortho_enabled': self._sun_mode_ortho_enabled,
            }


# Singleton instance
time_exclusion_manager = TimeExclusionManager()

