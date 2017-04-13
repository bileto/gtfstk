import pytest
import importlib
from pathlib import Path 

import pandas as pd 
from pandas.util.testing import assert_frame_equal, assert_series_equal
import numpy as np
from numpy.testing import assert_array_equal
import utm
import shapely.geometry as sg 

from .context import gtfstk, slow, HAS_GEOPANDAS, DATA_DIR, sample, cairns, cairns_date, cairns_trip_stats
from gtfstk import *


def test_valid_str():
    assert valid_str('hello3')
    assert not valid_str(np.nan)
    assert not valid_str(' ')

def test_valid_time():
    assert valid_time('2:43:00')
    assert not valid_time('32:43:00')

def test_valid_date():
    assert valid_date('20140310')
    assert not valid_date('2014031')

def test_valid_timezone():
    assert valid_timezone('Africa/Abidjan')
    assert not valid_timezone('zoom')

def test_valid_url():
    assert valid_url('http://www.example.com')
    assert not valid_url('www.example.com')

def test_valid_color():
    assert valid_color('00FFFF')
    assert not valid_color('0FF')
    assert not valid_color('GGFFFF')

def test_check_table():
    feed = sample.copy()
    cond = feed.routes['route_id'].isnull() 
    assert not check_table([], 'routes', feed.routes, cond, 'Bingo')
    assert check_table([], 'routes', feed.routes, ~cond, 'Bongo')

def test_check_column():
    feed = sample.copy()
    assert not check_column([], 'agency', feed.agency, 'agency_url', True,
      valid_url)
    feed.agency['agency_url'].iat[0] = 'example.com'
    assert check_column([], 'agency', feed.agency, 'agency_url', True,
      valid_url)

def test_check_column_id():
    feed = sample.copy()
    assert not check_column_id([], 'routes', feed.routes, 'route_id')
    feed.routes['route_id'].iat[0] = np.nan
    assert check_column_id([], 'routes', feed.routes, 'route_id')

def test_check_column_linked_id():
    feed = sample.copy()
    assert not check_column_linked_id([], 'trips', feed.trips, 'route_id',
      True, feed.routes)
    feed.trips['route_id'].iat[0] = 'Hummus!'
    assert check_column_linked_id([], 'trips', feed.trips, 'route_id',
      True, feed.routes)

def test_check_for_required_tables():
    assert not check_for_required_tables(sample)

    feed = sample.copy()
    feed.routes = None
    assert check_for_required_tables(feed)

def test_check_for_required_columns():
    assert not check_for_required_columns(sample)

    feed = sample.copy()
    del feed.routes['route_type']
    assert check_for_required_columns(feed)

def test_check_calendar():
    assert not check_calendar(sample)

    feed = sample.copy()
    feed.calendar['service_id'].iat[0] = feed.calendar['service_id'].iat[1]
    assert check_calendar(feed)

    for col in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday',
      'saturday', 'sunday', 'start_date', 'end_date']:
        feed = sample.copy()
        feed.calendar[col].iat[0] = '5'
        assert check_calendar(feed)

def test_check_routes():
    assert not check_routes(sample)

    feed = sample.copy()
    feed.routes['route_id'].iat[0] = feed.routes['route_id'].iat[1]
    assert check_routes(feed)

    feed = sample.copy()
    feed.routes['agency_id'] = 'Hubba hubba'
    assert check_routes(feed)

    feed = sample.copy()
    feed.routes['route_short_name'].iat[0] = ''
    assert check_routes(feed)

    feed = sample.copy()
    feed.routes['route_short_name'].iat[0] = ''
    feed.routes['route_long_name'].iat[0] = ''
    assert check_routes(feed)

    feed = sample.copy()
    feed.routes['route_type'].iat[0] = 8
    assert check_routes(feed)

    feed = sample.copy()
    feed.routes['route_color'].iat[0] = 'FFF'
    assert check_routes(feed)

    feed = sample.copy()
    feed.routes['route_text_color'].iat[0] = 'FFF'
    assert check_routes(feed)

