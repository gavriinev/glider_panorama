"""
SIYI A8 mini Gimbal Camera SDK (Python, UDP)
Based on A8 mini User Manual v1.6
Protocol: binary frames over UDP to 192.168.144.25:37260
"""

import socket
import struct
import time
import threading
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# CRC-16 (CCITT, poly=0x1021, init=0x0000)
# ---------------------------------------------------------------------------
CRC16_TAB = [
    0x0000,0x1021,0x2042,0x3063,0x4084,0x50a5,0x60c6,0x70e7,
    0x8108,0x9129,0xa14a,0xb16b,0xc18c,0xd1ad,0xe1ce,0xf1ef,
    0x1231,0x0210,0x3273,0x2252,0x52b5,0x4294,0x72f7,0x62d6,
    0x9339,0x8318,0xb37b,0xa35a,0xd3bd,0xc39c,0xf3ff,0xe3de,
    0x2462,0x3443,0x0420,0x1401,0x64e6,0x74c7,0x44a4,0x5485,
    0xa56a,0xb54b,0x8528,0x9509,0xe5ee,0xf5cf,0xc5ac,0xd58d,
    0x3653,0x2672,0x1611,0x0630,0x76d7,0x66f6,0x5695,0x46b4,
    0xb75b,0xa77a,0x9719,0x8738,0xf7df,0xe7fe,0xd79d,0xc7bc,
    0x48c4,0x58e5,0x6886,0x78a7,0x0840,0x1861,0x2802,0x3823,
    0xc9cc,0xd9ed,0xe98e,0xf9af,0x8948,0x9969,0xa90a,0xb92b,
    0x5af5,0x4ad4,0x7ab7,0x6a96,0x1a71,0x0a50,0x3a33,0x2a12,
    0xdbfd,0xcbdc,0xfbbf,0xeb9e,0x9b79,0x8b58,0xbb3b,0xab1a,
    0x6ca6,0x7c87,0x4ce4,0x5cc5,0x2c22,0x3c03,0x0c60,0x1c41,
    0xedae,0xfd8f,0xcdec,0xddcd,0xad2a,0xbd0b,0x8d68,0x9d49,
    0x7e97,0x6eb6,0x5ed5,0x4ef4,0x3e13,0x2e32,0x1e51,0x0e70,
    0xff9f,0xefbe,0xdfdd,0xcffc,0xbf1b,0xaf3a,0x9f59,0x8f78,
    0x9188,0x81a9,0xb1ca,0xa1eb,0xd10c,0xc12d,0xf14e,0xe16f,
    0x1080,0x00a1,0x30c2,0x20e3,0x5004,0x4025,0x7046,0x6067,
    0x83b9,0x9398,0xa3fb,0xb3da,0xc33d,0xd31c,0xe37f,0xf35e,
    0x02b1,0x1290,0x22f3,0x32d2,0x4235,0x5214,0x6277,0x7256,
    0xb5ea,0xa5cb,0x95a8,0x8589,0xf56e,0xe54f,0xd52c,0xc50d,
    0x34e2,0x24c3,0x14a0,0x0481,0x7466,0x6447,0x5424,0x4405,
    0xa7db,0xb7fa,0x8799,0x97b8,0xe75f,0xf77e,0xc71d,0xd73c,
    0x26d3,0x36f2,0x0691,0x16b0,0x6657,0x7676,0x4615,0x5634,
    0xd94c,0xc96d,0xf90e,0xe92f,0x99c8,0x89e9,0xb98a,0xa9ab,
    0x5844,0x4865,0x7806,0x6827,0x18c0,0x08e1,0x3882,0x28a3,
    0xcb7d,0xdb5c,0xeb3f,0xfb1e,0x8bf9,0x9bd8,0xabbb,0xbb9a,
    0x4a75,0x5a54,0x6a37,0x7a16,0x0af1,0x1ad0,0x2ab3,0x3a92,
    0xfd2e,0xed0f,0xdd6c,0xcd4d,0xbdaa,0xad8b,0x9de8,0x8dc9,
    0x7c26,0x6c07,0x5c64,0x4c45,0x3ca2,0x2c83,0x1ce0,0x0cc1,
    0xef1f,0xff3e,0xcf5d,0xdf7c,0xaf9b,0xbfba,0x8fd9,0x9ff8,
    0x6e17,0x7e36,0x4e55,0x5e74,0x2e93,0x3eb2,0x0ed1,0x1ef0,
]


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        tmp = (crc >> 8) & 0xFF
        crc = ((crc << 8) ^ CRC16_TAB[b ^ tmp]) & 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Frame builder / parser
