#!/usr/bin/env python3
"""BikeLogger Pi 4: lightweight ride logger + RideHub web UI.
Features: GPIO start/stop, serial GPS NMEA at 38400, BME280, Pololu LSM303DLHC,
Waveshare UPS HAT power monitoring, persistent Picamera2 autofocus capture,
plain-text WiFi scans, built-in Bluetooth device/RSSI scans, health logging, per-ride SQLite, local web history browser.
"""
import os, sys, time, json, math, glob, sqlite3, threading, subprocess, shutil, signal, html, re
from datetime import datetime, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

APP_DIR = Path('/opt/bikelogger')
DATA_DIR = Path(os.environ.get('BIKELOGGER_DATA', '/var/lib/bikelogger'))
RIDES_DIR = DATA_DIR / 'rides'
LOG_DIR = Path(os.environ.get('BIKELOGGER_LOG', '/var/log/bikelogger'))
CONFIG_PATH = APP_DIR / 'config.json'
VERSION_PATH = APP_DIR / 'version.json'
RUNNING = True

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')

def utc_id():
    return datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

def load_version():
    try:
        version=json.loads(VERSION_PATH.read_text())
        return {
            'commit':str(version.get('commit') or 'unknown'),
            'installed_at':version.get('installed_at')
        }
    except Exception:
        return {'commit':os.environ.get('BIKELOGGER_VERSION','development'),'installed_at':None}

def load_config():
    default = {
        'web_host': '0.0.0.0', 'web_port': 8080,
        'gps_port': '/dev/serial0', 'gps_baud': 38400,
        'camera_interval_sec': 5.0, 'camera_width': 2304, 'camera_height': 1296,
        'camera_quality': 92, 'camera_autofocus': 'continuous', 'camera_rotation_degrees': 180,
        'env_interval_sec': 2.0, 'gps_interval_note': 'GPS logs as NMEA sentences arrive',
        'wifi_interval_sec': 30.0, 'bluetooth_interval_sec': 60.0, 'bluetooth_scan_duration_sec': 10.0, 'health_interval_sec': 10.0,
        'bluetooth_adapter_index': 0,
        'start_button_gpio': 27, 'stop_button_gpio': 17,
        'button_bounce_sec': 0.15,
        'i2c_bus': 1, 'bme280_addresses': [0x76, 0x77],
        'imu_interval_sec': 0.1,
        'imu_accel_address': 0x19, 'imu_mag_address': 0x1E,
        'imu_gyro_addresses': [0x6B, 0x6A],
        'ups_interval_sec': 2.0, 'ups_model': 'D', 'ups_address': 0x43,
        'ups_empty_voltage': 3.0, 'ups_full_voltage': 4.2,
        'capture_when_idle': False,
        'log_plain_wifi_ssid': True,
        'log_bluetooth_names': True,
        'rsync_target': ''
    }
    if CONFIG_PATH.exists():
        try:
            default.update(json.loads(CONFIG_PATH.read_text()))
        except Exception as e:
            print(f'CONFIG WARNING: {e}', flush=True)
    return default

CONFIG = load_config()
APP_VERSION = load_version()
RIDES_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.ride_id = None
        self.ride_dir = None
        self.db_path = None
        self.db = None
        self.started_at = None
        self.last_gps = {}
        self.last_env = {}
        self.last_imu = {}
        self.last_ups = {}
        self.last_health = {}
        self.last_camera = {}
        self.last_wifi_count = 0
        self.last_wifi_devices = []
        self.last_wifi_scan_at = None
        self.last_bluetooth_count = 0
        self.last_bluetooth_devices = []
        self.bluetooth_status = {'state':'not scanned'}
        self.camera_status = 'not initialized'
        self.errors = []
    def add_error(self, msg):
        with self.lock:
            text = f'{now_iso()} {msg}'
            print('ERROR:', text, flush=True)
            self.errors.append(text)
            self.errors = self.errors[-50:]
STATE = State()

def db_exec(sql, params=()):
    with STATE.lock:
        if not STATE.db: return
        try:
            STATE.db.execute(sql, params)
            STATE.db.commit()
        except Exception as e:
            STATE.add_error(f'DB: {e}')

def init_db(path):
    db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    db.execute('pragma journal_mode=WAL')
    db.execute('pragma synchronous=NORMAL')
    schema = [
        '''create table if not exists meta(key text primary key, value text)''',
        '''create table if not exists events(ts text, type text, message text, data_json text)''',
        '''create table if not exists gps(ts text, lat real, lon real, alt_m real, speed_knots real, course_deg real, fix_quality integer, sats integer, hdop real, raw text)''',
        '''create table if not exists environment(ts text, sensor text, temperature_c real, humidity_pct real, pressure_hpa real, data_json text)''',
        '''create table if not exists imu(ts text, accel_x_raw integer, accel_y_raw integer, accel_z_raw integer, accel_x_g real, accel_y_g real, accel_z_g real, gyro_x_raw integer, gyro_y_raw integer, gyro_z_raw integer, gyro_x_dps real, gyro_y_dps real, gyro_z_dps real, mag_x_raw integer, mag_y_raw integer, mag_z_raw integer, mag_x_gauss real, mag_y_gauss real, mag_z_gauss real, data_json text)''',
        '''create table if not exists ups(ts text, model text, address text, bus_voltage_v real, shunt_voltage_mv real, supply_voltage_v real, current_ma real, power_w real, battery_percent real, state text, data_json text)''',
        '''create table if not exists wifi(ts text, iface text, bssid text, ssid text, signal_dbm real, freq_mhz integer, channel integer, data_json text)''',
        '''create table if not exists bluetooth(ts text, adapter text, address text, address_type text, name text, rssi_dbm real, flags text, data_json text)''',
        '''create table if not exists camera(ts text, file text, width integer, height integer, status text, data_json text)''',
        '''create table if not exists health(ts text, cpu_temp_c real, throttled text, load1 real, mem_available_kb integer, disk_free_mb integer, data_json text)'''
    ]
    for s in schema: db.execute(s)
    return db

def start_ride(reason='manual'):
    with STATE.lock:
        if STATE.ride_id:
            return STATE.ride_id
        ride_id = utc_id()
        ride_dir = RIDES_DIR / ride_id
        (ride_dir / 'photos').mkdir(parents=True, exist_ok=True)
        (ride_dir / 'thumbs').mkdir(parents=True, exist_ok=True)
        db_path = ride_dir / 'ride.sqlite'
        db = init_db(db_path)
        STATE.ride_id, STATE.ride_dir, STATE.db_path, STATE.db = ride_id, ride_dir, db_path, db
        STATE.started_at = now_iso()
        meta = {'ride_id': ride_id, 'started_at': STATE.started_at, 'config': CONFIG, 'reason': reason}
        for k,v in meta.items(): db.execute('insert or replace into meta values(?,?)', (k, json.dumps(v) if not isinstance(v,str) else v))
        db.execute('insert into events values(?,?,?,?)', (now_iso(), 'ride_start', reason, json.dumps(meta)))
        db.commit()
        print(f'RIDE START {ride_id}', flush=True)
        return ride_id

