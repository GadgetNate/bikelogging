#!/usr/bin/env python3
"""BikeLogger Pi 4: lightweight ride logger + RideHub web UI.
Features: GPIO start/stop, serial GPS NMEA at 38400, BME280, Pololu MiniIMU-9 v2,
Waveshare UPS HAT power monitoring, persistent Picamera2 autofocus capture,
plain-text WiFi scans, built-in Bluetooth device/RSSI scans, health logging, per-ride SQLite, local web history browser.
"""
import os, sys, time, json, math, glob, sqlite3, threading, subprocess, shutil, signal, html, re
from datetime import datetime, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

APP_DIR = Path('/opt/bikelogger')
DATA_DIR = Path(os.environ.get('BIKELOGGER_DATA', '/var/lib/bikelogger'))
RIDES_DIR = DATA_DIR / 'rides'
LOG_DIR = Path(os.environ.get('BIKELOGGER_LOG', '/var/log/bikelogger'))
CONFIG_PATH = APP_DIR / 'config.json'
RUNNING = True

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')

def utc_id():
    return datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')

def load_config():
    default = {
        'web_host': '0.0.0.0', 'web_port': 8080,
        'gps_port': '/dev/serial0', 'gps_baud': 38400,
        'camera_interval_sec': 5.0, 'camera_width': 2304, 'camera_height': 1296,
        'camera_quality': 92, 'camera_autofocus': 'continuous',
        'env_interval_sec': 2.0, 'gps_interval_note': 'GPS logs as NMEA sentences arrive',
        'wifi_interval_sec': 30.0, 'bluetooth_interval_sec': 60.0, 'bluetooth_scan_duration_sec': 10.0, 'health_interval_sec': 10.0,
        'start_button_gpio': 27, 'stop_button_gpio': 17,
        'button_bounce_sec': 0.15,
        'i2c_bus': 1, 'bme280_addresses': [0x76, 0x77],
        'imu_interval_sec': 0.1,
        'imu_accel_address': 0x19, 'imu_mag_address': 0x1E,
        'imu_gyro_addresses': [0x6B, 0x6A],
        'ups_interval_sec': 2.0, 'ups_address': 0x42,
        'ups_empty_voltage': 6.0, 'ups_full_voltage': 8.4,
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
        self.last_bluetooth_count = 0
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
        if self.gyro_addr is None:
            raise RuntimeError('L3GD20 not found at configured addresses')

        bus.write_byte_data(self.accel_addr, 0x20, 0x57)
        bus.write_byte_data(self.accel_addr, 0x23, 0x88)
        bus.write_byte_data(self.gyro_addr, 0x20, 0x0F)
        bus.write_byte_data(self.gyro_addr, 0x23, 0x80)
        bus.write_byte_data(self.mag_addr, 0x00, 0x14)
        bus.write_byte_data(self.mag_addr, 0x01, 0x20)
        bus.write_byte_data(self.mag_addr, 0x02, 0x00)

    def read(self):
        accel=self.bus.read_i2c_block_data(self.accel_addr, 0x28 | 0x80, 6)
        gyro=self.bus.read_i2c_block_data(self.gyro_addr, 0x28 | 0x80, 6)
        mag=self.bus.read_i2c_block_data(self.mag_addr, 0x03, 6)

        ax=signed_16(accel[0], accel[1]) >> 4
        ay=signed_16(accel[2], accel[3]) >> 4
        az=signed_16(accel[4], accel[5]) >> 4
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
            'gyro_x_dps':gx*self.GYRO_SCALE_DPS, 'gyro_y_dps':gy*self.GYRO_SCALE_DPS, 'gyro_z_dps':gz*self.GYRO_SCALE_DPS,
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
            print(f'MiniIMU-9 v2 ready: accel 0x{sensor.accel_addr:02x}, mag 0x{sensor.mag_addr:02x}, gyro 0x{sensor.gyro_addr:02x}', flush=True)
        except Exception as e:
            STATE.add_error(f'MiniIMU-9 v2 init: {e}'); return
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
                STATE.add_error(f'MiniIMU-9 v2 read: {e}')
            time.sleep(max(0.02, float(CONFIG['imu_interval_sec'])))

class WaveshareUPSHat:
    CALIBRATION = 4096
    CONFIG_32V_2A = 0x399F
    CURRENT_LSB_MA = 0.1
    POWER_LSB_W = 0.002

    def __init__(self, bus, addr, empty_voltage, full_voltage):
        self.bus = bus
        self.addr = addr
        self.empty_voltage = empty_voltage
        self.full_voltage = full_voltage
        if self.full_voltage <= self.empty_voltage:
            raise ValueError('UPS full voltage must be greater than empty voltage')
        self.write_register(0x05, self.CALIBRATION)
        self.write_register(0x00, self.CONFIG_32V_2A)
        self.read_register(0x02)

    def read_register(self, register):
        data=self.bus.read_i2c_block_data(self.addr, register, 2)
        return (data[0] << 8) | data[1]

    def write_register(self, register, value):
        self.bus.write_i2c_block_data(self.addr, register, [(value >> 8) & 0xFF, value & 0xFF])

    def read(self):
        self.write_register(0x05, self.CALIBRATION)
        shunt_raw=self.read_register(0x01)
        bus_raw=self.read_register(0x02)
        current_raw=self.read_register(0x04)
        power_raw=self.read_register(0x03)

        shunt_mv=(shunt_raw-65536 if shunt_raw > 32767 else shunt_raw)*0.01
        bus_voltage=(bus_raw >> 3)*0.004
        supply_voltage=bus_voltage+(shunt_mv/1000.0)
        current_ma=(current_raw-65536 if current_raw > 32767 else current_raw)*self.CURRENT_LSB_MA
        power_w=power_raw*self.POWER_LSB_W
        battery_percent=(bus_voltage-self.empty_voltage)/(self.full_voltage-self.empty_voltage)*100.0
        battery_percent=max(0.0,min(100.0,battery_percent))
        state='charging' if current_ma > 10.0 else 'discharging' if current_ma < -10.0 else 'idle'
        return {
            'model':'Waveshare UPS HAT (B)', 'address':f'0x{self.addr:02x}',
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
                float(CONFIG['ups_full_voltage'])
            )
            print(f'Waveshare UPS HAT ready at 0x{sensor.addr:02x}', flush=True)
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

class CameraWorker(threading.Thread):
    daemon=True
    def run(self):
        try:
            from picamera2 import Picamera2
            from libcamera import controls
        except Exception as e:
            STATE.camera_status = f'picamera2 unavailable: {e}'
            STATE.add_error(STATE.camera_status); return
        picam=None
        while RUNNING:
            try:
                if picam is None:
                    picam=Picamera2()
                    cfg=picam.create_still_configuration(main={'size': (int(CONFIG['camera_width']), int(CONFIG['camera_height']))})
                    picam.configure(cfg); picam.start(); time.sleep(2)
                    try:
                        if CONFIG.get('camera_autofocus') == 'continuous': picam.set_controls({'AfMode': controls.AfModeEnum.Continuous})
                        else: picam.set_controls({'AfMode': controls.AfModeEnum.Auto})
                    except Exception as e: STATE.add_error(f'Camera AF control warning: {e}')
                    STATE.camera_status='ready'
                    print('Camera ready', flush=True)
                should=False
                with STATE.lock:
                    should=bool(STATE.ride_id) or bool(CONFIG.get('capture_when_idle'))
                    ride_dir=STATE.ride_dir
                if should and ride_dir:
                    fname=f'photo_{utc_id()}.jpg'; out=ride_dir/'photos'/fname
                    try:
                        # Trigger AF cycle if not continuous; continuous will keep adjusting while bike moves.
                        picam.capture_file(str(out))
                        ts=now_iso(); data={'file': f'photos/{fname}', 'bytes': out.stat().st_size}
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

def wifi_scan_once():
    out=[]
    try:
        ifaces=[x for x in os.listdir('/sys/class/net') if x.startswith('wl')]
        for iface in ifaces:
            p=subprocess.run(['iw','dev',iface,'scan'], text=True, capture_output=True, timeout=20)
            text=p.stdout or p.stderr
            current=None
            for line in text.splitlines():
                line=line.strip()
                if line.startswith('BSS '):
                    if current: out.append(current)
                    current={'iface':iface,'bssid':line.split()[1].split('(')[0]}
                elif current and line.startswith('SSID:'):
                    current['ssid']=line[5:].strip()
                elif current and line.startswith('signal:'):
                    try: current['signal_dbm']=float(line.split()[1])
                    except Exception: pass
                elif current and line.startswith('freq:'):
                    try: current['freq_mhz']=int(line.split()[1])
                    except Exception: pass
            if current: out.append(current)
    except Exception as e:
        STATE.add_error(f'WiFi scan: {e}')
    return out

class WiFiWorker(threading.Thread):
    daemon=True
    def run(self):
        while RUNNING:
            rows=wifi_scan_once(); ts=now_iso()
            with STATE.lock: STATE.last_wifi_count=len(rows)
            if STATE.db:
                for r in rows:
                    db_exec('insert into wifi values(?,?,?,?,?,?,?,?)',(ts,r.get('iface'),r.get('bssid'),r.get('ssid',''),r.get('signal_dbm'),r.get('freq_mhz'),None,json.dumps(r)))
            time.sleep(float(CONFIG['wifi_interval_sec']))

def bluetooth_scan_once():
    """Scan with the Pi 4 built-in Bluetooth adapter and return nearby devices with RSSI when BlueZ reports it.
    Uses btmgmt because bluetoothctl commonly omits RSSI in scripted scans. Service runs as root.
    """
    rows=[]
    duration=max(3, int(float(CONFIG.get('bluetooth_scan_duration_sec', 10))))
    try:
        if not Path('/sys/class/bluetooth/hci0').exists():
            return rows
        subprocess.run(['rfkill','unblock','bluetooth'], text=True, capture_output=True, timeout=5)
        subprocess.run(['btmgmt','--index','0','power','on'], text=True, capture_output=True, timeout=8)
        # btmgmt find normally runs until interrupted; timeout is expected and useful here.
        p=subprocess.run(['timeout', str(duration), 'btmgmt', '--index', '0', 'find'], text=True, capture_output=True, timeout=duration+5)
        text=(p.stdout or '') + '\n' + (p.stderr or '')
        current=None
        for raw in text.splitlines():
            line=raw.strip()
            if not line:
                continue
            m=re.search(r'(hci\d+) dev_found: ([0-9A-Fa-f:]{17}) type ([^ ]+(?: [^ ]+)?) rssi (-?\d+)(?: flags (\S+))?', line)
            if m:
                if current:
                    rows.append(current)
                current={'adapter':m.group(1), 'address':m.group(2).upper(), 'address_type':m.group(3), 'rssi_dbm':int(m.group(4)), 'flags':m.group(5) or '', 'raw_lines':[line]}
                continue
            if current:
                current.setdefault('raw_lines',[]).append(line)
                # btmgmt may print name, short_name, or UUID/manufacturer data lines.
                nm=re.match(r'(?:name|short_name)\s+(.+)$', line, re.I)
                if nm and CONFIG.get('log_bluetooth_names', True):
                    current['name']=nm.group(1).strip()
        if current:
            rows.append(current)
        # De-duplicate within one scan, keeping strongest/latest report per address.
        best={}
        for r in rows:
            key=(r.get('adapter'), r.get('address'))
            if key not in best or (r.get('rssi_dbm') is not None and (best[key].get('rssi_dbm') is None or r.get('rssi_dbm') > best[key].get('rssi_dbm'))):
                best[key]=r
        return list(best.values())
    except FileNotFoundError as e:
        STATE.add_error(f'Bluetooth tool missing: {e}')
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        STATE.add_error(f'Bluetooth scan: {e}')
    return rows

class BluetoothWorker(threading.Thread):
    daemon=True
    def run(self):
        while RUNNING:
            rows=bluetooth_scan_once(); ts=now_iso()
            with STATE.lock: STATE.last_bluetooth_count=len(rows)
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

def page(title, body):
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;margin:1rem;line-height:1.35}} a.button,button{{display:inline-block;padding:.6rem .9rem;margin:.2rem;border:1px solid #555;border-radius:.4rem;background:#eee;color:#111;text-decoration:none}} .card{{border:1px solid #ddd;border-radius:.5rem;padding:.8rem;margin:.7rem 0}} table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.35rem;text-align:left}} img{{max-width:220px;max-height:160px;margin:.25rem}} code,pre{{background:#f5f5f5;padding:.2rem;white-space:pre-wrap}}</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): print('WEB', fmt%args, flush=True)
    def send(self, content, ctype='text/html', code=200):
        if isinstance(content,str): content=content.encode()
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(content))); self.end_headers(); self.wfile.write(content)
    def do_POST(self):
        path=urlparse(self.path).path
        if path=='/api/start': rid=start_ride('web'); self.send(json.dumps({'ride_id':rid}), 'application/json'); return
        if path=='/api/stop': rid=stop_ride('web'); self.send(json.dumps({'stopped':rid}), 'application/json'); return
        self.send('not found','text/plain',404)
    def do_GET(self):
        u=urlparse(self.path); path=u.path
        if path.startswith('/ride/') and '/photos/' in path:
            parts=path.split('/')
            rid=parts[2]; fname='/'.join(parts[4:])
            fp=RIDES_DIR/rid/'photos'/fname
            if fp.exists(): self.send(fp.read_bytes(),'image/jpeg'); return
            self.send('not found','text/plain',404); return
        if path.startswith('/ride/'):
            rid=path.split('/')[2]; d=RIDES_DIR/rid; dbp=d/'ride.sqlite'
            if not dbp.exists(): self.send('ride not found','text/plain',404); return
            db=sqlite3.connect(str(dbp)); db.row_factory=sqlite3.Row
            counts={}
            for t in ('gps','environment','imu','ups','wifi','bluetooth','camera','health','events'):
                try: counts[t]=db.execute(f'select count(*) from {t}').fetchone()[0]
                except Exception: counts[t]=0
            latest_gps=db.execute('select * from gps where lat is not null and lon is not null order by ts desc limit 1').fetchone()
            cams=db.execute('select * from camera order by ts desc limit 30').fetchall(); events=db.execute('select * from events order by ts desc limit 30').fetchall(); db.close()
            imgs=''.join(f'<a href="/ride/{rid}/photos/{html.escape(Path(r["file"]).name)}"><img src="/ride/{rid}/photos/{html.escape(Path(r["file"]).name)}"></a>' for r in cams if r['file'])
            gpshtml=f'<p>Latest GPS: {latest_gps["lat"]}, {latest_gps["lon"]}</p>' if latest_gps else '<p>No GPS fix yet.</p>'
            body=f'<p><a href="/">Home</a></p><div class="card"><h2>{html.escape(rid)}</h2>{gpshtml}<pre>{html.escape(json.dumps(counts,indent=2))}</pre></div><h2>Photos</h2>{imgs or "No photos."}<h2>Recent events</h2><pre>{html.escape(json.dumps([dict(e) for e in events], indent=2))}</pre>'
            self.send(page('Ride '+rid, body)); return
        if path=='/status.json':
            with STATE.lock:
                obj={'ride_id':STATE.ride_id,'started_at':STATE.started_at,'last_gps':STATE.last_gps,'last_env':STATE.last_env,'last_imu':STATE.last_imu,'last_ups':STATE.last_ups,'last_health':STATE.last_health,'last_camera':STATE.last_camera,'camera_status':STATE.camera_status,'last_wifi_count':STATE.last_wifi_count,'last_bluetooth_count':STATE.last_bluetooth_count,'errors':STATE.errors[-10:], 'rides_dir':str(RIDES_DIR)}
            self.send(json.dumps(obj,indent=2,default=str),'application/json'); return
        rides=list_rides()
        with STATE.lock:
            status={'ride_id':STATE.ride_id,'started_at':STATE.started_at,'gps':STATE.last_gps,'env':STATE.last_env,'imu':STATE.last_imu,'ups':STATE.last_ups,'health':STATE.last_health,'camera':STATE.camera_status,'wifi_count':STATE.last_wifi_count,'bluetooth_count':STATE.last_bluetooth_count,'errors':STATE.errors[-5:]}
        controls='<form method="post" action="/api/start"><button>Start ride</button></form><form method="post" action="/api/stop"><button>Stop ride</button></form>'
        cards=''.join(f'<div class="card"><h2><a href="/ride/{html.escape(r["id"])}">{html.escape(r["id"])}</a></h2><pre>{html.escape(json.dumps(r,indent=2))}</pre></div>' for r in rides)
        body=f'{controls}<p><a href="/status.json">status.json</a></p><div class="card"><h2>Current status</h2><pre>{html.escape(json.dumps(status,indent=2,default=str))}</pre></div><h2>Historic rides</h2>{cards or "No rides yet."}'
        self.send(page('BikeLogger RideHub', body))

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