# ---------------------------------------------------------------------------
STX = b'\x55\x66'

def build_frame(cmd_id: int, data: bytes = b'', seq: int = 0) -> bytes:
    """Pack a SIYI SDK frame."""
    ctrl = 0x01          # need_ack = 0, ack_pack = 0, reserved bits
    data_len = len(data)
    header = struct.pack('<2sBHHB', STX, ctrl, data_len, seq, cmd_id)
    payload = header + data
    checksum = struct.pack('<H', crc16(payload))
    return payload + checksum


def parse_frame(raw: bytes) -> Optional[dict]:
    """
    Parse a received SIYI frame.
    Returns dict with keys: ctrl, seq, cmd_id, data
    or None if invalid.
    """
    if len(raw) < 10:
        return None
    if raw[:2] != STX:
        return None
    ctrl, data_len, seq, cmd_id = struct.unpack_from('<BHHB', raw, 2)
    expected_len = 8 + data_len + 2
    if len(raw) < expected_len:
        return None
    data = raw[8: 8 + data_len]
    crc_recv = struct.unpack_from('<H', raw, 8 + data_len)[0]
    crc_calc = crc16(raw[:8 + data_len])
    if crc_recv != crc_calc:
        return None
    return {'ctrl': ctrl, 'seq': seq, 'cmd_id': cmd_id, 'data': data}


# ---------------------------------------------------------------------------
# CMD IDs
# ---------------------------------------------------------------------------
class CMD:
    HEARTBEAT          = 0x00
    FW_VERSION         = 0x01
    HW_ID              = 0x02
    AUTO_FOCUS         = 0x04
    MANUAL_ZOOM        = 0x05
    MANUAL_FOCUS       = 0x06
    GIMBAL_ROTATE      = 0x07
    CENTER             = 0x08
    GIMBAL_CONFIG_INFO = 0x0A
    FUNC_FEEDBACK      = 0x0B
    PHOTO_RECORD       = 0x0C
    GIMBAL_ATTITUDE    = 0x0D
    CONTROL_ANGLE      = 0x0E
    ABS_ZOOM           = 0x0F
    REQUEST_CODEC      = 0x20
    SEND_CODEC         = 0x21
    PUSH_ATTITUDE_FREQ = 0x25
    WORKING_MODE       = 0x19
    MAX_ZOOM           = 0x16
    CUR_ZOOM           = 0x18
    SINGLE_AXIS        = 0x41   # A8 mini only


# ---------------------------------------------------------------------------
# Gimbal attitude dataclass (simple)
# ---------------------------------------------------------------------------
class GimbalAttitude:
    def __init__(self, yaw=0.0, pitch=0.0, roll=0.0,
                 yaw_vel=0.0, pitch_vel=0.0, roll_vel=0.0):
        self.yaw = yaw
        self.pitch = pitch
        self.roll = roll
        self.yaw_vel = yaw_vel
        self.pitch_vel = pitch_vel
        self.roll_vel = roll_vel

    def __repr__(self):
        return (f"GimbalAttitude(yaw={self.yaw:.1f}, pitch={self.pitch:.1f}, "
                f"roll={self.roll:.1f}°)")


