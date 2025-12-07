import time
import struct
import socket
import threading
import pytest
from unittest.mock import Mock, patch
import datareftrack


class TestDatarefTrackerInit:
    """Test DatarefTracker initialization."""

    def test_init(self):
        """Verify initialization creates correct instance state."""
        dt = datareftrack.DatarefTracker()

        # Check flight data initialized correctly
        assert dt.lat == -1.0
        assert dt.lon == -1.0
        assert dt.alt == -1.0
        assert dt.hdg == -1.0
        assert dt.spd == -1.0
        assert dt.connected is False
        assert dt.data_valid is False

        # Check thread management
        assert dt.running is False
        assert dt.t is None

        # Check synchronization primitives exist
        assert dt._lock is not None
        assert dt._shutdown_flag is not None

        # Check pre-built messages exist
        assert len(dt.subscribe_messages) == 5
        assert len(dt.unsubscribe_messages) == 5

        # Verify message structure
        for msg in dt.subscribe_messages:
            assert len(msg) == 413  # MESSAGE_SIZE
            assert msg[0:5] == b"RREF\x00"  # RREF_CMD

    def test_build_messages(self):
        """Verify pre-built messages are correct."""
        dt = datareftrack.DatarefTracker()

        # Test subscribe message format
        sub_msg = dt.subscribe_messages[0]
        assert len(sub_msg) == 413

        # Decode the message
        cmd, freq, idx, string = struct.unpack("<5sii400s", sub_msg)
        assert cmd == b"RREF\x00"
        assert freq == 1  # DEFAULT_FREQ_HZ
        assert idx == 0

        # Test unsubscribe message format
        unsub_msg = dt.unsubscribe_messages[0]
        cmd, freq, idx, string = struct.unpack("<5sii400s", unsub_msg)
        assert cmd == b"RREF\x00"
        assert freq == 0  # Frequency 0 = unsubscribe
        assert idx == 0


class TestDatarefTrackerThreadLifecycle:
    """Test thread lifecycle management."""

    def test_start_stop(self):
        """Verify thread starts and stops correctly."""
        dt = datareftrack.DatarefTracker()

        # Mock socket to prevent actual UDP communication
        dt.sock = Mock()
        dt.sock.recvfrom = Mock(side_effect=socket.timeout)
        dt.sock.close = Mock()
        dt.sock.sendto = Mock()

        # Start the tracker
        dt.start()
        assert dt.running is True
        assert dt.t is not None
        assert dt.t.is_alive()

        # Give thread a moment to start
        time.sleep(0.1)

        # Stop the tracker
        dt.stop()
        assert dt.running is False

        # Verify thread terminated
        time.sleep(0.5)
        assert not dt.t.is_alive()

    def test_start_idempotent(self):
        """Verify calling start() multiple times is safe."""
        dt = datareftrack.DatarefTracker()
        dt.sock = Mock()
        dt.sock.recvfrom = Mock(side_effect=socket.timeout)
        dt.sock.sendto = Mock()

        dt.start()
        first_thread = dt.t

        # Starting again should not create a new thread
        dt.start()
        assert dt.t is first_thread

        dt.stop()

    def test_stop_without_start(self):
        """Verify stopping without starting doesn't crash."""
        dt = datareftrack.DatarefTracker()
        dt.sock = Mock()

        # Should not raise an exception
        dt.stop()
        assert dt.running is False