def stop_ride(reason='manual'):
    with STATE.lock:
        if not STATE.ride_id:
            return None
        rid = STATE.ride_id
        try:
            STATE.db.execute('insert into events values(?,?,?,?)', (now_iso(), 'ride_stop', reason, '{}'))
            STATE.db.execute('insert or replace into meta values(?,?)', ('stopped_at', now_iso()))
            STATE.db.commit(); STATE.db.close()
        except Exception as e:
            print(f'DB close warning: {e}', flush=True)
        STATE.ride_id = STATE.ride_dir = STATE.db_path = STATE.db = STATE.started_at = None
        print(f'RIDE STOP {rid}', flush=True)
        return rid

def nmea_latlon(value, hemi):
    if not value or not hemi: return None
    try:
        dot = value.find('.')
        deg_len = dot - 2
        deg = float(value[:deg_len]); minutes = float(value[deg_len:])
        dec = deg + minutes/60.0
        if hemi in ('S','W'): dec = -dec
        return dec
    except Exception:
        return None

def parse_nmea(line):
    line=line.strip()
    if not line.startswith('$'): return None
    parts=line.split(',')
    typ=parts[0][-3:]
    try:
        if typ == 'GGA' and len(parts) > 9:
            return {'lat': nmea_latlon(parts[2], parts[3]), 'lon': nmea_latlon(parts[4], parts[5]),
                    'fix_quality': int(parts[6] or 0), 'sats': int(parts[7] or 0), 'hdop': float(parts[8] or 0),
                    'alt_m': float(parts[9] or 0), 'raw': line}
        if typ == 'RMC' and len(parts) > 8:
            return {'lat': nmea_latlon(parts[3], parts[4]), 'lon': nmea_latlon(parts[5], parts[6]),
                    'speed_knots': float(parts[7] or 0), 'course_deg': float(parts[8] or 0), 'raw': line}
    except Exception:
        return {'raw': line}
    return {'raw': line}

class GPSWorker(threading.Thread):
    daemon=True
    def run(self):
        try:
            import serial
        except Exception as e:
            STATE.add_error(f'GPS serial module missing: {e}'); return
        port=CONFIG['gps_port']; baud=int(CONFIG['gps_baud'])
        while RUNNING:
            try:
                with serial.Serial(port, baudrate=baud, timeout=2) as ser:
                    print(f'GPS opened {port} {baud}', flush=True)
                    while RUNNING:
                        raw=ser.readline().decode('ascii','ignore').strip()
                        if not raw: continue
                        data=parse_nmea(raw)
                        if not data: continue
                        ts=now_iso()
                        with STATE.lock:
                            STATE.last_gps.update({k:v for k,v in data.items() if v is not None})
                            lg=STATE.last_gps.copy()
                        if STATE.db:
                            db_exec('insert into gps values(?,?,?,?,?,?,?,?,?,?)', (ts, lg.get('lat'), lg.get('lon'), lg.get('alt_m'), lg.get('speed_knots'), lg.get('course_deg'), lg.get('fix_quality'), lg.get('sats'), lg.get('hdop'), raw))
            except Exception as e:
                STATE.add_error(f'GPS {port}: {e}')
                time.sleep(5)

class BME280:
    def __init__(self, bus, addr):
        self.bus, self.addr = bus, addr
        bus.write_byte_data(addr, 0xF2, 0x01) # humidity x1
        bus.write_byte_data(addr, 0xF4, 0x27) # temp/pressure x1 normal
        bus.write_byte_data(addr, 0xF5, 0xA0)
        self.cal = self._read_cal()
        self.t_fine = 0
    def u16(self, r):
        b=self.bus.read_i2c_block_data(self.addr, r, 2); return b[0] | (b[1]<<8)
    def s16(self, r):
        v=self.u16(r); return v-65536 if v>32767 else v
    def _read_cal(self):
        c={}
        c['T1']=self.u16(0x88); c['T2']=self.s16(0x8A); c['T3']=self.s16(0x8C)
        c['P1']=self.u16(0x8E); c['P2']=self.s16(0x90); c['P3']=self.s16(0x92); c['P4']=self.s16(0x94); c['P5']=self.s16(0x96); c['P6']=self.s16(0x98); c['P7']=self.s16(0x9A); c['P8']=self.s16(0x9C); c['P9']=self.s16(0x9E)
        c['H1']=self.bus.read_byte_data(self.addr,0xA1)
        c['H2']=self.s16(0xE1); c['H3']=self.bus.read_byte_data(self.addr,0xE3)
        e4=self.bus.read_byte_data(self.addr,0xE4); e5=self.bus.read_byte_data(self.addr,0xE5); e6=self.bus.read_byte_data(self.addr,0xE6)
        c['H4']=(e4<<4)|(e5&0x0F); c['H4']=c['H4']-4096 if c['H4']>2047 else c['H4']
        c['H5']=(e6<<4)|(e5>>4); c['H5']=c['H5']-4096 if c['H5']>2047 else c['H5']
        h6=self.bus.read_byte_data(self.addr,0xE7); c['H6']=h6-256 if h6>127 else h6
        return c
    def read(self):
        d=self.bus.read_i2c_block_data(self.addr,0xF7,8)
        adc_p=(d[0]<<12)|(d[1]<<4)|(d[2]>>4); adc_t=(d[3]<<12)|(d[4]<<4)|(d[5]>>4); adc_h=(d[6]<<8)|d[7]; c=self.cal
        v1=(((adc_t>>3)-(c['T1']<<1))*c['T2'])>>11
        v2=(((((adc_t>>4)-c['T1'])*((adc_t>>4)-c['T1']))>>12)*c['T3'])>>14
        self.t_fine=v1+v2; temp=((self.t_fine*5+128)>>8)/100.0
        var1=self.t_fine-128000; var2=var1*var1*c['P6']; var2=var2+((var1*c['P5'])<<17); var2=var2+(c['P4']<<35); var1=((var1*var1*c['P3'])>>8)+((var1*c['P2'])<<12); var1=((((1<<47)+var1))*c['P1'])>>33
        pressure=None
        if var1 != 0:
            p=1048576-adc_p; p=(((p<<31)-var2)*3125)//var1; var1=(c['P9']*(p>>13)*(p>>13))>>25; var2=(c['P8']*p)>>19; p=((p+var1+var2)>>8)+(c['P7']<<4); pressure=p/25600.0
        h=self.t_fine-76800.0
        hum=(adc_h-(c['H4']*64.0+c['H5']/16384.0*h))*(c['H2']/65536.0*(1.0+c['H6']/67108864.0*h*(1.0+c['H3']/67108864.0*h)))
        hum=hum*(1.0-c['H1']*hum/524288.0); hum=max(0,min(100,hum))
        return temp, hum, pressure

class EnvWorker(threading.Thread):
    daemon=True
    def run(self):
        sensors=[]
        try:
            try: import smbus
            except Exception: import smbus2 as smbus
            bus=smbus.SMBus(int(CONFIG['i2c_bus']))
            for addr in CONFIG['bme280_addresses']:
                try:
                    chip=bus.read_byte_data(addr,0xD0)
                    if chip in (0x60,0x58): sensors.append((f'BME280@0x{addr:02x}', BME280(bus, addr)))
                except Exception: pass
            if not sensors: STATE.add_error('No BME280/BMP280 found at 0x76/0x77')
        except Exception as e:
            STATE.add_error(f'I2C/BME init: {e}'); return
        while RUNNING:
            for name,s in sensors:
                try:
                    t,h,p=s.read(); ts=now_iso(); data={'temperature_c':t,'humidity_pct':h,'pressure_hpa':p}
                    with STATE.lock: STATE.last_env[name]=data
                    if STATE.db: db_exec('insert into environment values(?,?,?,?,?,?)',(ts,name,t,h,p,json.dumps(data)))
                except Exception as e: STATE.add_error(f'{name}: {e}')
            time.sleep(float(CONFIG['env_interval_sec']))

