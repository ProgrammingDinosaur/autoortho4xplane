#!/usr/bin/env python3

"""
DatarefTracker: Thread-safe X-Plane dataref collection via UDP.

This module provides a singleton DatarefTracker instance that connects
to X-Plane via UDP to receive real-time flight data (position, heading,
speed, altitude).

Connection State:
    The tracker automatically connects when X-Plane is running a flight
    and reconnects if the connection is lost. The 'connected' and
    'data_valid' flags indicate the current state.
"""

import binascii
import time
import struct
import socket
import threading
import math
from dataclasses import dataclass
from typing import Optional, Dict, List

# Handle imports for both frozen (PyInstaller) and direct Python execution
try:
    from autoortho.aoconfig import CFG
except ImportError:
    from aoconfig import CFG

import logging

log = logging.getLogger(__name__)
UDP_IP = "127.0.0.1"


# =============================================================================
# FlightSample and FlightDataAverager
# =============================================================================

@dataclass
class FlightSample:
    """Single flight data sample with timestamp."""
    timestamp: float
    lat: float
    lon: float
    alt_ft: float
    hdg: float
    spd: float  # ground speed in m/s


class FlightDataAverager:
    """
    Averages flight data samples over a time window.
    
    Used to smooth out instantaneous flight data for more stable predictions.
    Handles circular averaging for heading (0/360 wraparound).
    """
    
    # Configuration
    WINDOW_SECONDS = 60.0  # Time window for averaging
    MIN_SAMPLES = 5  # Minimum samples needed for valid average
    
    def __init__(self, window_seconds: float = None):
        """Initialize the averager with optional custom window size."""
        self._lock = threading.Lock()
        self._samples: List[FlightSample] = []
        if window_seconds is not None:
            self.WINDOW_SECONDS = window_seconds
    
    def add_sample(self, lat: float, lon: float, alt_ft: float, 
                   hdg: float, spd: float) -> None:
        """
        Add a new flight sample and prune old samples.
        
        Args:
            lat: Latitude in degrees
            lon: Longitude in degrees  
            alt_ft: Altitude AGL in feet
            hdg: Heading in degrees (0-360)
            spd: Ground speed in m/s
        """
        now = time.time()
        sample = FlightSample(
            timestamp=now,
            lat=lat,
            lon=lon,
            alt_ft=alt_ft,
            hdg=hdg,
            spd=spd
        )
        
        with self._lock:
            self._samples.append(sample)
            self._prune_old_samples(now)
    
    def _prune_old_samples(self, now: float) -> None:
        """Remove samples older than the window. Must be called with lock held."""
        cutoff = now - self.WINDOW_SECONDS
        self._samples = [s for s in self._samples if s.timestamp >= cutoff]
    
    def sample_count(self) -> int:
        """Return number of samples in the window."""
        with self._lock:
            return len(self._samples)
    
    def is_valid(self) -> bool:
        """Return True if we have enough samples for a valid average."""
        with self._lock:
            return len(self._samples) >= self.MIN_SAMPLES
    
    def clear(self) -> None:
        """Clear all samples."""
        with self._lock:
            self._samples.clear()
    
    def get_averages(self) -> Optional[Dict]:
        """
        Compute averaged flight data.
        
        Returns:
            dict with keys:
                - 'lat': Average latitude
                - 'lon': Average longitude
                - 'alt_ft': Average altitude AGL in feet
                - 'heading': Circular-averaged heading in degrees (0-360)
                - 'ground_speed_mps': Average ground speed in m/s
                - 'vertical_speed_fpm': Computed vertical speed in feet/minute
            Returns None if not enough samples.
        """
        with self._lock:
            if len(self._samples) < self.MIN_SAMPLES:
                return None
            
            samples = list(self._samples)  # Copy for thread safety
        
        n = len(samples)
        
        # Simple averages
        avg_lat = sum(s.lat for s in samples) / n
        avg_lon = sum(s.lon for s in samples) / n
        avg_alt = sum(s.alt_ft for s in samples) / n
        avg_spd = sum(s.spd for s in samples) / n
        
        # Circular averaging for heading
        avg_heading = self._circular_mean_heading([s.hdg for s in samples])
        
        # Compute vertical speed from altitude change over time
        vertical_speed_fpm = self._compute_vertical_speed(samples)
        
        return {
            'lat': avg_lat,
            'lon': avg_lon,
            'alt_ft': avg_alt,
            'heading': avg_heading,
            'ground_speed_mps': avg_spd,
            'vertical_speed_fpm': vertical_speed_fpm,
        }
    
    def _circular_mean_heading(self, headings: List[float]) -> float:
        """
        Compute circular mean of headings (handles 0/360 wraparound).
        
        Uses vector averaging: convert to unit vectors, average, then back to angle.
        """
        if not headings:
            return 0.0
        
        # Convert to radians and compute x,y components
        sin_sum = sum(math.sin(math.radians(h)) for h in headings)
        cos_sum = sum(math.cos(math.radians(h)) for h in headings)
        
        # Average the components
        n = len(headings)
        avg_sin = sin_sum / n
        avg_cos = cos_sum / n
        
        # Convert back to angle
        avg_heading = math.degrees(math.atan2(avg_sin, avg_cos))
        
        # Normalize to 0-360
        if avg_heading < 0:
            avg_heading += 360.0
        
        return avg_heading
    
    def _compute_vertical_speed(self, samples: List[FlightSample]) -> float:
        """
        Compute vertical speed in feet per minute using linear regression.
        
        Uses the first and last samples if we have at least 2.
        """
        if len(samples) < 2:
            return 0.0
        
        # Get time and altitude deltas
        first = samples[0]
        last = samples[-1]
        
        dt_seconds = last.timestamp - first.timestamp
        if dt_seconds <= 0:
            return 0.0
        
        dalt_ft = last.alt_ft - first.alt_ft
        
        # Convert to feet per minute
        vertical_speed_fpm = (dalt_ft / dt_seconds) * 60.0
        
        return vertical_speed_fpm