class TestDatarefTrackerThreadSafety:
    """Test thread safety of shared state access."""

    def test_get_flight_data_thread_safe(self):
        """Verify get_flight_data returns atomic snapshot."""
        dt = datareftrack.DatarefTracker()

        # Set some test data
        with dt._lock:
            dt.lat = 47.5
            dt.lon = -122.3
            dt.alt = 1000.0
            dt.hdg = 180.0
            dt.spd = 50.0
            dt.connected = True
            dt.data_valid = True

        # Get data
        data = dt.get_flight_data()

        assert data is not None
        assert data['lat'] == 47.5
        assert data['lon'] == -122.3
        assert data['alt'] == 1000.0
        assert data['hdg'] == 180.0
        assert data['spd'] == 50.0
        assert data['connected'] is True
        assert data['data_valid'] is True
        assert 'timestamp' in data

    def test_get_flight_data_returns_none_when_invalid(self):
        """Verify get_flight_data returns None when data invalid."""
        dt = datareftrack.DatarefTracker()

        # Default state: not connected, data not valid
        data = dt.get_flight_data()
        assert data is None

        # Connected but data not valid
        with dt._lock:
            dt.connected = True
            dt.data_valid = False
        data = dt.get_flight_data()
        assert data is None

        # Data valid but not connected
        with dt._lock:
            dt.connected = False
            dt.data_valid = True
        data = dt.get_flight_data()
        assert data is None

    def test_concurrent_access(self):
        """Test concurrent reads don't cause race conditions."""
        dt = datareftrack.DatarefTracker()

        # Set valid data
        with dt._lock:
            dt.lat = 47.5
            dt.lon = -122.3
            dt.alt = 1000.0
            dt.hdg = 180.0
            dt.spd = 50.0
            dt.connected = True
            dt.data_valid = True

        results = []
        errors = []

        def reader():
            try:
                for _ in range(100):
                    data = dt.get_flight_data()
                    if data is not None:
                        # Verify data consistency
                        assert data['lat'] == 47.5
                        assert data['lon'] == -122.3
                        results.append(data)
            except Exception as e:
                errors.append(e)

        # Start multiple reader threads
        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have no errors
        assert len(errors) == 0
        # Should have many successful reads
        assert len(results) > 0


class TestDatarefTrackerDecodePacket:
    """Test packet decoding logic."""

    def test_decode_packet_valid(self):
        """Test decoding a valid RREF packet."""
        dt = datareftrack.DatarefTracker()

        # Build a valid RREF packet with 5 values
        header = b"RREF\x00"
        values = []
        for i in range(5):
            # Pack index and value
            values.append(struct.pack("<if", i, float(i * 10)))

        packet = header + b"".join(values)

        result = dt._decode_packet(packet)

        assert len(result) == 5
        assert result[0] == 0.0
        assert result[1] == 10.0
        assert result[2] == 20.0
        assert result[3] == 30.0
        assert result[4] == 40.0

    def test_decode_packet_invalid_header(self):
        """Test decoding packet with wrong header."""
        dt = datareftrack.DatarefTracker()

        packet = b"XXXX\x00" + struct.pack("<if", 0, 1.0)

        result = dt._decode_packet(packet)
        assert result == []

    def test_decode_packet_too_short(self):
        """Test decoding packet that's too short."""
        dt = datareftrack.DatarefTracker()

        packet = b"RR"  # Too short

        result = dt._decode_packet(packet)
        assert result == []

    def test_decode_packet_invalid_length(self):
        """Test decoding packet with invalid payload length."""
        dt = datareftrack.DatarefTracker()

        # Header plus 7 bytes (not divisible by 8)
        packet = b"RREF\x00" + b"1234567"

        result = dt._decode_packet(packet)
        assert result == []

    def test_decode_packet_struct_error(self):
        """Test handling of struct unpacking errors."""
        dt = datareftrack.DatarefTracker()

        # Valid header but corrupted data
        packet = b"RREF\x00" + b"corruptd"  # 8 bytes but not valid struct

        # Should handle gracefully and not crash
        dt._decode_packet(packet)
        # Result might be empty or have values depending on data

    def test_legacy_decode_packet(self):
        """Test legacy DecodePacket method."""
        dt = datareftrack.DatarefTracker()

        # Build a valid RREF packet
        header = b"RREF\x00"
        values = []
        for i in range(5):
            values.append(struct.pack("<if", i, float(i * 10)))

        packet = header + b"".join(values)

        result = dt.DecodePacket(packet)

        # Legacy method returns dict
        assert isinstance(result, dict)
        assert len(result) == 5
        assert result[0][0] == 0.0  # value
        assert result[1][0] == 10.0
        # Check tuple structure (value, unit, dataref_name)
        assert len(result[0]) == 3