def signed_16(lo, hi):
    value=(hi << 8) | lo
    return value-65536 if value > 32767 else value

def signed_16_be(hi, lo):
    return signed_16(lo, hi)

class MiniIMU9V2:
    ACCEL_SCALE_G = 0.001
    GYRO_SCALE_DPS = 0.00875
    MAG_XY_SCALE_GAUSS = 1.0 / 1100.0
    MAG_Z_SCALE_GAUSS = 1.0 / 980.0

    def __init__(self, bus, accel_addr, mag_addr, gyro_addresses):
        self.bus = bus
        self.accel_addr = accel_addr
        self.mag_addr = mag_addr
        self.gyro_addr = None
        for addr in gyro_addresses:
            try:
                if bus.read_byte_data(addr, 0x0F) == 0xD4:
                    self.gyro_addr = addr
                    break
            except Exception:
                pass

        bus.write_byte_data(self.accel_addr, 0x20, 0x57)
        bus.write_byte_data(self.accel_addr, 0x23, 0x88)
        if self.gyro_addr is not None:
            bus.write_byte_data(self.gyro_addr, 0x20, 0x0F)
            bus.write_byte_data(self.gyro_addr, 0x23, 0x80)
        bus.write_byte_data(self.mag_addr, 0x00, 0x14)
        bus.write_byte_data(self.mag_addr, 0x01, 0x20)
        bus.write_byte_data(self.mag_addr, 0x02, 0x00)

    def read(self):
        accel=self.bus.read_i2c_block_data(self.accel_addr, 0x28 | 0x80, 6)
        mag=self.bus.read_i2c_block_data(self.mag_addr, 0x03, 6)

        ax=signed_16(accel[0], accel[1]) >> 4
        ay=signed_16(accel[2], accel[3]) >> 4
        az=signed_16(accel[4], accel[5]) >> 4
        gx=gy=gz=None
        if self.gyro_addr is not None:
            gyro=self.bus.read_i2c_block_data(self.gyro_addr, 0x28 | 0x80, 6)
            gx=signed_16(gyro[0], gyro[1])
            gy=signed_16(gyro[2], gyro[3])
            gz=signed_16(gyro[4], gyro[5])
        mx=signed_16_be(mag[0], mag[1])
        mz=signed_16_be(mag[2], mag[3])
        my=signed_16_be(mag[4], mag[5])

        return {
            'accel_x_raw':ax, 'accel_y_raw':ay, 'accel_z_raw':az,
            'accel_x_g':ax*self.ACCEL_SCALE_G, 'accel_y_g':ay*self.ACCEL_SCALE_G, 'accel_z_g':az*self.ACCEL_SCALE_G,
            'gyro_x_raw':gx, 'gyro_y_raw':gy, 'gyro_z_raw':gz,
            'gyro_x_dps':gx*self.GYRO_SCALE_DPS if gx is not None else None,
            'gyro_y_dps':gy*self.GYRO_SCALE_DPS if gy is not None else None,
            'gyro_z_dps':gz*self.GYRO_SCALE_DPS if gz is not None else None,
            'mag_x_raw':mx, 'mag_y_raw':my, 'mag_z_raw':mz,
            'mag_x_gauss':mx*self.MAG_XY_SCALE_GAUSS, 'mag_y_gauss':my*self.MAG_XY_SCALE_GAUSS, 'mag_z_gauss':mz*self.MAG_Z_SCALE_GAUSS
        }

class IMUWorker(threading.Thread):
    daemon=True
    def run(self):
        try:
            try: import smbus
            except Exception: import smbus2 as smbus
            bus=smbus.SMBus(int(CONFIG['i2c_bus']))
            sensor=MiniIMU9V2(
                bus,
                int(CONFIG['imu_accel_address']),
                int(CONFIG['imu_mag_address']),
                [int(addr) for addr in CONFIG['imu_gyro_addresses']]
            )
            gyro_status=f'0x{sensor.gyro_addr:02x}' if sensor.gyro_addr is not None else 'not installed'
            print(f'Pololu LSM303DLHC ready: accel 0x{sensor.accel_addr:02x}, mag 0x{sensor.mag_addr:02x}, gyro {gyro_status}', flush=True)
        except Exception as e:
            STATE.add_error(f'Pololu LSM303DLHC init: {e}'); return
        while RUNNING:
            try:
                data=sensor.read(); ts=now_iso()
                with STATE.lock: STATE.last_imu=data
                if STATE.db:
                    db_exec(
                        'insert into imu values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (ts,
                         data['accel_x_raw'],data['accel_y_raw'],data['accel_z_raw'],
                         data['accel_x_g'],data['accel_y_g'],data['accel_z_g'],
                         data['gyro_x_raw'],data['gyro_y_raw'],data['gyro_z_raw'],
                         data['gyro_x_dps'],data['gyro_y_dps'],data['gyro_z_dps'],
                         data['mag_x_raw'],data['mag_y_raw'],data['mag_z_raw'],
                         data['mag_x_gauss'],data['mag_y_gauss'],data['mag_z_gauss'],
                         json.dumps(data))
                    )
            except Exception as e:
                STATE.add_error(f'Pololu LSM303DLHC read: {e}')
            time.sleep(max(0.02, float(CONFIG['imu_interval_sec'])))

class WaveshareUPSHat:
    PROFILES = {
        'B': {
            'calibration':4096, 'config':0x399F,
            'current_lsb_ma':0.1, 'power_lsb_w':0.002,
            'reverse_current':False
        },
        'D': {
            'calibration':26868, 'config':0x0EEF,
            'current_lsb_ma':0.1524, 'power_lsb_w':0.003048,
            'reverse_current':True
        }
    }

    def __init__(self, bus, addr, empty_voltage, full_voltage, model='D'):
        self.bus = bus
        self.addr = addr
        self.empty_voltage = empty_voltage
        self.full_voltage = full_voltage
        self.model = str(model).upper()
        if self.model not in self.PROFILES:
            raise ValueError(f'Unsupported Waveshare UPS HAT model: {self.model}')
        self.profile = self.PROFILES[self.model]
        if self.full_voltage <= self.empty_voltage:
            raise ValueError('UPS full voltage must be greater than empty voltage')
        self.write_register(0x05, self.profile['calibration'])
        self.write_register(0x00, self.profile['config'])
        self.read_register(0x02)

    def read_register(self, register):
        data=self.bus.read_i2c_block_data(self.addr, register, 2)
        return (data[0] << 8) | data[1]

    def write_register(self, register, value):
        self.bus.write_i2c_block_data(self.addr, register, [(value >> 8) & 0xFF, value & 0xFF])

    def read(self):
        self.write_register(0x05, self.profile['calibration'])
        shunt_raw=self.read_register(0x01)
        bus_raw=self.read_register(0x02)
        current_raw=self.read_register(0x04)
        power_raw=self.read_register(0x03)

        shunt_mv=(shunt_raw-65536 if shunt_raw > 32767 else shunt_raw)*0.01
        bus_voltage=(bus_raw >> 3)*0.004
        supply_voltage=bus_voltage+(shunt_mv/1000.0)
        current_ma=(current_raw-65536 if current_raw > 32767 else current_raw)*self.profile['current_lsb_ma']
        if self.profile['reverse_current']:
            current_ma=-current_ma
        power_w=power_raw*self.profile['power_lsb_w']
        battery_percent=(bus_voltage-self.empty_voltage)/(self.full_voltage-self.empty_voltage)*100.0
        battery_percent=max(0.0,min(100.0,battery_percent))
        state='charging' if current_ma > 10.0 else 'discharging' if current_ma < -10.0 else 'idle'
        return {
            'model':f'Waveshare UPS HAT ({self.model})', 'address':f'0x{self.addr:02x}',
            'bus_voltage_v':bus_voltage, 'shunt_voltage_mv':shunt_mv,
            'supply_voltage_v':supply_voltage, 'current_ma':current_ma,
            'power_w':power_w, 'battery_percent':battery_percent, 'state':state
        }

