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
from aoconfig import CFG
import logging

log = logging.getLogger(__name__)
UDP_IP = "127.0.0.1"


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
    ]
    # fmt:on

    def __init__(self):
        """
        Initialize DatarefTracker with thread-safe state and pre-built
        messages.
        """
        # Thread synchronization
        self._lock = threading.Lock()
        self._shutdown_flag = threading.Event()

        # Flight data (protected by _lock)
        self.lat = -1.0
        self.lon = -1.0
        self.alt = -1.0
        self.hdg = -1.0
        self.spd = -1.0
        self.connected = False
        self.data_valid = False

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
                  'spd', 'connected', 'data_valid', 'timestamp'.
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
                'connected': self.connected,
                'data_valid': self.data_valid,
                'timestamp': time.time()
            }

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

                if len(values) == 5:
                    lat = values[0]
                    lon = values[1]
                    alt = values[2]
                    hdg = values[3]
                    spd = values[4]

                    # Validate position data
                    if self._validate_position(lat, lon, alt):
                        self.lat = lat
                        self.lon = lon
                        self.alt = alt
                        self.hdg = hdg
                        self.spd = spd
                        self.data_valid = True
                    else:
                        self.data_valid = False
                else:
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

            # Decode all values
            num_values = len(values_data) // self.VALUE_SIZE
            retvalues = []

            for i in range(num_values):
                offset = self.PACKET_HEADER_SIZE + (i * self.VALUE_SIZE)
                singledata = data[offset:offset + self.VALUE_SIZE]

                try:
                    (idx, value) = struct.unpack("<if", singledata)
                    retvalues.append(value)
                except struct.error as e:
                    log.error(
                        f"Struct unpacking error at index {i}: {e}"
                    )
                    return []

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
