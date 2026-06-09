import importlib.util
from pathlib import Path

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
