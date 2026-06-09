import importlib.util
import math
import os
from pathlib import Path

os.environ['BIKELOGGER_DATA'] = '/tmp/bikelogger-test-data'
os.environ['BIKELOGGER_LOG'] = '/tmp/bikelogger-test-log'

spec = importlib.util.spec_from_file_location('bikelogger', str(Path(__file__).resolve().parents[1] / 'bikelogger' / 'bikelogger.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def test_parse_gga():
    raw = '$GNGGA,014217.00,2929.69095,N,09507.78775,W,2,12,1.03,5.5,M,-24.1,M,,*7B'
    d = mod.parse_nmea(raw)
    assert round(d['lat'], 6) == 29.494849
    assert round(d['lon'], 6) == -95.129796
    assert d['fix_quality'] == 2
    assert d['sats'] == 12

def test_signed_16():
    assert mod.signed_16(0x34, 0x12) == 0x1234
    assert mod.signed_16(0x00, 0x80) == -32768
    assert mod.signed_16_be(0xFF, 0xFE) == -2

class FakeIMUBus:
    def __init__(self):
        self.writes = []

    def read_byte_data(self, addr, register):
        if addr == 0x6B and register == 0x0F:
            return 0xD4
        raise OSError('device not present')

    def write_byte_data(self, addr, register, value):
        self.writes.append((addr, register, value))

    def read_i2c_block_data(self, addr, register, length):
        assert length == 6
        if addr == 0x19:
            return [0x00, 0x40, 0x00, 0xE0, 0x00, 0x10]
        if addr == 0x6B:
            return [0x64, 0x00, 0x9C, 0xFF, 0x00, 0x80]
        if addr == 0x1E:
            return [0x04, 0x4C, 0xFC, 0x2C, 0x02, 0x26]
        raise OSError('unexpected address')

def test_minimu9_v2_read_and_scale():
    bus = FakeIMUBus()
    sensor = mod.MiniIMU9V2(bus, 0x19, 0x1E, [0x6B, 0x6A])
    data = sensor.read()

    assert sensor.gyro_addr == 0x6B
    assert data['accel_x_raw'] == 1024
    assert data['accel_y_raw'] == -512
    assert data['accel_z_raw'] == 256
    assert math.isclose(data['accel_x_g'], 1.024)
    assert data['gyro_x_raw'] == 100
    assert data['gyro_y_raw'] == -100
    assert data['gyro_z_raw'] == -32768
    assert math.isclose(data['gyro_x_dps'], 0.875)
    assert data['mag_x_raw'] == 1100
    assert data['mag_y_raw'] == 550
    assert data['mag_z_raw'] == -980
    assert math.isclose(data['mag_x_gauss'], 1.0)
    assert math.isclose(data['mag_y_gauss'], 0.5)
    assert math.isclose(data['mag_z_gauss'], -1.0)