class UPSWorker(threading.Thread):
    daemon=True
    def run(self):
        try:
            try: import smbus
            except Exception: import smbus2 as smbus
            bus=smbus.SMBus(int(CONFIG['i2c_bus']))
            sensor=WaveshareUPSHat(
                bus,
                int(CONFIG['ups_address']),
                float(CONFIG['ups_empty_voltage']),
                float(CONFIG['ups_full_voltage']),
                CONFIG.get('ups_model','D')
            )
            print(f'Waveshare UPS HAT ({sensor.model}) ready at 0x{sensor.addr:02x}', flush=True)
        except Exception as e:
            STATE.add_error(f'Waveshare UPS HAT init: {e}'); return
        while RUNNING:
            try:
                data=sensor.read(); ts=now_iso()
                with STATE.lock: STATE.last_ups=data
                if STATE.db:
                    db_exec(
                        'insert into ups values(?,?,?,?,?,?,?,?,?,?,?)',
                        (ts,data['model'],data['address'],data['bus_voltage_v'],
                         data['shunt_voltage_mv'],data['supply_voltage_v'],
                         data['current_ma'],data['power_w'],data['battery_percent'],
                         data['state'],json.dumps(data))
                    )
            except Exception as e:
                STATE.add_error(f'Waveshare UPS HAT read: {e}')
            time.sleep(max(0.2, float(CONFIG['ups_interval_sec'])))

def camera_transform_options(rotation_degrees):
    rotation=int(rotation_degrees)
    if rotation == 0:
        return {'hflip':0,'vflip':0}
    if rotation == 180:
        return {'hflip':1,'vflip':1}
    raise ValueError(f'camera_rotation_degrees must be 0 or 180, got {rotation}')

class CameraWorker(threading.Thread):
    daemon=True
    def run(self):
        try:
            from picamera2 import Picamera2
            from libcamera import controls, Transform
        except Exception as e:
            STATE.camera_status = f'picamera2 unavailable: {e}'
            STATE.add_error(STATE.camera_status); return
        picam=None
        while RUNNING:
            try:
                if picam is None:
                    picam=Picamera2()
                    transform=Transform(**camera_transform_options(CONFIG.get('camera_rotation_degrees',180)))
                    cfg=picam.create_still_configuration(
                        main={'size': (int(CONFIG['camera_width']), int(CONFIG['camera_height']))},
                        transform=transform
                    )
                    picam.configure(cfg); picam.start(); time.sleep(2)
                    try:
                        if CONFIG.get('camera_autofocus') == 'continuous': picam.set_controls({'AfMode': controls.AfModeEnum.Continuous})
                        else: picam.set_controls({'AfMode': controls.AfModeEnum.Auto})
                    except Exception as e: STATE.add_error(f'Camera AF control warning: {e}')
                    STATE.camera_status='ready'
                    print(f'Camera ready, rotation {int(CONFIG.get("camera_rotation_degrees",180))} degrees', flush=True)
                should=False
                with STATE.lock:
                    should=bool(STATE.ride_id) or bool(CONFIG.get('capture_when_idle'))
                    ride_dir=STATE.ride_dir
                if should and ride_dir:
                    fname=f'photo_{utc_id()}.jpg'; out=ride_dir/'photos'/fname
                    try:
                        # Trigger AF cycle if not continuous; continuous will keep adjusting while bike moves.
                        picam.capture_file(str(out))
                        ts=now_iso(); data={'file': f'photos/{fname}', 'bytes': out.stat().st_size, 'captured_at':ts}
                        with STATE.lock: STATE.last_camera=data; STATE.camera_status='captured'
                        if STATE.db: db_exec('insert into camera values(?,?,?,?,?,?)',(ts,f'photos/{fname}',int(CONFIG['camera_width']),int(CONFIG['camera_height']),'ok',json.dumps(data)))
                    except Exception as e:
                        STATE.camera_status=f'capture error: {e}'; STATE.add_error(STATE.camera_status)
                time.sleep(float(CONFIG['camera_interval_sec']))
            except Exception as e:
                STATE.camera_status=f'camera restart: {e}'; STATE.add_error(STATE.camera_status)
                try:
                    if picam: picam.stop(); picam.close()
                except Exception: pass
                picam=None; time.sleep(5)

def wifi_channel(freq_mhz):
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz <= 2472:
        return (freq_mhz - 2407) // 5
    if 5000 <= freq_mhz <= 5895:
        return (freq_mhz - 5000) // 5
    if 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    return None