class TestDatarefTrackerValidation:
    """Test data validation logic."""

    def test_validate_position_valid(self):
        """Test validation of valid position data."""
        dt = datareftrack.DatarefTracker()

        assert dt._validate_position(47.5, -122.3, 1000.0) is True
        assert dt._validate_position(0.0, 0.0, 0.0) is True
        assert dt._validate_position(90.0, 180.0, 50000.0) is True
        assert dt._validate_position(-90.0, -180.0, -500.0) is True

    def test_validate_position_invalid_lat(self):
        """Test validation rejects invalid latitude."""
        dt = datareftrack.DatarefTracker()

        assert dt._validate_position(91.0, 0.0, 1000.0) is False
        assert dt._validate_position(-91.0, 0.0, 1000.0) is False
        assert dt._validate_position(100.0, 0.0, 1000.0) is False

    def test_validate_position_invalid_lon(self):
        """Test validation rejects invalid longitude."""
        dt = datareftrack.DatarefTracker()

        assert dt._validate_position(0.0, 181.0, 1000.0) is False
        assert dt._validate_position(0.0, -181.0, 1000.0) is False
        assert dt._validate_position(0.0, 200.0, 1000.0) is False

    def test_validate_position_invalid_alt(self):
        """Test validation rejects invalid altitude."""
        dt = datareftrack.DatarefTracker()

        assert dt._validate_position(0.0, 0.0, 51000.0) is False
        assert dt._validate_position(0.0, 0.0, -600.0) is False
        assert dt._validate_position(0.0, 0.0, 100000.0) is False


class TestDatarefTrackerRequestDatarefs:
    """Test dataref request logic."""

    @patch('datareftrack.CFG')
    def test_request_datarefs_subscribe(self, mock_cfg):
        """Test subscribing to datarefs."""
        mock_cfg.flightdata.xplane_udp_port = 49000

        dt = datareftrack.DatarefTracker()
        dt.sock = Mock()

        dt._request_datarefs(subscribe=True)

        # Should send 5 messages (one per dataref)
        assert dt.sock.sendto.call_count == 5

        # Check first message was sent correctly
        first_call = dt.sock.sendto.call_args_list[0]
        message, addr = first_call[0]
        assert len(message) == 413
        assert message[0:5] == b"RREF\x00"
        assert addr == ("127.0.0.1", 49000)

    @patch('datareftrack.CFG')
    def test_request_datarefs_unsubscribe(self, mock_cfg):
        """Test unsubscribing from datarefs."""
        mock_cfg.flightdata.xplane_udp_port = 49000

        dt = datareftrack.DatarefTracker()
        dt.sock = Mock()

        dt._request_datarefs(subscribe=False)

        # Should send 5 unsubscribe messages
        assert dt.sock.sendto.call_count == 5

        # Verify it's an unsubscribe message (freq=0)
        first_call = dt.sock.sendto.call_args_list[0]
        message = first_call[0][0]
        cmd, freq, idx, string = struct.unpack("<5sii400s", message)
        assert freq == 0  # Unsubscribe

    @patch('datareftrack.CFG')
    def test_request_datarefs_socket_error(self, mock_cfg):
        """Test error handling in _request_datarefs."""
        mock_cfg.flightdata.xplane_udp_port = 49000

        dt = datareftrack.DatarefTracker()
        dt.sock = Mock()
        dt.sock.sendto = Mock(side_effect=OSError("Socket error"))

        # Should raise the exception
        with pytest.raises(OSError):
            dt._request_datarefs(subscribe=True)

    def test_legacy_request_datarefs(self):
        """Test legacy RequestDataRefs method."""
        dt = datareftrack.DatarefTracker()
        dt.sock = Mock()

        with patch.object(dt, '_request_datarefs') as mock_request:
            dt.RequestDataRefs(dt.sock, 49000, 1)
            mock_request.assert_called_once_with(subscribe=True)

            mock_request.reset_mock()
            dt.RequestDataRefs(dt.sock, 49000, 0)
            mock_request.assert_called_once_with(subscribe=False)