class DatarefTracker(object):
    """Thread-safe tracker for X-Plane flight data via UDP."""

    # Protocol constants
    RREF_HEADER = b"RREF"
    RREF_CMD = b"RREF\x00"
    PACKET_HEADER_SIZE = 5
    VALUE_SIZE = 8  # idx (4 bytes) + value (4 bytes)
    DATAREF_STRING_SIZE = 400
    MESSAGE_SIZE = 413  # 5 (cmd) + 4 (freq) + 4 (idx) + 400 (string)
    RECV_BUFFER_SIZE = 1024
    DEFAULT_FREQ_HZ = 1
    RECONNECT_DELAY_SEC = 5
    UDP_TIMEOUT_SEC = 5
    THREAD_JOIN_TIMEOUT_SEC = 10

    # List of datarefs to request.
    # fmt:off
    datarefs = [
        # (dataref, unit, description, decimals for formatted output)
        ("sim/flightmodel/position/latitude", "°N",
         "The latitude of the aircraft", 6),
        ("sim/flightmodel/position/longitude", "°E",
         "The longitude of the aircraft", 6),
        ("sim/flightmodel/position/y_agl", "m",
         "AGL", 0),
        ("sim/flightmodel/position/mag_psi", "°",
         "The real magnetic heading of the aircraft", 0),
        ("sim/flightmodel/position/groundspeed", "m/s",
         "The ground speed of the aircraft", 0),
        ("sim/time/local_time_sec", "s",
         "Local time (seconds since midnight)", 0),
        ("sim/flightmodel2/position/pressure_altitude", "ft",
         "Pressure altitude in standard atmosphere", 0),
        ("sim/graphics/scenery/sun_pitch_degrees", "°",
         "Sun elevation angle from horizontal (v9.40.0+)", 1),
    ]
    # fmt:on

    def __init__(self):
        """
        Initialize DatarefTracker with thread-safe state and pre-built
        messages.
        """
        # Thread synchronization
        # Use RLock (reentrant lock) to allow the same thread to acquire the lock
        # multiple times - required because properties like alt_agl_ft acquire the
        # lock internally, and callers may already hold the lock when accessing them.
        self._lock = threading.RLock()
        self._shutdown_flag = threading.Event()

        # Flight data (protected by _lock)
        self.lat = -1.0
        self.lon = -1.0
        self.alt = -1.0
        self.hdg = -1.0
        self.spd = -1.0
        self.local_time_sec = -1.0  # Local time (seconds since midnight)
        self.pressure_alt = -1.0  # Pressure altitude in feet
        self.sun_pitch = -999.0  # Sun elevation angle (-90 to +90, -999 = invalid)
        self.connected = False
        self.data_valid = False
        self.has_ever_connected = False  # True once first connection established

        # Flight data averager for smoothed predictions
        self.flight_averager = FlightDataAverager()

        # Thread management
        self.t = None
        self.running = False

        # Socket
        self.sock = socket.socket(
            socket.AF_INET,  # Internet
            socket.SOCK_DGRAM,  # UDP
        )
        self.sock.settimeout(self.UDP_TIMEOUT_SEC)

        # Pre-build UDP messages for performance
        # (avoid repeated encoding/packing)
        self._build_messages()

        log.info("Dataref Tracker instance created")

    @property
    def alt_agl_ft(self) -> float:
        """
        Get altitude AGL in feet (thread-safe).
        
        Returns:
            float: Altitude AGL in feet, or -1 if not available.
        """
        with self._lock:
            if not self.connected or not self.data_valid:
                return -1.0
            # self.alt is in meters, convert to feet
            return self.alt * 3.28084

    def get_flight_averages(self) -> Optional[Dict]:
        """
        Get averaged flight data for stable predictions.
        
        Returns:
            dict: Averaged flight data with 'heading', 'ground_speed_mps',
                  'vertical_speed_fpm', 'lat', 'lon', 'alt_ft'.
                  Returns None if not enough samples.
        """
        return self.flight_averager.get_averages()

    def clear_flight_averages(self) -> None:
        """Clear all flight averages (e.g., after reconnect)."""
        self.flight_averager.clear()

    def set_flight_averages(self, averages: Dict) -> None:
        """
        Set mock flight averages (for testing).
        
        Note: This directly sets the averages by adding samples to match.
        For proper testing, use the flight_averager directly.
        """
        # This is a convenience method for tests
        # Add enough samples to make averages valid
        for _ in range(FlightDataAverager.MIN_SAMPLES):
            self.flight_averager.add_sample(
                lat=averages.get('lat', 0),
                lon=averages.get('lon', 0),
                alt_ft=averages.get('alt_ft', 0),
                hdg=averages.get('heading', 0),
                spd=averages.get('ground_speed_mps', 0)
            )
            time.sleep(0.001)  # Small delay for different timestamps

    def _build_messages(self):
        """
        Pre-build subscribe and unsubscribe messages for all datarefs.
        """
        self.subscribe_messages = []
        self.unsubscribe_messages = []

        for idx, dataref in enumerate(self.datarefs):
            # Pre-encode the dataref string
            encoded = dataref[0].encode()

            # Pre-build subscribe message (freq=DEFAULT_FREQ_HZ)
            sub_msg = struct.pack(
                "<5sii400s",
                self.RREF_CMD,
                self.DEFAULT_FREQ_HZ,
                idx,
                encoded
            )
            self.subscribe_messages.append(sub_msg)

            # Pre-build unsubscribe message (freq=0)
            unsub_msg = struct.pack(
                "<5sii400s",
                self.RREF_CMD,
                0,
                idx,
                encoded
            )
            self.unsubscribe_messages.append(unsub_msg)

    def get_flight_data(self):
        """
        Thread-safe getter for all flight data.

        Returns:
            dict: Flight data with keys 'lat', 'lon', 'alt', 'hdg',
                  'spd', 'local_time_sec', 'pressure_alt', 'sun_pitch',
                  'connected', 'data_valid', 'timestamp'.
                  Returns None if not connected or data is invalid.
        """
        with self._lock:
            if not self.connected or not self.data_valid:
                return None
            return {
                'lat': self.lat,
                'lon': self.lon,
                'alt': self.alt,
                'hdg': self.hdg,
                'spd': self.spd,
                'local_time_sec': self.local_time_sec,
                'pressure_alt': self.pressure_alt,
                'sun_pitch': self.sun_pitch,
                'connected': self.connected,
                'data_valid': self.data_valid,
                'timestamp': time.time()
            }

    def get_local_time_sec(self):
        """
        Thread-safe getter for local sim time.

        Returns:
            float: Seconds since midnight in sim time, or -1 if not available.
        """
        with self._lock:
            if not self.connected or not self.data_valid:
                return -1.0
            return self.local_time_sec

    def get_pressure_alt(self):
        """
        Thread-safe getter for pressure altitude.

        Returns:
            float: Pressure altitude in feet, or -1 if not available.
        """
        with self._lock:
            if not self.connected or not self.data_valid:
                return -1.0
            return self.pressure_alt

    def get_sun_pitch(self):
        """
        Thread-safe getter for sun pitch (elevation angle).

        The sun pitch is the angle of the sun above or below the horizon:
        - Positive values: sun is above the horizon (daytime)
        - Zero: sun is at the horizon (sunrise/sunset)
        - Negative values: sun is below the horizon (twilight/night)
          - < -6°: civil twilight ends
          - < -12°: nautical twilight ends
          - < -18°: astronomical twilight ends (full night)

        Returns:
            float: Sun pitch in degrees (-90 to +90), or -999 if not available.
        """
        with self._lock:
            if not self.connected or not self.data_valid:
                return -999.0
            return self.sun_pitch

    def start(self):
        """Start the UDP listening thread."""
        if self.running:
            return
        log.info("Starting UDP listening thread")
        self._shutdown_flag.clear()
        self.running = True
        self.t = threading.Thread(target=self._dataref_tracker_udp_listen)
        self.t.daemon = True  # Ensure thread doesn't prevent exit
        self.t.start()

    def stop(self):
        """Stop the UDP listening thread and unsubscribe from datarefs."""
        log.info("Dataref Tracker shutdown requested.")

        # Signal shutdown to prevent race conditions
        self._shutdown_flag.set()
        self.running = False

        # Unsubscribe from datarefs
        # X-Plane might already be shut down, so ignore errors
        try:
            self._request_datarefs(subscribe=False)
        except (Exception, OSError):
            pass

        # Wait for thread to terminate with timeout
        if self.t and self.t.is_alive():
            self.t.join(timeout=self.THREAD_JOIN_TIMEOUT_SEC)
            if self.t.is_alive():
                log.error(
                    "UDP listener thread did not terminate gracefully"
                )

        # Close socket
        try:
            self.sock.close()
        except Exception:
            pass

        log.info("Dataref Tracker stopped.")

    def _validate_position(self, lat, lon, alt):
        """
        Validate that position data is within reasonable ranges.

        Args:
            lat (float): Latitude in degrees
            lon (float): Longitude in degrees
            alt (float): Altitude in meters AGL

        Returns:
            bool: True if all values are valid, False otherwise
        """
        if not (-90 <= lat <= 90):
            log.warning(f"Invalid latitude: {lat}")
            return False
        if not (-180 <= lon <= 180):
            log.warning(f"Invalid longitude: {lon}")
            return False
        # Reasonable altitude range: -500m to 50000m AGL
        # (allowing negative for below ground detection)
        if not (-500 <= alt <= 50000):
            log.warning(f"Invalid altitude: {alt}")
            return False
        return True

    # We're really only interested in whether or not X-Plane is able
    # to send us datarefs, which it only does when a flight is active.
    # Note that a flight is not considered active when X-Plane is
    # displaying the "Reading new scenery files" splash screen before
    # a flight.
    #
    # - For the feature where we increase maxwait during the initial
    #   scenery load, we just need to know if the flight is active
    #   (could be flying). When it is not active (e.g., at a splash
    #   screen before the flight), we know we are not flying and can
    #   increase maxwait without the risk of a stutter.
    #
    # - For the predictive preload feature, we need to know if the
    #   flight is active so we can get good datarefs to predict the
    #   flight path.
    #
    # We don't really need to know if X-Plane is simply up and running.
    # So there really isn't any need to use the "beacon" feature to
    # "find" a running instance of X-Plane. If we ever have a need to
    # do so, here are some resources:
    # https://github.com/charlylima/XPlaneUDP/blob/master/XPlaneUdp.py
    # https://gitlab.bliesener.com/jbliesener/PiDisplay/-/...
    #                                   .../blob/master/XPlaneUDP.py
    #
    # It therefore seems like the best way to detect when we can get
    # datarefs is to just periodically subscribe for datarefs and see
    # if a response comes back in a reasonable time. If it does not,
    # clear/reset the socket and try again after a short wait.
    def _dataref_tracker_udp_listen(self):
        """Main UDP listening loop (runs in separate thread)."""
        log.info("UDP listening thread started and listening!")

        # Subscribe to dataref updates at the default rate
        self._request_datarefs(subscribe=True)

        while self.running and not self._shutdown_flag.is_set():
            try:
                data, addr = self.sock.recvfrom(self.RECV_BUFFER_SIZE)
            except Exception:
                # All exceptions including ConnectionResetError and
                # socket.timeout are handled in the same way.
                # This can happen when X-Plane is not running yet or
                # has stopped. For now, reset the socket, wait a bit
                # (quietly), and retry.

                # Update connection state (thread-safe)
                with self._lock:
                    if self.connected:
                        log.info("Flight has stopped.")
                        # Clear flight averages when connection is lost
                        self.flight_averager.clear()
                    self.connected = False
                    self.data_valid = False

                # Check if we're shutting down before recreating socket
                if self._shutdown_flag.is_set():
                    break

                # Recreate socket
                try:
                    self.sock.close()
                except Exception:
                    pass

                self.sock = socket.socket(
                    socket.AF_INET,  # Internet
                    socket.SOCK_DGRAM,  # UDP
                )
                self.sock.settimeout(self.UDP_TIMEOUT_SEC)

                # Wait before retrying
                time.sleep(self.RECONNECT_DELAY_SEC)

                # Check again if we're shutting down
                if self._shutdown_flag.is_set():
                    break

                # Re-subscribe to datarefs
                self._request_datarefs(subscribe=True)
                continue

            # Socket read was successful - decode packet
            values = self._decode_packet(data)

            # Update flight data (thread-safe, atomic update)
            with self._lock:
                if not self.connected:
                    log.info("Flight is starting.")
                    self.connected = True
                    self.has_ever_connected = True  # Permanent flag for startup detection

                # Accept 5+ values for backward compatibility
                # (6th=local_time_sec, 7th=pressure_alt, 8th=sun_pitch)
                if len(values) >= 5:
                    lat = values[0]
                    lon = values[1]
                    alt = values[2]
                    hdg = values[3]
                    spd = values[4]
                    # local_time is optional (6th value) for backward compat
                    local_time = values[5] if len(values) >= 6 else None
                    # pressure_alt is optional (7th value)
                    pressure_alt = values[6] if len(values) >= 7 else None
                    # sun_pitch is optional (8th value)
                    sun_pitch = values[7] if len(values) >= 8 else None

                    # Validate position data
                    if self._validate_position(lat, lon, alt):
                        self.lat = lat
                        self.lon = lon
                        self.alt = alt
                        self.hdg = hdg
                        self.spd = spd
                        # Only update local_time_sec if we received it
                        if local_time is not None:
                            self.local_time_sec = local_time
                        # Only update pressure_alt if we received it
                        if pressure_alt is not None:
                            self.pressure_alt = pressure_alt
                        # Only update sun_pitch if we received it
                        if sun_pitch is not None:
                            self.sun_pitch = sun_pitch
                        self.data_valid = True
                        
                        # Feed the flight averager (alt is in meters, convert to feet)
                        self.flight_averager.add_sample(
                            lat=lat,
                            lon=lon,
                            alt_ft=alt * 3.28084,  # Convert meters to feet
                            hdg=hdg,
                            spd=spd
                        )
                    else:
                        self.data_valid = False
                else:
                    # Not enough values - log and mark invalid
                    log.debug(f"Incomplete packet: got {len(values)}, need 5+")
                    self.data_valid = False

            # Debug logging (uncomment if needed)
            # log.info(
            #     f"Lat: {self.lat:.6f}, Lon: {self.lon:.6f}, "
            #     f"Alt: {self.alt:.0f}, Hdg: {self.hdg:.0f}, "
            #     f"Spd: {self.spd:.0f}"
            # )

        # No longer in running state
        log.info("UDP listen thread exiting...")
        # Let the thread exit

    def _request_datarefs(self, subscribe=True):
        """
        Send dataref subscription requests to X-Plane using pre-built
        messages.

        Args:
            subscribe (bool): True to subscribe (freq=1), False to
                              unsubscribe (freq=0)
        """
        messages = (self.subscribe_messages if subscribe
                    else self.unsubscribe_messages)
        udp_port = CFG.flightdata.xplane_udp_port

        for message in messages:
            try:
                self.sock.sendto(message, (UDP_IP, int(udp_port)))
            except Exception as e:
                # Don't log warnings during shutdown - socket errors are expected
                if not self._shutdown_flag.is_set():
                    log.warning(f"Failed to send dataref request: {e}")
                raise

    def _decode_packet(self, data):
        """
        Decode RREF packet from X-Plane.

        Args:
            data (bytes): Raw UDP packet data

        Returns:
            list: List of float values in the order they were
                  requested, or empty list if packet is invalid
        """
        try:
            # Validate packet length
            if len(data) < self.PACKET_HEADER_SIZE:
                log.warning(f"Packet too short: {len(data)} bytes")
                return []

            # Validate header
            header = data[0:4]
            if header != self.RREF_HEADER:
                log.warning(
                    f"Unknown packet header: {binascii.hexlify(header)}"
                )
                return []

            # Extract values
            values_data = data[self.PACKET_HEADER_SIZE:]

            # Validate payload length
            if len(values_data) % self.VALUE_SIZE != 0:
                log.warning(
                    f"Invalid packet length: {len(values_data)} not "
                    f"divisible by {self.VALUE_SIZE}"
                )
                return []

            # Decode all values - use idx from packet to place in correct position
            # X-Plane sends each value with its assigned index, which may not
            # be in sequential order
            num_values = len(values_data) // self.VALUE_SIZE
            num_datarefs = len(self.datarefs)
            retvalues = [None] * num_datarefs  # Pre-allocate with None

            for i in range(num_values):
                offset = self.PACKET_HEADER_SIZE + (i * self.VALUE_SIZE)
                singledata = data[offset:offset + self.VALUE_SIZE]

                try:
                    (idx, value) = struct.unpack("<if", singledata)
                    # Use the idx from the packet to place value correctly
                    if 0 <= idx < num_datarefs:
                        retvalues[idx] = value
                    else:
                        log.warning(
                            f"Received dataref index {idx} out of range "
                            f"(expected 0-{num_datarefs - 1})"
                        )
                except struct.error as e:
                    log.error(
                        f"Struct unpacking error at index {i}: {e}"
                    )
                    return []

            # Check if we got all required values (first 5 are essential)
            if any(v is None for v in retvalues[:5]):
                log.debug("Incomplete packet: missing essential datarefs")
                return []

            # Replace remaining None values with -1.0 for optional datarefs
            retvalues = [v if v is not None else -1.0 for v in retvalues]

            return retvalues

        except Exception as e:
            log.error(f"Unexpected error decoding packet: {e}")
            return []

    # Legacy method for backwards compatibility (if needed externally)
    def RequestDataRefs(self, sock, UDP_PORT=49000, REQ_FREQ=1):
        """
        Legacy method for requesting datarefs.
        Prefer using _request_datarefs() instead.
        """
        self._request_datarefs(subscribe=(REQ_FREQ > 0))

    def DecodePacket(self, data):
        """
        Legacy method for decoding packets.
        Prefer using _decode_packet() instead.

        Returns:
            dict: Dictionary mapping idx to (value, unit, dataref_name)
                  tuples
        """
        values = self._decode_packet(data)
        retvalues = {}
        for idx, value in enumerate(values):
            if idx < len(self.datarefs):
                retvalues[idx] = (
                    value,
                    self.datarefs[idx][1],
                    self.datarefs[idx][0]
                )
        return retvalues


dt = DatarefTracker()