# ---------------------------------------------------------------------------
# Main SDK class
# ---------------------------------------------------------------------------
class SIYICamera:
    """
    SIYI A8 mini SDK over UDP.

    Example
    -------
    cam = SIYICamera()
    cam.connect()
    cam.center()
    att = cam.get_attitude()
    print(att)
    cam.set_angle(yaw=30.0, pitch=-20.0)
    cam.take_photo()
    cam.disconnect()
    """

    DEFAULT_IP   = "192.168.144.25"
    DEFAULT_PORT = 37260
    TIMEOUT      = 2.0   # seconds

    def __init__(self, ip: str = DEFAULT_IP, port: int = DEFAULT_PORT):
        self.ip   = ip
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._seq  = 0
        self._lock = threading.Lock()

        # Cached attitude (updated by push thread if enabled)
        self.attitude = GimbalAttitude()
        self._push_thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Open UDP socket."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self.TIMEOUT)
        print(f"[SIYI] Connected to {self.ip}:{self.port} (UDP)")

    def disconnect(self) -> None:
        """Close socket and stop background threads."""
        self._running = False
        if self._push_thread and self._push_thread.is_alive():
            self._push_thread.join(timeout=2)
        if self._sock:
            self._sock.close()
            self._sock = None
        print("[SIYI] Disconnected")

    # ------------------------------------------------------------------
    # Low-level send / receive
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return seq

    def _send(self, cmd_id: int, data: bytes = b'') -> bytes:
        """Send a frame and return raw response bytes (or empty on timeout)."""
        frame = build_frame(cmd_id, data, self._next_seq())
        with self._lock:
            try:
                self._sock.sendto(frame, (self.ip, self.port))
                resp, _ = self._sock.recvfrom(1024)
                return resp
            except socket.timeout:
                return b''

    def _send_parsed(self, cmd_id: int, data: bytes = b'') -> Optional[dict]:
        raw = self._send(cmd_id, data)
        if not raw:
            return None
        return parse_frame(raw)

    # ------------------------------------------------------------------
    # Attitude push (background thread)
    # ------------------------------------------------------------------
    def start_attitude_push(self, freq_hz: int = 10) -> None:
        """
        Ask the gimbal to push attitude data at `freq_hz` Hz.
        Runs a background thread that updates self.attitude.
        """
        # Send frequency config: 0x25
        data = struct.pack('<B', freq_hz)
        self._send(CMD.PUSH_ATTITUDE_FREQ, data)

        self._running = True
        self._push_thread = threading.Thread(
            target=self._attitude_listener, daemon=True)
        self._push_thread.start()
        print(f"[SIYI] Attitude push started @ {freq_hz} Hz")

    def _attitude_listener(self) -> None:
        """Background thread: receive pushed attitude frames."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(1.0)
        while self._running:
            try:
                raw, _ = sock.recvfrom(1024)
                frame = parse_frame(raw)
                if frame and frame['cmd_id'] == CMD.GIMBAL_ATTITUDE:
                    self._decode_attitude(frame['data'])
            except socket.timeout:
                pass
        sock.close()

    def _decode_attitude(self, data: bytes) -> None:
        if len(data) < 12:
            return
        vals = struct.unpack_from('<6h', data)  # 6 × int16_t
        self.attitude = GimbalAttitude(
            yaw       = vals[0] / 10.0,
            pitch     = vals[1] / 10.0,
            roll      = vals[2] / 10.0,
            yaw_vel   = vals[3] / 10.0,
            pitch_vel = vals[4] / 10.0,
            roll_vel  = vals[5] / 10.0,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_firmware_version(self) -> Optional[str]:
        """Request firmware version string."""
        resp = self._send_parsed(CMD.FW_VERSION)
        if resp and resp['data']:
            return resp['data'].decode(errors='replace').strip('\x00')
        return None

    def get_hardware_id(self) -> Optional[str]:
        """Request hardware ID."""
        resp = self._send_parsed(CMD.HW_ID)
        if resp and resp['data']:
            return resp['data'].hex()
        return None

    def get_attitude(self) -> Optional[GimbalAttitude]:
        """Request current gimbal attitude (single poll)."""
        resp = self._send_parsed(CMD.GIMBAL_ATTITUDE)
        if resp:
            self._decode_attitude(resp['data'])
            return self.attitude
        return None

    def center(self) -> bool:
        """Center the gimbal to 0, 0."""
        resp = self._send_parsed(CMD.CENTER, b'\x01')
        return bool(resp and resp['data'] and resp['data'][0] == 1)

    def set_angle(self, yaw: float, pitch: float) -> Optional[GimbalAttitude]:
        """
        Set absolute gimbal angles.
        Yaw:   -135.0 … +135.0 degrees
        Pitch:  -90.0 … +25.0  degrees
        """
        yaw   = max(-135.0, min(135.0, yaw))
        pitch = max(-90.0,  min(25.0,  pitch))
        data  = struct.pack('<hh', int(yaw * 10), int(pitch * 10))
        resp  = self._send_parsed(CMD.CONTROL_ANGLE, data)
        if resp:
            self._decode_attitude(resp['data'])
            return self.attitude
        return None

    def set_angle_single(self, angle: float, axis: str = 'yaw') -> Optional[GimbalAttitude]:
        """
        Single-axis control (A8 mini only).
        axis: 'yaw' or 'pitch'
        """
        axis_flag = 0 if axis.lower() == 'yaw' else 1
        data = struct.pack('<hB', int(angle * 10), axis_flag)
        resp = self._send_parsed(CMD.SINGLE_AXIS, data)
        if resp:
            self._decode_attitude(resp['data'])
            return self.attitude
        return None

    def rotate(self, yaw_speed: int = 0, pitch_speed: int = 0) -> bool:
        """
        Rotate gimbal at given speed (-100 … 0 … 100).
        Send (0, 0) to stop.
        """
        yaw_speed   = max(-100, min(100, yaw_speed))
        pitch_speed = max(-100, min(100, pitch_speed))
        data = struct.pack('<bb', yaw_speed, pitch_speed)
        resp = self._send_parsed(CMD.GIMBAL_ROTATE, data)
        return bool(resp and resp['data'] and resp['data'][0] == 1)

    def stop_rotate(self) -> bool:
        """Stop gimbal rotation."""
        return self.rotate(0, 0)

    def zoom(self, direction: int) -> Optional[float]:
        """
        Manual zoom: direction = 1 (in), -1 (out), 0 (stop).
        Returns current zoom level or None.
        """
        data = struct.pack('<b', max(-1, min(1, direction)))
        resp = self._send_parsed(CMD.MANUAL_ZOOM, data)
        if resp and len(resp['data']) >= 2:
            return struct.unpack_from('<H', resp['data'])[0] / 10.0
        return None

    def zoom_in(self)  -> Optional[float]: return self.zoom(1)
    def zoom_out(self) -> Optional[float]: return self.zoom(-1)
    def zoom_stop(self)-> Optional[float]: return self.zoom(0)

    def set_zoom(self, level: float) -> bool:
        """
        Absolute zoom (1.0 … 6.0 for A8 mini — digital zoom).
        level: e.g. 4.5
        """
        int_part   = int(level)
        float_part = round((level - int_part) * 10)
        data = struct.pack('<BB', int_part, float_part)
        resp = self._send_parsed(CMD.ABS_ZOOM, data)
        return bool(resp and resp['data'] and resp['data'][0] == 1)

    def get_zoom(self) -> Optional[float]:
        """Get current zoom level."""
        resp = self._send_parsed(CMD.CUR_ZOOM)
        if resp and len(resp['data']) >= 2:
            return resp['data'][0] + resp['data'][1] / 10.0
        return None

    def get_max_zoom(self) -> Optional[float]:
        """Get maximum available zoom level."""
        resp = self._send_parsed(CMD.MAX_ZOOM)
        if resp and len(resp['data']) >= 2:
            return resp['data'][0] + resp['data'][1] / 10.0
        return None

    def take_photo(self) -> bool:
        """Take a photo (requires TF card)."""
        self._send(CMD.PHOTO_RECORD, b'\x00')
        return True

    def start_recording(self) -> bool:
        """Start video recording (requires TF card)."""
        self._send(CMD.PHOTO_RECORD, b'\x02')
        return True

    def stop_recording(self) -> bool:
        """Stop video recording."""
        self._send(CMD.PHOTO_RECORD, b'\x02')  # toggle
        return True

    def set_mode_lock(self)   -> None: self._send(CMD.PHOTO_RECORD, b'\x03')
    def set_mode_follow(self) -> None: self._send(CMD.PHOTO_RECORD, b'\x04')
    def set_mode_fpv(self)    -> None: self._send(CMD.PHOTO_RECORD, b'\x05')

    def enable_hdmi(self)  -> None: self._send(CMD.PHOTO_RECORD, b'\x06')
    def enable_cvbs(self)  -> None: self._send(CMD.PHOTO_RECORD, b'\x07')
    def disable_video_output(self) -> None: self._send(CMD.PHOTO_RECORD, b'\x08')

    def get_config(self) -> Optional[dict]:
        """
        Request gimbal configuration.
        Returns dict with: hdr, recording, motion_mode, mounting, video_output
        """
        resp = self._send_parsed(CMD.GIMBAL_CONFIG_INFO)
        if not resp or len(resp['data']) < 7:
            return None
        d = resp['data']
        motion_modes = {0: 'lock', 1: 'follow', 2: 'fpv'}
        mounting     = {0: 'reserved', 1: 'normal', 2: 'upside_down'}
        video_out    = {0: 'hdmi', 1: 'cvbs'}
        return {
            'hdr':          bool(d[1]),
            'recording':    d[3],   # 0=off,1=on,2=no_tf,3=data_loss
            'motion_mode':  motion_modes.get(d[4], 'unknown'),
            'mounting':     mounting.get(d[5], 'unknown'),
            'video_output': video_out.get(d[6], 'unknown'),
        }


# ---------------------------------------------------------------------------
# Quick demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cam = SIYICamera()
    cam.connect()

    print("Firmware:", cam.get_firmware_version())
    print("HW ID:   ", cam.get_hardware_id())

    att = cam.get_attitude()
    print("Attitude:", att)

    print("Centering...")
    cam.center()
    time.sleep(2)

    print("Point down (pitch=-90)...")
    cam.set_angle(yaw=0.0, pitch=-45.0)
    time.sleep(2)

    print("Config:", cam.get_config())
    print("Zoom level:", cam.get_zoom())

    cam.disconnect()
