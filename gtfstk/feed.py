"""
This module defines a ``Feed`` class to represent GTFS feeds.
There is an instance attribute for every valid GTFS table (routes, stops, etc.), which stores the table as a Pandas DataFrame, or as ``None`` in case that table is missing.

The ``Feed`` class also has heaps of methods: a method to compute route stats, a method to compute screen line counts, validations methods, etc.
To ease reading, almost all of these methods are defined in other modules and grouped by theme (``routes.py``, ``stops.py``, etc.).
These methods, or rather functions that operate on feeds, are then imported within the ``Feed`` class.
However, this separation of methods messes up the ``Feed`` class documentation slightly by introducing an extra leading ``feed`` parameter in the method signatures.
Ignore that extra parameter; it refers to the ``Feed`` instance, usually called ``self`` and usually hidden automatically by documentation tools. 

Conventions in the code below:
    - Dates are encoded as date strings of the form YYMMDD
    - Times are encoded as time strings of the form HH:MM:SS with the possibility that the hour is greater than 24
    - 'DataFrame' and 'Series' refer to Pandas DataFrame and Series objects, respectively
"""
from pathlib import Path
import tempfile
import shutil
from copy import deepcopy
import dateutil.relativedelta as rd
from collections import OrderedDict
import json

import pandas as pd 
import numpy as np
import utm
import shapely.geometry as sg 

from . import constants as cs
from . import helpers as hp


class Feed(object):
    """
    An instance of this class represents a not-necessarily-valid GTFS feed, where GTFS tables are stored as DataFrames.
    Beware that the stop times DataFrame can be big (several gigabytes), so make sure you have enough memory to handle it.

    Public instance attributes:

    - ``dist_units``: a string in :const:`.constants.DIST_UNITS`; specifies the distance units to use when calculating various stats, such as route service distance; should match the implicit distance units of the  ``shape_dist_traveled`` column values, if present
    - ``agency``
    - ``stops``
    - ``routes``
    - ``trips``
    - ``stop_times``
    - ``calendar``
    - ``calendar_dates`` 
    - ``fare_attributes``
    - ``fare_rules``
    - ``shapes``
    - ``frequencies``
    - ``transfers``
    - ``feed_info``

    There are also a few private instance attributes that are derived from public attributes and are automatically updated when those public attributes change.
    However, for this update to work, you must update the primary attributes like this::

        feed.trips['route_short_name'] = 'bingo'
        feed.trips = feed.trips

    and **not** like this::

        feed.trips['route_short_name'] = 'bingo'

    The first way ensures that the altered trips DataFrame is saved as the new ``trips`` attribute, but the second way does not.
    """
    # Import heaps of methods from modules split by functionality; i learned this trick from https://groups.google.com/d/msg/comp.lang.python/goLBrqcozNY/DPgyaZ6gAwAJ
    from .calendar import get_dates, get_first_week
    from .routes import get_routes, compute_route_stats, compute_route_time_series, get_route_timetable, route_to_geojson
    from .shapes import build_geometry_by_shape, shapes_to_geojson, get_shapes_intersecting_geometry, append_dist_to_shapes
    from .stops import get_stops, build_geometry_by_stop, compute_stop_activity, compute_stop_stats, compute_stop_time_series, get_stop_timetable, get_stops_in_polygon
    from .stop_times import get_stop_times, append_dist_to_stop_times, get_start_and_end_times 
    from .trips import is_active_trip, get_trips, compute_trip_activity, compute_busiest_date, compute_trip_stats, locate_trips, trip_to_geojson
    from .miscellany import describe, assess_quality, convert_dist, compute_feed_stats, compute_feed_time_series, create_shapes, compute_bounds, compute_center, restrict_to_routes, restrict_to_polygon, compute_screen_line_counts
    from .cleaners import clean_ids, clean_stop_times, clean_route_short_names, drop_dead_routes, aggregate_routes, clean, drop_invalid_columns
    from .validators import validate, check_for_required_tables, check_for_required_columns, check_agency, check_calendar, check_calendar_dates, check_fare_attributes, check_fare_rules, check_feed_info, check_frequencies, check_routes, check_shapes, check_stops, check_stop_times, check_transfers, check_trips 


    def __init__(self, dist_units, agency=None, stops=None, routes=None, 
      trips=None, stop_times=None, calendar=None, calendar_dates=None, 
      fare_attributes=None, fare_rules=None, shapes=None, 
      frequencies=None, transfers=None, feed_info=None):
        """
        Assume that every non-None input is a Pandas DataFrame, except for ``dist_units`` which should be a string in :const:`.constants.DIST_UNITS`.

        No other format checking is performed.
        In particular, a Feed instance need not represent a valid GTFS feed.
        """
        # Set primary attributes; the @property magic below will then
        # validate some and automatically set secondary attributes
        for prop, val in locals().items():
            if prop in cs.FEED_ATTRS_PUBLIC:
                setattr(self, prop, val)        

    @property 
    def dist_units(self):
        """
        A public Feed attribute made into a property for easy validation.
        """
        return self._dist_units

    @dist_units.setter
    def dist_units(self, val):
        if val not in cs.DIST_UNITS:
            raise ValueError('Distance units are required and '\
              'must lie in {!s}'.format(cs.DIST_UNITS))
        else:
            self._dist_units = val

    # If ``self.trips`` changes then update ``self._trips_i``
    @property
    def trips(self):
        """
        A public Feed attribute made into a property for easy auto-updating of private feed attributes based on the trips DataFrame.
        """
        return self._trips

    @trips.setter
    def trips(self, val):
        self._trips = val 
        if val is not None and not val.empty:
            self._trips_i = self._trips.set_index('trip_id')
        else:
            self._trips_i = None

    # If ``self.calendar`` changes, then update ``self._calendar_i``
    @property
    def calendar(self):
        """
        A public Feed attribute made into a property for easy auto-updating of private feed attributes based on the calendar DataFrame.
        """
        return self._calendar

    @calendar.setter
    def calendar(self, val):
        self._calendar = val 
        if val is not None and not val.empty:
            self._calendar_i = self._calendar.set_index('service_id')
        else:
            self._calendar_i = None 

    # If ``self.calendar_dates`` changes, then update ``self._calendar_dates_g``
    @property 
    def calendar_dates(self):
        """
        A public Feed attribute made into a property for easy auto-updating of private feed attributes based on the calendar dates DataFrame.
        """        
        return self._calendar_dates 

    @calendar_dates.setter
    def calendar_dates(self, val):
        self._calendar_dates = val
        if val is not None and not val.empty:
            self._calendar_dates_g = self._calendar_dates.groupby(
              ['service_id', 'date'])
        else:
            self._calendar_dates_g = None

    def __eq__(self, other):
        """
        Define two feeds be equal if and only if their :const:`.constants.FEED_ATTRS` attributes are equal, or almost equal in the case of DataFrames (but not groupby DataFrames).
        Almost equality is checked via :func:`.helpers.almost_equal`, which   canonically sorts DataFrame rows and columns.
        """
        # Return False if failures
        for key in cs.FEED_ATTRS_PUBLIC:
            x = getattr(self, key)
            y = getattr(other, key)
            # DataFrame case
            if isinstance(x, pd.DataFrame):
                if not isinstance(y, pd.DataFrame) or\
                  not hp.almost_equal(x, y):
                    return False 
            # Other case
            else:
                if x != y:
                    return False
        # No failures
        return True

    def copy(self):
        """
        Return a copy of this feed, that is, a feed with all the same public and private attributes.
        """
        other = Feed(dist_units=self.dist_units)
        for key in set(cs.FEED_ATTRS) - set(['dist_units']):
            value = getattr(self, key)
            if isinstance(value, pd.DataFrame):
                # Pandas copy DataFrame
                value = value.copy()
            elif isinstance(value, pd.core.groupby.DataFrameGroupBy):
                # Pandas does not have a copy method for groupby objects
                # as far as i know
                value = deepcopy(value)
            setattr(other, key, value)
        
        return other


