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

class FakeLSM303Bus(FakeIMUBus):
    def read_byte_data(self, addr, register):
        raise OSError('gyro not installed')

def test_lsm303dlhc_logs_without_gyro():
    bus = FakeLSM303Bus()
    sensor = mod.MiniIMU9V2(bus, 0x19, 0x1E, [0x6B, 0x6A])
    data = sensor.read()

    assert sensor.gyro_addr is None
    assert math.isclose(data['accel_x_g'], 1.024)
    assert data['gyro_x_raw'] is None
    assert data['gyro_x_dps'] is None
    assert math.isclose(data['mag_x_gauss'], 1.0)
    assert not any(write[0] in (0x6A, 0x6B) for write in bus.writes)

def test_parse_iw_scan_hotspots():
    text = '''
BSS 30:57:8e:ef:7a:a3(on wlan0)
        freq: 2412
        signal: -47.00 dBm
        SSID: OurPlace
BSS c8:c6:fe:27:c6:83(on wlan0)
        freq: 5180
        signal: -51.00 dBm
        SSID:
BSS Load:
        signal: -1.00 dBm
'''
    rows = mod.parse_iw_scan(text, 'wlan0')

    assert rows == [
        {'iface':'wlan0','bssid':'30:57:8e:ef:7a:a3','freq_mhz':2412,'channel':1,'signal_dbm':-47.0,'ssid':'OurPlace'},
        {'iface':'wlan0','bssid':'c8:c6:fe:27:c6:83','freq_mhz':5180,'channel':36,'signal_dbm':-51.0,'ssid':'(hidden)'}
    ]

def test_wifi_channel_mapping():
    assert mod.wifi_channel(2412) == 1
    assert mod.wifi_channel(2484) == 14
    assert mod.wifi_channel(5180) == 36
    assert mod.wifi_channel(5955) == 1
    assert mod.wifi_channel(1234) is None

class FakeUPSHatBus:
    def __init__(self):
        self.writes = []
        self.registers = {
            0x01: [0xFC, 0x18],
            0x02: [0x38, 0x40],
            0x03: [0x07, 0x08],
            0x04: [0xEC, 0x78]
        }

    def write_i2c_block_data(self, addr, register, data):
        self.writes.append((addr, register, data))

    def read_i2c_block_data(self, addr, register, length):
        assert addr == 0x42
        assert length == 2
        return self.registers[register]

def test_waveshare_ups_hat_read_and_scale():
    bus = FakeUPSHatBus()
    sensor = mod.WaveshareUPSHat(bus, 0x42, 6.0, 8.4)
    data = sensor.read()

    assert (0x42, 0x05, [0x10, 0x00]) in bus.writes
    assert (0x42, 0x00, [0x39, 0x9F]) in bus.writes
    assert math.isclose(data['bus_voltage_v'], 7.2)
    assert math.isclose(data['shunt_voltage_mv'], -10.0)
    assert math.isclose(data['supply_voltage_v'], 7.19)
    assert math.isclose(data['current_ma'], -500.0)
    assert math.isclose(data['power_w'], 3.6)
    assert math.isclose(data['battery_percent'], 50.0)
    assert data['state'] == 'discharging'

def test_waveshare_ups_hat_rejects_invalid_voltage_range():
    try:
        mod.WaveshareUPSHat(FakeUPSHatBus(), 0x42, 8.4, 6.0)
        assert False
    except ValueError:
        pass

def test_parse_btmgmt_devices():
    output = '''
Discovery started
hci0 dev_found: AA:BB:CC:DD:EE:01 type LE Random rssi -72 flags 0x0000 name Bike Sensor
hci0 dev_found: AA:BB:CC:DD:EE:02 type BR/EDR rssi -41 flags 0x0000 eir_len 12
hci0 dev_found: AA:BB:CC:DD:EE:01 type LE Random rssi -58 flags 0x0000 name Bike Sensor
'''
    rows = mod.parse_btmgmt_devices(output)

    assert len(rows) == 2
    assert rows[0]['address'] == 'AA:BB:CC:DD:EE:02'
    assert rows[0]['address_type'] == 'BR/EDR'
    assert rows[1]['address'] == 'AA:BB:CC:DD:EE:01'
    assert rows[1]['name'] == 'Bike Sensor'
    assert rows[1]['rssi_dbm'] == -58

def test_parse_btmgmt_devices_multiline_name_and_ansi():
    output = '\x1b[0mhci1 dev_found: 11:22:33:44:55:66 type LE Public rssi -63 flags 0x0004\nname Heart Rate Monitor\n'
    rows = mod.parse_btmgmt_devices(output)

    assert rows == [{
        'adapter':'hci1',
        'address':'11:22:33:44:55:66',
        'address_type':'LE Public',
        'rssi_dbm':-63,
        'flags':'0x0004',
        'raw_lines':[
            'hci1 dev_found: 11:22:33:44:55:66 type LE Public rssi -63 flags 0x0004',
            'name Heart Rate Monitor'
        ],
        'name':'Heart Rate Monitor'
    }]

def test_dashboard_helpers_escape_content():
    table = mod.data_table([{'name':'<script>','rssi':-40}],[('name','Name'),('rssi','RSSI')])
    page = mod.page('Test <title>', table, refresh=5)

    assert '&lt;script&gt;' in page
    assert '<script>' not in page
    assert 'content="5"' in page

def test_camera_transform_options():
    assert mod.camera_transform_options(0) == {'hflip':0,'vflip':0}
    assert mod.camera_transform_options(180) == {'hflip':1,'vflip':1}
    try:
        mod.camera_transform_options(90)
        assert False
    except ValueError:
        pass