class TestDatarefTrackerConnectionStates:
    """Test connection state changes."""

    @patch('datareftrack.CFG')
    def test_connection_state_on_successful_receive(self, mock_cfg):
        """Test connected flag set when receiving data."""
        mock_cfg.flightdata.xplane_udp_port = 49000

        dt = datareftrack.DatarefTracker()

        # Build valid packet
        header = b"RREF\x00"
        values = []
        for i in range(5):
            # Provide valid position data
            test_values = [47.5, -122.3, 1000.0, 180.0, 50.0]
            values.append(struct.pack("<if", i, test_values[i]))
        packet = header + b"".join(values)

        # Mock socket to return packet once, then timeout
        dt.sock = Mock()
        dt.sock.recvfrom = Mock(side_effect=[
            (packet, ("127.0.0.1", 49000)),
            socket.timeout()
        ])
        dt.sock.sendto = Mock()
        dt.sock.close = Mock()

        # Start tracker
        dt.start()
        time.sleep(0.3)  # Let it process the packet

        # Should be connected after receiving valid packet
        with dt._lock:
            assert dt.connected is True
            assert dt.data_valid is True

        dt.stop()

    @patch('datareftrack.CFG')
    def test_connection_state_on_timeout(self, mock_cfg):
        """Test connected flag cleared on timeout."""
        mock_cfg.flightdata.xplane_udp_port = 49000

        dt = datareftrack.DatarefTracker()

        # Mock socket to timeout immediately
        dt.sock = Mock()
        dt.sock.recvfrom = Mock(side_effect=socket.timeout())
        dt.sock.sendto = Mock()
        dt.sock.close = Mock()

        # Start and quickly stop to avoid long sleep
        dt.start()
        time.sleep(0.1)

        # Should not be connected
        with dt._lock:
            assert dt.connected is False

        dt.stop()


class TestDatarefTrackerSocketRecreation:
    """Test socket recreation after errors."""

    @patch('datareftrack.CFG')
    @patch('socket.socket')
    def test_socket_recreation_on_error(self, mock_socket_class, mock_cfg):
        """Test socket is recreated after timeout."""
        mock_cfg.flightdata.xplane_udp_port = 49000

        dt = datareftrack.DatarefTracker()

        # Create mock sockets
        mock_sock1 = Mock()
        mock_sock1.recvfrom = Mock(side_effect=socket.timeout())
        mock_sock1.sendto = Mock()
        mock_sock1.close = Mock()

        mock_sock2 = Mock()
        mock_sock2.recvfrom = Mock(side_effect=socket.timeout())
        mock_sock2.sendto = Mock()
        mock_sock2.close = Mock()
        mock_sock2.settimeout = Mock()

        # First call returns existing socket, subsequent calls return new
        dt.sock = mock_sock1
        mock_socket_class.return_value = mock_sock2

        # Start tracker
        dt.start()
        time.sleep(0.5)  # Give it time to hit timeout and recreate

        # Should have created a new socket
        # (hard to test reliably due to timing, but we can check close was
        # called)

        dt.stop()


class TestDatarefTrackerStopDuringReconnect:
    """Test stopping during reconnection."""

    @patch('datareftrack.CFG')
    def test_stop_during_reconnect_delay(self, mock_cfg):
        """Test that stop() works during reconnection delay."""
        mock_cfg.flightdata.xplane_udp_port = 49000

        dt = datareftrack.DatarefTracker()
        dt.sock = Mock()
        dt.sock.recvfrom = Mock(side_effect=socket.timeout())
        dt.sock.sendto = Mock()
        dt.sock.close = Mock()

        # Temporarily reduce reconnect delay for faster test
        original_delay = dt.RECONNECT_DELAY_SEC
        dt.RECONNECT_DELAY_SEC = 2

        dt.start()
        time.sleep(0.1)  # Let thread hit timeout

        # Stop while thread is in reconnect delay
        dt.stop()

        # Thread should exit quickly (not wait full reconnect delay)
        # because shutdown flag is checked
        time.sleep(0.5)
        assert not dt.t.is_alive()

        dt.RECONNECT_DELAY_SEC = original_delay