def parse_iw_scan(text, iface):
    rows=[]
    current=None
    bssid_re=re.compile(r'^BSS\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})(?:\(|\s|$)', re.I)
    def append_current():
        if current:
            current.setdefault('ssid','(hidden)')
            rows.append(current)
    for raw in text.splitlines():
        line=raw.strip()
        match=bssid_re.match(line)
        if match:
            append_current()
            current={'iface':iface,'bssid':match.group(1).lower()}
        elif line.startswith('BSS '):
            append_current()
            current=None
        elif current and line.startswith('SSID:'):
            current['ssid']=line[5:].strip() or '(hidden)'
        elif current and line.startswith('signal:'):
            try:
                current['signal_dbm']=float(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif current and line.startswith('freq:'):
            try:
                current['freq_mhz']=round(float(line.split()[1]))
                current['channel']=wifi_channel(current['freq_mhz'])
            except (IndexError, ValueError):
                pass
    append_current()
    return rows

def wifi_scan_once():
    out=[]
    try:
        ifaces=[x for x in os.listdir('/sys/class/net') if x.startswith('wl')]
        for iface in ifaces:
            p=subprocess.run(['iw','dev',iface,'scan'], text=True, capture_output=True, timeout=20)
            if p.returncode and not p.stdout:
                raise RuntimeError((p.stderr or f'iw scan exited {p.returncode}').strip())
            out.extend(parse_iw_scan(p.stdout, iface))
    except Exception as e:
        STATE.add_error(f'WiFi scan: {e}')
    deduped={}
    for row in out:
        previous=deduped.get(row['bssid'])
        if previous is None or row.get('signal_dbm',-999) > previous.get('signal_dbm',-999):
            deduped[row['bssid']]=row
    return list(deduped.values())

class WiFiWorker(threading.Thread):
    daemon=True
    def run(self):
        while RUNNING:
            rows=wifi_scan_once(); ts=now_iso()
            rows=sorted(rows, key=lambda r: r.get('signal_dbm') if r.get('signal_dbm') is not None else -999, reverse=True)
            with STATE.lock:
                STATE.last_wifi_count=len(rows)
                STATE.last_wifi_devices=rows[:20]
                STATE.last_wifi_scan_at=ts
            if STATE.db:
                for r in rows:
                    db_exec('insert into wifi values(?,?,?,?,?,?,?,?)',(ts,r.get('iface'),r.get('bssid'),r.get('ssid',''),r.get('signal_dbm'),r.get('freq_mhz'),r.get('channel'),json.dumps(r)))
            time.sleep(float(CONFIG['wifi_interval_sec']))

def parse_btmgmt_devices(text):
    rows=[]
    current=None
    device_re=re.compile(r'hci(?P<index>\d+)\s+dev_found:\s+(?P<address>[0-9A-Fa-f:]{17})\s+type\s+(?P<type>.+?)\s+rssi\s+(?P<rssi>-?\d+)\s+flags\s+(?P<flags>\S+)(?P<tail>.*)$', re.I)
    name_re=re.compile(r'^(?:name|short_name)\s+(.+)$', re.I)
    for raw in text.splitlines():
        line=re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', raw).strip()
        if not line:
            continue
        match=device_re.search(line)
        if match:
            if current:
                rows.append(current)
            current={
                'adapter':f'hci{match.group("index")}',
                'address':match.group('address').upper(),
                'address_type':match.group('type').strip(),
                'rssi_dbm':int(match.group('rssi')),
                'flags':match.group('flags'),
                'raw_lines':[line]
            }
            inline_name=re.search(r'(?:^|\s)(?:name|short_name)\s+(.+)$',match.group('tail'),re.I)
            if inline_name and CONFIG.get('log_bluetooth_names', True):
                current['name']=inline_name.group(1).strip().strip('"')
            continue
        if current:
            current['raw_lines'].append(line)
            match=name_re.match(line)
            if match and CONFIG.get('log_bluetooth_names', True):
                current['name']=match.group(1).strip().strip('"')
    if current:
        rows.append(current)
    best={}
    for row in rows:
        key=(row.get('adapter'),row.get('address'))
        previous=best.get(key)
        if previous is None or row.get('rssi_dbm',-999) > previous.get('rssi_dbm',-999):
            best[key]=row
    return sorted(best.values(), key=lambda r: r.get('rssi_dbm',-999), reverse=True)

def parse_bluetoothctl_devices(scan_text, known_devices_text=''):
    devices={}
    ansi_re=re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
    event_re=re.compile(r'^\[(?:NEW|CHG)\]\s+Device\s+([0-9A-Fa-f:]{17})(?:\s+(.*))?$')
    known_re=re.compile(r'^Device\s+([0-9A-Fa-f:]{17})(?:\s+(.*))?$')

    def device(address):
        address=address.upper()
        return devices.setdefault(address,{
            'adapter':'hci0',
            'address':address,
            'address_type':'BlueZ',
            'rssi_dbm':None,
            'flags':''
        })

    for raw in scan_text.splitlines():
        line=ansi_re.sub('',raw).strip()
        match=event_re.match(line)
        if not match:
            continue
        row=device(match.group(1))
        detail=(match.group(2) or '').strip()
        rssi=re.match(r'RSSI:\s+.*\((-?\d+)\)$',detail)
        if rssi:
            row['rssi_dbm']=int(rssi.group(1))
            continue
        name=re.match(r'(?:Name|Alias):\s+(.+)$',detail)
        if name and CONFIG.get('log_bluetooth_names',True):
            row['name']=name.group(1).strip()
            continue
        if detail and not re.match(r'(?:TxPower|UUIDs|ManufacturerData)\s*:',detail):
            advertised=detail.strip()
            if CONFIG.get('log_bluetooth_names',True) and advertised.replace('-',':').upper() != row['address']:
                row['name']=advertised

    for raw in known_devices_text.splitlines():
        line=ansi_re.sub('',raw).strip()
        match=known_re.match(line)
        if not match:
            continue
        address=match.group(1).upper()
        if address not in devices:
            continue
        advertised=(match.group(2) or '').strip()
        if CONFIG.get('log_bluetooth_names',True) and advertised and advertised.replace('-',':').upper() != address:
            devices[address]['name']=advertised

    return sorted(devices.values(), key=lambda row: row.get('rssi_dbm') if row.get('rssi_dbm') is not None else -999, reverse=True)

def bluetooth_scan_once():
    """Scan through bluetoothd and return nearby devices with RSSI."""
    duration=max(3, int(float(CONFIG.get('bluetooth_scan_duration_sec', 10))))
    index=int(CONFIG.get('bluetooth_adapter_index', 0))
    adapter=f'hci{index}'
    status={'state':'starting','adapter':adapter,'started_at':now_iso(),'duration_sec':duration}
    try:
        if not Path(f'/sys/class/bluetooth/{adapter}').exists():
            status.update({'state':'unavailable','error':f'{adapter} not found','finished_at':now_iso(),'devices':0})
            return [],status
        power=subprocess.run(['bluetoothctl','power','on'], text=True, capture_output=True, timeout=5)
        command=['bluetoothctl','--timeout',str(duration),'scan','on']
        p=subprocess.run(command, text=True, capture_output=True, timeout=duration+5)
        text=(p.stdout or '') + '\n' + (p.stderr or '')
        known=subprocess.run(['bluetoothctl','devices'], text=True, capture_output=True, timeout=5)
        rows=parse_bluetoothctl_devices(text,known.stdout or '')
        for row in rows:
            row['adapter']=adapter
        errors=[]
        if power.returncode != 0:
            errors.append((power.stderr or power.stdout).strip())
        if p.returncode != 0:
            errors.append(f'bluetoothctl scan exited {p.returncode}')
        if known.returncode != 0:
            errors.append(f'bluetoothctl devices exited {known.returncode}')
        status.update({
            'state':'ok' if rows else 'no devices',
            'backend':'bluetoothctl',
            'finished_at':now_iso(),
            'devices':len(rows),
            'return_code':p.returncode,
            'errors':[error for error in errors if error],
            'output_tail':'\n'.join(text.strip().splitlines()[-12:])
        })
        return rows,status
    except FileNotFoundError as e:
        status.update({'state':'error','error':f'tool missing: {e}','finished_at':now_iso(),'devices':0})
    except subprocess.TimeoutExpired as e:
        status.update({'state':'error','error':f'scan timeout: {e}','finished_at':now_iso(),'devices':0})
    except Exception as e:
        status.update({'state':'error','error':str(e),'finished_at':now_iso(),'devices':0})
    return [],status

class BluetoothWorker(threading.Thread):
    daemon=True
    def run(self):
        while RUNNING:
            rows,status=bluetooth_scan_once(); ts=now_iso()
            with STATE.lock:
                STATE.last_bluetooth_count=len(rows)
                STATE.last_bluetooth_devices=rows[:50]
                STATE.bluetooth_status=status
            if status.get('state') in ('error','unavailable'):
                STATE.add_error(f'Bluetooth scan: {status.get("error",status.get("state"))}')
            if STATE.db:
                for r in rows:
                    db_exec('insert into bluetooth values(?,?,?,?,?,?,?,?)', (ts, r.get('adapter'), r.get('address'), r.get('address_type'), r.get('name',''), r.get('rssi_dbm'), r.get('flags',''), json.dumps(r)))
            time.sleep(float(CONFIG.get('bluetooth_interval_sec', 60.0)))

def health_once():
    data={}
    try:
        p=subprocess.run(['vcgencmd','measure_temp'], text=True, capture_output=True, timeout=3)
        m=re.search(r"temp=([0-9.]+)", p.stdout); data['cpu_temp_c']=float(m.group(1)) if m else None
    except Exception: data['cpu_temp_c']=None
    try:
        p=subprocess.run(['vcgencmd','get_throttled'], text=True, capture_output=True, timeout=3)
        data['throttled']=p.stdout.strip()
    except Exception: data['throttled']=''
    try: data['load1']=os.getloadavg()[0]
    except Exception: data['load1']=None
    try:
        mem={}
        for line in Path('/proc/meminfo').read_text().splitlines():
            k,v=line.split(':',1); mem[k]=int(v.strip().split()[0])
        data['mem_available_kb']=mem.get('MemAvailable')
    except Exception: data['mem_available_kb']=None
    try: data['disk_free_mb']=shutil.disk_usage(str(DATA_DIR)).free//(1024*1024)
    except Exception: data['disk_free_mb']=None
    return data

class HealthWorker(threading.Thread):
    daemon=True
    def run(self):
        while RUNNING:
            data=health_once(); ts=now_iso()
            with STATE.lock: STATE.last_health=data
            if STATE.db: db_exec('insert into health values(?,?,?,?,?,?,?)',(ts,data.get('cpu_temp_c'),data.get('throttled'),data.get('load1'),data.get('mem_available_kb'),data.get('disk_free_mb'),json.dumps(data)))
            time.sleep(float(CONFIG['health_interval_sec']))

class ButtonWorker(threading.Thread):
    daemon=True
    def run(self):
        try:
            from gpiozero import Button
        except Exception as e:
            STATE.add_error(f'GPIO unavailable: {e}'); return
        try:
            start=Button(int(CONFIG['start_button_gpio']), pull_up=True, bounce_time=float(CONFIG['button_bounce_sec']))
            stop=Button(int(CONFIG['stop_button_gpio']), pull_up=True, bounce_time=float(CONFIG['button_bounce_sec']))
            start.when_pressed=lambda: start_ride('GPIO start button')
            stop.when_pressed=lambda: stop_ride('GPIO stop button')
            print(f'Buttons ready: start GPIO{CONFIG["start_button_gpio"]}, stop GPIO{CONFIG["stop_button_gpio"]}', flush=True)
            while RUNNING: time.sleep(1)
        except Exception as e:
            STATE.add_error(f'GPIO buttons: {e}')

def list_rides():
    rides=[]
    for d in sorted(RIDES_DIR.glob('*'), reverse=True):
        if not d.is_dir(): continue
        dbp=d/'ride.sqlite'; photos=list((d/'photos').glob('*.jpg')) if (d/'photos').exists() else []
        info={'id':d.name,'photos':len(photos),'path':str(d)}
        if dbp.exists():
            try:
                db=sqlite3.connect(str(dbp));
                meta=dict(db.execute('select key,value from meta').fetchall())
                info.update({k:meta.get(k,'') for k in ('started_at','stopped_at')})
                for table in ('gps','environment','imu','ups','wifi','bluetooth','camera','health','events'):
                    try: info[table]=db.execute(f'select count(*) from {table}').fetchone()[0]
                    except sqlite3.OperationalError: info[table]=0
                db.close()
            except Exception as e: info['error']=str(e)
        rides.append(info)
    return rides

def display_value(value, digits=2, suffix=''):
    if value is None or value == '':
        return '—'
    if isinstance(value,float):
        return f'{value:.{digits}f}{suffix}'
    return f'{value}{suffix}'

def metric(label, value, tone=''):
    return f'<div class="metric {tone}"><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>'

def data_table(rows, columns, empty='No data yet.'):
    if not rows:
        return f'<p class="muted">{html.escape(empty)}</p>'
    head=''.join(f'<th>{html.escape(label)}</th>' for _,label in columns)
    body=[]
    for row in rows:
        cells=''.join(f'<td>{html.escape(display_value(row.get(key)))}</td>' for key,_ in columns)
        body.append(f'<tr>{cells}</tr>')
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'

def latest_photos(limit=8, ride_id=None):
    candidates=[]
    if ride_id:
        candidates=[RIDES_DIR/ride_id]
    else:
        with STATE.lock:
            active=STATE.ride_id
        if active:
            candidates.append(RIDES_DIR/active)
        candidates.extend(d for d in sorted(RIDES_DIR.glob('*'),reverse=True) if d not in candidates)
    for ride_dir in candidates:
        photo_dir=ride_dir/'photos'
        if not photo_dir.exists():
            continue
        photos=sorted(photo_dir.glob('*.jpg'),key=lambda p:p.stat().st_mtime,reverse=True)[:limit]
        if photos:
            return [{'ride_id':ride_dir.name,'file':photo.name,'mtime':datetime.fromtimestamp(photo.stat().st_mtime).astimezone().isoformat(timespec='seconds')} for photo in photos]
    return []

def photo_gallery(photos):
    if not photos:
        return '<p class="muted">No photos captured yet.</p>'
    items=[]
    for photo in photos:
        rid=quote(photo['ride_id'],safe='')
        name=quote(photo['file'],safe='')
        url=f'/ride/{rid}/photos/{name}'
        items.append(f'<a class="photo" href="{url}"><img loading="lazy" src="{url}" alt="Ride photo"><span>{html.escape(photo["mtime"])}</span></a>')
    return f'<div class="gallery">{"".join(items)}</div>'

def ride_history_table(rides):
    if not rides:
        return '<p class="muted">No rides yet.</p>'
    rows=[]
    for ride in rides:
        rid=html.escape(str(ride.get('id','')))
        url=quote(str(ride.get('id','')),safe='')
        rows.append(f'''<tr><td><a href="/ride/{url}">{rid}</a></td><td>{html.escape(display_value(ride.get("started_at")))}</td><td>{html.escape(display_value(ride.get("stopped_at")))}</td><td>{ride.get("gps",0)}</td><td>{ride.get("photos",0)}</td><td>{ride.get("bluetooth",0)}</td></tr>''')
    return f'<div class="table-wrap"><table><thead><tr><th>Ride</th><th>Started</th><th>Stopped</th><th>GPS rows</th><th>Photos</th><th>Bluetooth rows</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'

def page(title, body, refresh=None):
    refresh_tag=f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ''
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh_tag}<title>{html.escape(title)}</title>
<style>
:root{{--bg:#09111f;--panel:#111c2d;--panel2:#17243a;--text:#edf4ff;--muted:#94a6be;--line:#2a3b54;--blue:#63b3ff;--green:#55d98b;--amber:#ffc857;--red:#ff6b6b}}
*{{box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:var(--bg);color:var(--text);line-height:1.4}}
main{{max-width:1400px;margin:auto;padding:1rem}}header{{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}}
h1,h2,h3{{margin:.15rem 0}}h1{{font-size:clamp(1.6rem,4vw,2.5rem)}}h2{{font-size:1.05rem;color:#d9e8fa}}a{{color:var(--blue)}}.muted{{color:var(--muted)}}
.badge{{display:inline-flex;align-items:center;gap:.4rem;padding:.35rem .7rem;border-radius:999px;background:#21324b;color:var(--muted)}}.badge.live{{background:#123c2b;color:var(--green)}}.dot{{width:.55rem;height:.55rem;border-radius:50%;background:currentColor}}
.actions{{display:flex;gap:.5rem;flex-wrap:wrap}}form{{margin:0}}button,.button{{border:0;border-radius:.55rem;padding:.7rem 1rem;background:var(--blue);color:#05101d;font-weight:700;text-decoration:none;cursor:pointer}}button.stop{{background:var(--red)}}.button.secondary{{background:#263954;color:var(--text)}}
.grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:.8rem}}.card{{grid-column:span 4;background:linear-gradient(145deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:.8rem;padding:1rem;min-width:0}}.wide{{grid-column:span 8}}.full{{grid-column:1/-1}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:.55rem;margin-top:.7rem}}.metric{{background:#0b1627;border:1px solid #22334d;border-radius:.6rem;padding:.65rem}}.metric span{{display:block;color:var(--muted);font-size:.78rem}}.metric strong{{display:block;font-size:1.15rem;margin-top:.15rem;overflow-wrap:anywhere}}.metric.good strong{{color:var(--green)}}.metric.warn strong{{color:var(--amber)}}.metric.bad strong{{color:var(--red)}}
.table-wrap{{overflow:auto;margin-top:.6rem}}table{{border-collapse:collapse;width:100%;font-size:.88rem}}th,td{{text-align:left;border-bottom:1px solid var(--line);padding:.5rem;white-space:nowrap}}th{{color:var(--muted);font-weight:600}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.65rem;margin-top:.7rem}}.photo{{position:relative;display:block;min-height:130px;border-radius:.6rem;overflow:hidden;background:#050a12}}.photo img{{width:100%;height:180px;object-fit:cover;display:block}}.photo span{{position:absolute;bottom:0;left:0;right:0;padding:.35rem .5rem;background:#000a;color:#fff;font-size:.72rem}}
.errors{{margin:.6rem 0 0;padding-left:1.2rem;color:#ffb6b6}}pre{{background:#07101d;border:1px solid var(--line);padding:.7rem;border-radius:.5rem;white-space:pre-wrap;overflow-wrap:anywhere;color:#bdd0e8}}
@media(max-width:900px){{.card,.wide{{grid-column:1/-1}}}}@media(max-width:520px){{main{{padding:.7rem}}.photo img{{height:145px}}}}
</style></head><body><main>{body}</main></body></html>'''

def latest_row(db, table):
    try:
        row=db.execute(f'select * from {table} order by ts desc limit 1').fetchone()
        return dict(row) if row else {}
    except sqlite3.OperationalError:
        return {}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): print('WEB', fmt%args, flush=True)
    def send(self, content, ctype='text/html', code=200):
        if isinstance(content,str): content=content.encode()
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(content))); self.end_headers(); self.wfile.write(content)
    def redirect(self, location='/'):
        self.send_response(303)
        self.send_header('Location',location)
        self.send_header('Content-Length','0')
        self.send_header('Cache-Control','no-store')
        self.end_headers()
    def do_POST(self):
        path=urlparse(self.path).path
        if path=='/api/start':
            start_ride('web')
            self.redirect('/')
            return
        if path=='/api/stop':
            stop_ride('web')
            self.redirect('/')
            return
        self.send('not found','text/plain',404)
    def do_GET(self):
        u=urlparse(self.path); path=u.path
        if path.startswith('/ride/') and '/photos/' in path:
            parts=path.split('/')
            rid=parts[2]; fname=parts[-1]
            if not re.fullmatch(r'[A-Za-z0-9_.-]+',rid) or Path(fname).name != fname:
                self.send('not found','text/plain',404); return
            fp=RIDES_DIR/rid/'photos'/fname
            if fp.is_file(): self.send(fp.read_bytes(),'image/jpeg'); return
            self.send('not found','text/plain',404); return
        if path.startswith('/ride/'):
            rid=path.split('/')[2]
            if not re.fullmatch(r'[A-Za-z0-9_.-]+',rid):
                self.send('ride not found','text/plain',404); return
            d=RIDES_DIR/rid; dbp=d/'ride.sqlite'
            if not dbp.exists(): self.send('ride not found','text/plain',404); return
            db=sqlite3.connect(str(dbp)); db.row_factory=sqlite3.Row
            counts={}
            for t in ('gps','environment','imu','ups','wifi','bluetooth','camera','health','events'):
                try: counts[t]=db.execute(f'select count(*) from {t}').fetchone()[0]
                except Exception: counts[t]=0
            latest={table:latest_row(db,table) for table in ('gps','environment','imu','ups','health')}
            bluetooth=[dict(row) for row in db.execute('select * from bluetooth order by ts desc limit 20').fetchall()]
            events=[dict(row) for row in db.execute('select * from events order by ts desc limit 20').fetchall()]
            db.close()
            gps=latest['gps']; env=latest['environment']; ups=latest['ups']; health=latest['health']
            body=f'''<header><div><a href="/">← Dashboard</a><h1>Ride {html.escape(rid)}</h1></div></header>
<div class="grid">
<section class="card">{metric("GPS",f'{display_value(gps.get("lat"),6)}, {display_value(gps.get("lon"),6)}')}{metric("Speed",display_value(gps.get("speed_knots"),1," kn"))}{metric("Satellites",display_value(gps.get("sats")))}</section>
<section class="card">{metric("Temperature",display_value(env.get("temperature_c"),1," °C"))}{metric("Humidity",display_value(env.get("humidity_pct"),1," %"))}{metric("Pressure",display_value(env.get("pressure_hpa"),1," hPa"))}</section>
<section class="card">{metric("Battery",display_value(ups.get("battery_percent"),1," %"))}{metric("Voltage",display_value(ups.get("bus_voltage_v"),2," V"))}{metric("Current",display_value(ups.get("current_ma"),0," mA"))}</section>
<section class="card full"><h2>Latest photos</h2>{photo_gallery(latest_photos(12,rid))}</section>
<section class="card wide"><h2>Bluetooth observations</h2>{data_table(bluetooth,[("ts","Time"),("name","Name"),("address","Address"),("address_type","Type"),("rssi_dbm","RSSI dBm")])}</section>
<section class="card"><h2>Ride totals</h2><pre>{html.escape(json.dumps(counts,indent=2))}</pre></section>
<section class="card full"><h2>Recent events</h2>{data_table(events,[("ts","Time"),("type","Type"),("message","Message")])}</section>
</div>'''
            self.send(page('Ride '+rid,body)); return
        if path=='/status.json':
            with STATE.lock:
                obj={'version':APP_VERSION,'ride_id':STATE.ride_id,'started_at':STATE.started_at,'last_gps':STATE.last_gps,'last_env':STATE.last_env,'last_imu':STATE.last_imu,'last_ups':STATE.last_ups,'last_health':STATE.last_health,'last_camera':STATE.last_camera,'camera_status':STATE.camera_status,'last_wifi_count':STATE.last_wifi_count,'last_wifi_scan_at':STATE.last_wifi_scan_at,'last_wifi_devices':STATE.last_wifi_devices,'last_bluetooth_count':STATE.last_bluetooth_count,'last_bluetooth_devices':STATE.last_bluetooth_devices,'bluetooth_status':STATE.bluetooth_status,'errors':STATE.errors[-10:], 'rides_dir':str(RIDES_DIR)}
            self.send(json.dumps(obj,indent=2,default=str),'application/json'); return
        rides=list_rides()
        with STATE.lock:
            status={'ride_id':STATE.ride_id,'started_at':STATE.started_at,'gps':STATE.last_gps.copy(),'env':STATE.last_env.copy(),'imu':STATE.last_imu.copy(),'ups':STATE.last_ups.copy(),'health':STATE.last_health.copy(),'camera':STATE.camera_status,'camera_data':STATE.last_camera.copy(),'wifi_count':STATE.last_wifi_count,'wifi_scan_at':STATE.last_wifi_scan_at,'wifi_devices':list(STATE.last_wifi_devices),'bluetooth_count':STATE.last_bluetooth_count,'bluetooth_devices':list(STATE.last_bluetooth_devices),'bluetooth_status':STATE.bluetooth_status.copy(),'errors':STATE.errors[-8:]}
        env=next(iter(status['env'].values()),{}) if status['env'] else {}
        gps=status['gps']; imu=status['imu']; ups=status['ups']; health=status['health']; bt=status['bluetooth_status']
        ride_live=bool(status['ride_id'])
        controls='<div class="actions"><form method="post" action="/api/start"><button>Start ride</button></form><form method="post" action="/api/stop"><button class="stop">Stop ride</button></form><a class="button secondary" href="/status.json">Raw status</a></div>'
        ride_rows=[{'id':r.get('id'),'started_at':r.get('started_at'),'stopped_at':r.get('stopped_at'),'gps':r.get('gps',0),'photos':r.get('photos',0),'bluetooth':r.get('bluetooth',0)} for r in rides[:12]]
        camera=status['camera_data']
        bt_detail=bt.get('error') or '; '.join(bt.get('errors',[]))
        errors=''.join(f'<li>{html.escape(error)}</li>' for error in status['errors'])
        version_text=f'Version {APP_VERSION["commit"]}'
        if APP_VERSION.get('installed_at'):
            version_text+=f' · installed {APP_VERSION["installed_at"]}'
        body=f'''<header><div><h1>BikeLogger RideHub</h1><div class="badge {"live" if ride_live else ""}"><span class="dot"></span>{"Recording "+html.escape(status["ride_id"]) if ride_live else "Ready, not recording"}</div><div class="muted">{html.escape(version_text)}</div></div>{controls}</header>
<div class="grid">
<section class="card"><h2>GPS</h2><div class="metrics">{metric("Latitude",display_value(gps.get("lat"),6))}{metric("Longitude",display_value(gps.get("lon"),6))}{metric("Speed",display_value(gps.get("speed_knots"),1," kn"))}{metric("Course",display_value(gps.get("course_deg"),1," °"))}{metric("Altitude",display_value(gps.get("alt_m"),1," m"))}{metric("Fix quality",display_value(gps.get("fix_quality")))}{metric("Satellites",display_value(gps.get("sats")))}{metric("HDOP",display_value(gps.get("hdop"),2))}</div></section>
<section class="card"><h2>Environment</h2><div class="metrics">{metric("Temperature",display_value(env.get("temperature_c"),1," °C"))}{metric("Humidity",display_value(env.get("humidity_pct"),1," %"))}{metric("Pressure",display_value(env.get("pressure_hpa"),1," hPa"))}</div></section>
<section class="card"><h2>UPS battery</h2><div class="metrics">{metric("Charge",display_value(ups.get("battery_percent"),1," %"),"good" if ups.get("battery_percent",0)>40 else "warn")}{metric("Bus voltage",display_value(ups.get("bus_voltage_v"),2," V"))}{metric("Supply voltage",display_value(ups.get("supply_voltage_v"),2," V"))}{metric("Shunt",display_value(ups.get("shunt_voltage_mv"),2," mV"))}{metric("Current",display_value(ups.get("current_ma"),0," mA"))}{metric("Power",display_value(ups.get("power_w"),2," W"))}{metric("State",display_value(ups.get("state")))}</div></section>
<section class="card wide"><h2>Motion / IMU</h2><div class="metrics">{metric("Accel X",display_value(imu.get("accel_x_g"),3," g"))}{metric("Accel Y",display_value(imu.get("accel_y_g"),3," g"))}{metric("Accel Z",display_value(imu.get("accel_z_g"),3," g"))}{metric("Gyro X",display_value(imu.get("gyro_x_dps"),1," °/s"))}{metric("Gyro Y",display_value(imu.get("gyro_y_dps"),1," °/s"))}{metric("Gyro Z",display_value(imu.get("gyro_z_dps"),1," °/s"))}{metric("Mag X",display_value(imu.get("mag_x_gauss"),3," G"))}{metric("Mag Y",display_value(imu.get("mag_y_gauss"),3," G"))}{metric("Mag Z",display_value(imu.get("mag_z_gauss"),3," G"))}</div></section>
<section class="card"><h2>System / camera</h2><div class="metrics">{metric("CPU",display_value(health.get("cpu_temp_c"),1," °C"))}{metric("Load",display_value(health.get("load1"),2))}{metric("Memory free",display_value(health.get("mem_available_kb"),0," kB"))}{metric("Disk free",display_value(health.get("disk_free_mb"),0," MB"))}{metric("Throttle",display_value(health.get("throttled")))}{metric("Camera",status["camera"])}{metric("Last capture",display_value(camera.get("captured_at")))}</div></section>
<section class="card full"><h2>Latest photos</h2>{photo_gallery(latest_photos(8))}</section>
<section class="card wide"><h2>Bluetooth devices</h2><p class="muted">Scan: {html.escape(display_value(bt.get("state")))} · {html.escape(display_value(bt.get("finished_at")))} · {status["bluetooth_count"]} devices{(" · "+html.escape(bt_detail)) if bt_detail else ""}</p>{data_table(status["bluetooth_devices"],[("name","Name"),("address","Address"),("address_type","Type"),("rssi_dbm","RSSI dBm")],"No Bluetooth devices reported by the last scan.")}</section>
<section class="card full"><h2>Most Recent WiFi Hotspots</h2><p class="muted">{status["wifi_count"]} networks · last scan {html.escape(display_value(status["wifi_scan_at"]))} · strongest signal first</p>{data_table(status["wifi_devices"],[("ssid","SSID"),("bssid","BSSID"),("signal_dbm","Signal dBm"),("channel","Channel"),("freq_mhz","Frequency MHz"),("iface","Interface")],"No WiFi hotspots reported by the last scan.")}</section>
<section class="card full"><h2>Ride history</h2>{ride_history_table(ride_rows)}</section>
<section class="card full"><h2>Recent errors</h2>{"<ul class=errors>"+errors+"</ul>" if errors else '<p class="muted">No recent errors.</p>'}</section>
</div><p class="muted">BikeLogger {html.escape(APP_VERSION["commit"])} · Dashboard refreshes every 5 seconds.</p>'''
        self.send(page('BikeLogger RideHub',body,refresh=5))

def main():
    def handle(sig, frame):
        global RUNNING
        RUNNING=False; stop_ride(f'signal {sig}'); sys.exit(0)
    signal.signal(signal.SIGTERM, handle); signal.signal(signal.SIGINT, handle)
    print('BikeLogger starting', now_iso(), flush=True)
    threads=[GPSWorker(), EnvWorker(), IMUWorker(), UPSWorker(), CameraWorker(), WiFiWorker(), BluetoothWorker(), HealthWorker(), ButtonWorker()]
    for t in threads: t.start()
    host=CONFIG['web_host']; port=int(CONFIG['web_port'])
    print(f'RideHub web: http://{host}:{port}', flush=True)
    ThreadingHTTPServer((host,port), Handler).serve_forever()

if __name__=='__main__': main()