def test_check_shapes():
    assert not check_shapes(sample)

    # Make a nonempty shapes table to check
    feed = sample.copy()
    rows = [
      ['1100015',-16.743632,145.668255,10001, 1.2],
      ['1100015',-16.743522,145.668394,10002, 1.3],
      ]
    columns=['shape_id', 'shape_pt_lat', 'shape_pt_lon', 'shape_pt_sequence', 'shape_dist_traveled']
    feed.shapes = pd.DataFrame(rows, columns=columns)
    assert not check_shapes(feed)

    feed1 = feed.copy()
    feed1.shapes['shape_id'].iat[0] = ''
    assert check_shapes(feed1)

    for column in ['shape_pt_lon', 'shape_pt_lat']:
        feed1 = feed.copy()
        feed1.shapes[column] = 185
        assert check_shapes(feed1)

    feed1 = feed.copy()
    feed1.shapes['shape_dist_traveled'].iat[1] = 0
    assert check_shapes(feed1)

def test_check_stops():
    assert not check_stops(sample)

    feed = sample.copy()
    feed.stops['stop_id'].iat[0] = feed.stops['stop_id'].iat[1]
    assert check_stops(feed)

    for column in ['stop_code', 'stop_desc', 'zone_id', 'parent_station']:
        feed = sample.copy()
        feed.stops[column] = ''
        assert check_stops(feed)    

    for column in ['stop_url', 'stop_timezone']:
        feed = sample.copy()
        feed.stops[column] = 'Wa wa'
        assert check_stops(feed)

    for column in ['stop_lon', 'stop_lat', 'location_type', 'wheelchair_boarding']:
        feed = sample.copy()
        feed.stops[column] = 185
        assert check_stops(feed)

    feed = sample.copy()
    feed.stops['location_type'] = 1
    feed.stops['parent_station'] = 'bingo' 
    assert check_stops(feed)

    feed = sample.copy()
    feed.stops['location_type'] = 0
    feed.stops['parent_station'] = feed.stops['stop_id'].iat[1]
    assert check_stops(feed)

def test_check_stop_times():
    assert not check_stop_times(sample)

    feed = sample.copy()
    feed.stop_times['trip_id'].iat[0] = 'bingo'
    assert check_stop_times(feed)

    for col in ['arrival_time', 'departure_time']:
        feed = sample.copy()
        feed.stop_times[col].iat[0] = '1:0:00'
        assert check_stop_times(feed)

    feed = sample.copy()
    feed.stop_times['stop_id'].iat[0] = 'bingo'
    assert check_stop_times(feed)

    feed = sample.copy()
    feed.stop_times['stop_headsign'].iat[0] = ''
    assert check_stop_times(feed)

    for col in ['pickup_type', 'drop_off_type']:
        feed = sample.copy()
        feed.stop_times[col] = 'bongo'
        assert check_stop_times(feed)

    feed = sample.copy()
    feed.stop_times['shape_dist_traveled'] = 1
    feed.stop_times['shape_dist_traveled'].iat[1] = 0.9
    assert check_stop_times(feed)

    feed = sample.copy()
    feed.stop_times['timepoint'] = 3
    assert check_stop_times(feed)

def test_check_trips():
    assert not check_trips(sample)

    feed = sample.copy()
    feed.trips['trip_id'].iat[0] = feed.trips['trip_id'].iat[1]
    assert check_trips(feed)

    feed = sample.copy()
    feed.trips['route_id'] = 'Hubba hubba'
    assert check_trips(feed)

    feed = sample.copy()
    feed.trips['service_id'] = 'Boom boom'
    assert check_trips(feed)

    feed = sample.copy()
    feed.trips['direction_id'].iat[0] = 7
    assert check_trips(feed)

    feed = sample.copy()
    feed.trips['block_id'].iat[0] = ''
    assert check_trips(feed)

    feed = sample.copy()
    feed.trips['block_id'].iat[0] = 'Bam'
    assert check_trips(feed)

    feed = sample.copy()
    feed.trips['shape_id'].iat[0] = 'Hello'
    assert check_trips(feed)

    feed = sample.copy()
    feed.trips['wheelchair_accessible'] = ''
    assert check_trips(feed)

def test_validate():    
    assert not validate(sample)