# -------------------------------------
# Functions about input and output
# -------------------------------------
def read_gtfs(path, dist_units=None):
    """
    Create a Feed object from the given path and given distance units.
    The path should be a directory containing GTFS text files or a zip file that unzips as a collection of GTFS text files (and not as a directory containing GTFS text files).
    """
    path = Path(path)
    if not path.exists():
        raise ValueError("Path {!s} does not exist".format(path))

    # Unzip path to temporary directory if necessary
    if path.is_file():
        zipped = True
        tmp_dir = tempfile.TemporaryDirectory()
        src_path = Path(tmp_dir.name)
        shutil.unpack_archive(str(path), tmp_dir.name, 'zip')
    else:
        zipped = False
        src_path = path

    # Read files into feed dictionary of DataFrames
    feed_dict = {table: None for table in cs.GTFS_REF['table']}
    for p in src_path.iterdir():
        table = p.stem
        if p.is_file() and table in feed_dict:
            feed_dict[table] = pd.read_csv(p, dtype=cs.DTYPE, encoding='utf-8-sig') 
            # utf-8-sig gets rid of the byte order mark (BOM);
            # see http://stackoverflow.com/questions/17912307/u-ufeff-in-python-string 
        
    feed_dict['dist_units'] = dist_units

    # Delete temporary directory
    if zipped:
        tmp_dir.cleanup()

    # Create feed 
    return Feed(**feed_dict)

def write_gtfs(feed, path, ndigits=6):
    """
    Export the given feed to the given path.
    If the path end in '.zip', then write the feed as a zip archive.
    Otherwise assume the path is a directory, and write the feed as a collection of CSV files to that directory, creating the directory if it does not exist.
    Round all decimals to ``ndigits`` decimal places.
    All distances will be the distance units ``feed.dist_units``.
    """
    path = Path(path)

    if path.suffix == '.zip':
        # Write to temporary directory before zipping
        zipped = True
        tmp_dir = tempfile.TemporaryDirectory()
        new_path = Path(tmp_dir.name)
    else:
        zipped = False
        if not path.exists():
            path.mkdir()
        new_path = path 

    for table in cs.GTFS_REF['table'].unique():
        f = getattr(feed, table)
        if f is None:
            continue

        f = f.copy()
        # Some columns need to be output as integers.
        # If there are NaNs in any such column, 
        # then Pandas will format the column as float, which we don't want.
        f_int_cols = set(cs.INT_COLS) & set(f.columns)
        for s in f_int_cols:
            f[s] = f[s].fillna(-1).astype(int).astype(str).\
              replace('-1', '')
        p = new_path/(table + '.txt')
        f.to_csv(str(p), index=False, float_format='%.{!s}f'.format(ndigits))

    # Zip directory 
    if zipped:
        basename = str(path.parent/path.stem)
        shutil.make_archive(basename, format='zip', root_dir=tmp_dir.name)    
        tmp_dir.cleanup()