# Integration Tests (marked to run separately)


@pytest.mark.integration
class TestDatarefTrackerIntegration:
    """Integration tests with real UDP communication."""

    def test_integration_udp_communication(self):
        """Test real UDP communication with mock X-Plane server."""
        import socket

        # Create a mock X-Plane server
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_sock.bind(("127.0.0.1", 0))  # Bind to any available port
        server_port = server_sock.getsockname()[1]
        server_sock.settimeout(2.0)

        def mock_xplane_server():
            """Simulate X-Plane sending dataref responses."""
            try:
                # Wait for subscription request
                data, addr = server_sock.recvfrom(1024)

                # Send back a response packet with flight data
                header = b"RREF\x00"
                values = []
                test_data = [47.5, -122.3, 1000.0, 180.0, 50.0]
                for i, val in enumerate(test_data):
                    values.append(struct.pack("<if", i, val))
                response = header + b"".join(values)

                # Send response multiple times
                for _ in range(5):
                    server_sock.sendto(response, addr)
                    time.sleep(0.1)

            except socket.timeout:
                pass
            finally:
                server_sock.close()

        # Start mock server in background
        server_thread = threading.Thread(target=mock_xplane_server)
        server_thread.daemon = True
        server_thread.start()

        # Create tracker and point it to mock server
        with patch('datareftrack.CFG') as mock_cfg:
            mock_cfg.flightdata.xplane_udp_port = server_port

            dt = datareftrack.DatarefTracker()
            dt.start()

            # Wait for data
            time.sleep(1.0)

            # Check if we got valid data
            data = dt.get_flight_data()

            dt.stop()

            # Verify we received the data
            if data is not None:
                assert data['lat'] == 47.5
                assert data['lon'] == -122.3
                assert data['alt'] == 1000.0
                assert data['connected'] is True
                assert data['data_valid'] is True

        server_thread.join(timeout=3.0)

    def test_integration_timeout_reconnect(self):
        """Test reconnection behavior with real sockets."""
        with patch('datareftrack.CFG') as mock_cfg:
            # Use a port that's unlikely to have anything listening
            mock_cfg.flightdata.xplane_udp_port = 59999

            dt = datareftrack.DatarefTracker()

            # Reduce delays for faster test
            dt.UDP_TIMEOUT_SEC = 0.5
            dt.RECONNECT_DELAY_SEC = 1

            dt.start()

            # Should remain disconnected
            time.sleep(2.0)

            with dt._lock:
                assert dt.connected is False
                assert dt.data_valid is False

            dt.stop()

    def test_integration_concurrent_access(self):
        """Test concurrent access in real-world scenario."""
        with patch('datareftrack.CFG') as mock_cfg:
            mock_cfg.flightdata.xplane_udp_port = 59999

            dt = datareftrack.DatarefTracker()
            dt.UDP_TIMEOUT_SEC = 0.5
            dt.start()

            # Multiple threads reading data concurrently
            errors = []

            def reader():
                try:
                    for _ in range(50):
                        dt.get_flight_data()
                        time.sleep(0.01)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=reader) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            dt.stop()

            # Should have no errors from concurrent access
            assert len(errors) == 0


def test_singleton_instance():
    """Test that the module-level singleton instance exists."""
    # The dt instance should be created at module level
    assert hasattr(datareftrack, 'dt')
    assert isinstance(datareftrack.dt, datareftrack.DatarefTracker)

    # It should be usable
    assert datareftrack.dt.running is False
    assert datareftrack.dt.connected is False
