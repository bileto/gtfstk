"""
A class to represent partial and complete, valid and invalid GTFS feeds.
Functions for computing various quantities from a Feed object,
such as trips distances and durations.

CONVENTIONS:

In conformance with GTFS and unless specified otherwise, 
dates are encoded as date strings of 
the form YYMMDD and times are encoded as time strings of the form HH:MM:SS
with the possibility that the hour is greater than 24.

Unless specified otherwise, 'data frame' and 'series' refer to
Pandas data frames and series, respectively.
"""
from pathlib import Path
import datetime as dt
import dateutil.relativedelta as rd
from collections import OrderedDict, Counter
import os
import zipfile
import tempfile
import shutil
import json

import pandas as pd
import numpy as np
from shapely.geometry import Point, LineString, mapping
import utm

from . import utilities as utils


REQUIRED_GTFS_FILES = [
  'agency',  
  'stops',   
  'routes',
  'trips',
  'stop_times',
  'calendar',
  ]

OPTIONAL_GTFS_FILES = [
  'calendar_dates',  
  'fare_attributes',    
  'fare_rules',  
  'shapes',  
  'frequencies',     
  'transfers',   
  'feed_info',
  ]

DTYPE = {
  'stop_id': str, 
  'stop_code': str,
  'route_id': str, 
  'route_short_name': str,
  'trip_id': str, 
  'service_id': str, 
  'shape_id': str, 
  'start_date': str, 
  'end_date': str,
  'date': str,
}

# Columns that must be formatted as integers when outputting GTFS
INT_COLS = [
  'location_type',
  'wheelchair_boarding',
  'route_type',
  'direction_id',
  'stop_sequence',
  'wheelchair_accessible',
  'bikes_allowed',
  'pickup_type',
  'drop_off_type',
  'timepoint',
  'monday',
  'tuesday',
  'wednesday',
  'thursday',
  'friday',
  'saturday',
  'sunday',
  'exception_type',
  'payment_method',
  'transfers',
  'shape_pt_sequence',
  'exact_times',
  'transfer_type',
  'transfer_duration',
  'min_transfer_time',
]


# TODO: Explain attributes
class Feed(object):
    """
    A class to gather some or all GTFS files as data frames.
    That's it.
    Business logic lives outside the class.
    
    Warning: the stop times data frame can be big (several gigabytes), 
    so make sure you have enough memory to handle it.

    Attributes:

    - agency
    - stops
    - routes
    - trips 
    - stop_times
    - calendar
    - calendar_dates 
    - fare_attributes
    - fare_rules
    - shapes
    - frequencies
    - transfers
    - feed_info
    - dist_units_in
    - dist_units_out
    - convert_dist
    """
    def __init__(self, agency=None, stops=None, routes=None, trips=None, 
      stop_times=None, calendar=None, calendar_dates=None, 
      fare_attributes=None, fare_rules=None, shapes=None, 
      frequencies=None, transfers=None, feed_info=None,
      dist_units_in=None, dist_units_out=None):
        """
        Assume that every non-None input is a Pandas data frame,
        except for ``dist_units_in`` and ``dist_units_out`` which 
        should be strings.

        If the ``shapes`` or ``stop_times`` data frame has the optional
        column ``shape_dist_traveled``,
        then the native distance units used in those data frames must be 
        specified with ``dist_units_in``. 
        Supported distance units are listed in ``utils.DISTANCE_UNITS``.
        
        If ``shape_dist_traveled`` column does not exist, then 
        ``dist_units_in`` is not required and will be set to ``'km'``.
        The parameter ``dist_units_out`` specifies the distance units for 
        the outputs of functions that act on feeds, 
        e.g. ``compute_trips_stats()``.
        If ``dist_units_out`` is not specified, then it will be set to
        ``dist_units_in``.

        No other format checking is performed.
        In particular, a Feed instance need not represent a valid GTFS feed.
        """
        # Set attributes
        for kwarg, value in locals().items():
            if kwarg == 'self':
                continue
            setattr(self, kwarg, value)

        # Check for valid distance units
        # Require dist_units_in if feed has distances
        if (self.stop_times is not None and\
          'shape_dist_traveled' in self.stop_times.columns) or\
          (self.shapes is not None and\
          'shape_dist_traveled' in self.shapes.columns):
            if self.dist_units_in is None:
                raise ValueError(
                  'This feed has distances, so you must specify dist_units_in')    
        DU = utils.DISTANCE_UNITS
        for du in [self.dist_units_in, self.dist_units_out]:
            if du is not None and du not in DU:
                raise ValueError('Distance units must lie in {!s}'.format(DU))

        # Set defaults
        if self.dist_units_in is None:
            self.dist_units_in = 'km'
        if self.dist_units_out is None:
            self.dist_units_out = self.dist_units_in
        
        # Set distance conversion function
        self.convert_dist = utils.get_convert_dist(self.dist_units_in,
          self.dist_units_out)

        # Convert distances to dist_units_out if necessary
        if self.stop_times is not None and\
          'shape_dist_traveled' in self.stop_times.columns:
            self.stop_times['shape_dist_traveled'] =\
              self.stop_times['shape_dist_traveled'].map(self.convert_dist)

        if self.shapes is not None and\
          'shape_dist_traveled' in self. shapes.columns:
            self.shapes['shape_dist_traveled'] =\
              self.shapes['shape_dist_traveled'].map(self.convert_dist)

        # Create some extra data frames for fast searching
        if self.trips is not None and not self.trips.empty:
            self.trips_i = self.trips.set_index('trip_id')
        else:
            self.trips_i = None

        if self.calendar is not None and not self.calendar.empty:
            self.calendar_i = self.calendar.set_index('service_id')
        else:
            self.calendar_i = None 

        if self.calendar_dates is not None and not self.calendar_dates.empty:
            self.calendar_dates_g = self.calendar_dates.groupby(
              ['service_id', 'date'])
        else:
            self.calendar_dates_g = None

# -------------------------------------
# Functions about input and output
# -------------------------------------
def read_gtfs(path, dist_units_in=None, dist_units_out=None):
    """
    Create a Feed object from the given path and 
    given distance units.
    The path points to a directory containing GTFS text files or 
    a zip file that unzips as a collection of GTFS text files
    (but not as a directory containing GTFS text files).
    """
    # Unzip path if necessary
    zipped = False
    if zipfile.is_zipfile(path):
        # Extract to temporary location
        zipped = True
        archive = zipfile.ZipFile(path)
        path = path.rstrip('.zip') + '/'
        archive.extractall(path)

    path = Path(path)

    # Read files into feed dictionary of data frames
    feed_dict = {}
    for f in REQUIRED_GTFS_FILES + OPTIONAL_GTFS_FILES:
        ff = f + '.txt'
        p = Path(path, ff)
        if p.exists():
            feed_dict[f] = pd.read_csv(p.as_posix(), dtype=DTYPE)
        else:
            feed_dict[f] = None
        
    feed_dict['dist_units_in'] = dist_units_in
    feed_dict['dist_units_out'] = dist_units_out

    # Remove extracted zip directory
    if zipped:
        shutil.rmtree(path.as_posix())

    # Create feed 
    return Feed(**feed_dict)

def write_gtfs(feed, path, ndigits=6):
    """
    Export the given feed to a zip archive located at ``path``.
    Round all decimals to ``ndigits`` decimal places.
    All distances will be displayed in units ``feed.dist_units_out``.
    """
    # Remove '.zip' extension from path, because it gets added
    # automatically below
    path = path.rstrip('.zip')

    # Write files to a temporary directory 
    tmp_dir = tempfile.mkdtemp()
    names = REQUIRED_GTFS_FILES + OPTIONAL_GTFS_FILES
    int_cols_set = set(INT_COLS)
    for name in names:
        f = getattr(feed, name)
        if f is None:
            continue

        f = f.copy()
        # Some columns need to be output as integers.
        # If there are integers and NaNs in any such column, 
        # then Pandas will format the column as float, which we don't want.
        s = list(int_cols_set & set(f.columns))
        if s:
            f[s] = f[s].fillna(-1).astype(int).astype(str).\
              replace('-1', '')
        tmp_path = Path(tmp_dir, name + '.txt')
        f.to_csv(tmp_path.as_posix(), index=False, 
          float_format='%.{!s}f'.format(ndigits))

    # Zip directory 
    shutil.make_archive(path, format='zip', root_dir=tmp_dir)    

    # Delete temporary directory
    shutil.rmtree(tmp_dir)

# -------------------------------------
# Functions about calendars
# -------------------------------------
def get_dates(feed, as_date_obj=False):
    """
    Return a chronologically ordered list of dates
    for which this feed is valid.
    If ``as_date_obj == True``, then return the dates as
    as ``datetime.date`` objects.  
    """
    if feed.calendar is not None:
        start_date = feed.calendar['start_date'].min()
        end_date = feed.calendar['end_date'].max()
    else:
        # Use calendar_dates
        start_date = feed.calendar_dates['date'].min()
        end_date = feed.calendar_dates['date'].max()
    
    start_date = utils.datestr_to_date(start_date)
    end_date = utils.datestr_to_date(end_date)
    num_days = (end_date - start_date).days
    result = [start_date + rd.relativedelta(days=+d) 
      for d in range(num_days + 1)]
    
    if not as_date_obj:
        result = [utils.datestr_to_date(x, inverse=True)
          for x in result]
    
    return result

def get_first_week(feed, as_date_obj=False):
    """
    Return a list of date corresponding
    to the first Monday--Sunday week for which this feed is valid.
    In the unlikely event that this feed does not cover a full 
    Monday--Sunday week, then return whatever initial segment of the 
    week it does cover. 
    If ``as_date_obj == True``, then return the dates as
    as ``datetime.date`` objects.          
    """
    dates = get_dates(feed, as_date_obj=True)
    # Get first Monday
    monday_index = None
    for (i, date) in enumerate(dates):
        if date.weekday() == 0:
            monday_index = i
            break
    week = []
    for j in range(7):
        try:
            week.append(dates[monday_index + j])
        except:
            break
    # Convert to date strings if requested
    if not as_date_obj:
        week = [utils.datestr_to_date(x, inverse=True)
          for x in week]
    return week

# -------------------------------------
# Functions about trips
# -------------------------------------
def count_active_trips(trips, time):
    """
    Given a data frame containing the rows

    - trip_id
    - start_time: start time of the trip in seconds past midnight
    - end_time: end time of the trip in seconds past midnight

    and a time in seconds past midnight, return the number of 
    trips in the data frame that are active at the given time.
    A trip is a considered active at time t if 
    start_time <= t < end_time.
    """
    return trips[(trips['start_time'] <= time) &\
      (trips['end_time'] > time)].shape[0]

def is_active_trip(feed, trip, date):
    """
    If the given trip (trip ID) is active on the given date,
    then return ``True``; otherwise return ``False``.
    To avoid error checking in the interest of speed, 
    assume ``trip`` is a valid trip ID in the feed and 
    ``date`` is a valid date object.

    Assume the following feed attributes are not None:

    - trips_i

    NOTES: 

    This function is key for getting all trips, routes, 
    etc. that are active on a given date, so the function needs to be fast. 
    """
    service = feed.trips_i.at[trip, 'service_id']
    # Check feed.calendar_dates_g.
    caldg = feed.calendar_dates_g
    if caldg is not None:
        if (service, date) in caldg.groups:
            et = caldg.get_group((service, date))['exception_type'].iat[0]
            if et == 1:
                return True
            else:
                # Exception type is 2
                return False
    # Check feed.calendar_i
    cali = feed.calendar_i
    if cali is not None:
        if service in cali.index:
            weekday_str = utils.weekday_to_str(
              utils.datestr_to_date(date).weekday())
            if cali.at[service, 'start_date'] <= date <= cali.at[service,
              'end_date'] and cali.at[service, weekday_str] == 1:
                return True
            else:
                return False
    # If you made it here, then something went wrong
    return False

def get_trips(feed, date=None, time=None):
    """
    Return the section of ``feed.trips`` that contains
    only trips active on the given date.
    If the date is not given, then return all trips.
    If a date and time are given, 
    then return only those trips active at that date and time.
    Do not take times modulo 24.
    """
    f = feed.trips.copy()
    if date is None:
        return f

    f['is_active'] = f['trip_id'].map(
      lambda trip: feed.is_active_trip(trip, date))
    f = f[f['is_active']]
    del f['is_active']

    if time is not None:
        # Get trips active during given time
        g = f.merge(feed.stop_times[['trip_id', 'departure_time']])
      
        def F(group):
            d = {}
            start = group['departure_time'].min()
            end = group['departure_time'].max()
            try:
                result = start <= time <= end
            except TypeError:
                result = False
            d['is_active'] = result
            return pd.Series(d)

        h = g.groupby('trip_id').apply(F).reset_index()
        f = f.merge(h[h['is_active']])
        del f['is_active']

    return f

def compute_trips_activity(feed, dates):
    """
    Return a  data frame with the columns

    - trip_id
    - ``dates[0]``: 1 if the trip is active on ``dates[0]``; 
      0 otherwise
    - ``dates[1]``: 1 if the trip is active on ``dates[0]``; 
      0 otherwise
    - etc.
    - ``dates[-1]``: 1 if the trip is active on ``dates[-1]``; 
      0 otherwise

    If ``dates`` is ``None`` or the empty list, then return an 
    empty data frame with the column 'trip_id'.
    """
    if not dates:
        return pd.DataFrame(columns=['trip_id'])

    f = feed.trips.copy()
    for date in dates:
        f[date] = f['trip_id'].map(lambda trip: 
          int(feed.is_active_trip(trip, date)))
    return f[['trip_id'] + dates]

def get_busiest_date(feed, dates):
    """
    Given a list of dates, return the first date that has the 
    maximum number of active trips.
    """
    f = compute_trips_activity(feed, dates)
    s = [(f[date].sum(), date) for date in dates]
    return max(s)[1]

def compute_trips_stats(feed, compute_dist_from_shapes=False):
    """
    Return a  data frame with the following columns:

    - trip_id
    - route_id
    - route_short_name
    - direction_id
    - shape_id
    - num_stops: number of stops on trip
    - start_time: first departure time of the trip
    - end_time: last departure time of the trip
    - start_stop_id: stop ID of the first stop of the trip 
    - end_stop_id: stop ID of the last stop of the trip
    - is_loop: 1 if the start and end stop are less than 400m apart and
      0 otherwise
    - distance: distance of the trip in ``feed.dist_units_out``; 
      contains all ``np.nan`` entries if ``feed.shapes is None``
    - duration: duration of the trip in hours
    - speed: distance/duration

    NOTES:

    If ``feed.stop_times`` has a ``shape_dist_traveled`` column
    and ``compute_dist_from_shapes == False``,
    then use that column to compute the distance column.
    Else if ``feed.shapes is not None``, then compute the distance 
    column using the shapes and Shapely. 
    Otherwise, set the distances to ``np.nan``.

    Calculating trip distances with ``compute_dist_from_shapes=True``
    seems pretty accurate.
    For example, calculating trip distances on the Portland feed using
    ``compute_dist_from_shapes=False`` and ``compute_dist_from_shapes=True``,
    yields a difference of at most 0.83km.
    """        

    # Start with stop times and extra trip info.
    # Convert departure times to seconds past midnight to 
    # compute durations.
    f = feed.trips[['route_id', 'trip_id', 'direction_id', 'shape_id']]
    f = f.merge(feed.routes[['route_id', 'route_short_name']])
    f = f.merge(feed.stop_times).sort_values(['trip_id', 'stop_sequence'])
    f['departure_time'] = f['departure_time'].map(utils.timestr_to_seconds)
    
    # Compute all trips stats except distance, 
    # which is possibly more involved
    geometry_by_stop = feed.build_geometry_by_stop(use_utm=True)
    g = f.groupby('trip_id')

    def my_agg(group):
        d = OrderedDict()
        d['route_id'] = group['route_id'].iat[0]
        d['route_short_name'] = group['route_short_name'].iat[0]
        d['direction_id'] = group['direction_id'].iat[0]
        d['shape_id'] = group['shape_id'].iat[0]
        d['num_stops'] = group.shape[0]
        d['start_time'] = group['departure_time'].iat[0]
        d['end_time'] = group['departure_time'].iat[-1]
        d['start_stop_id'] = group['stop_id'].iat[0]
        d['end_stop_id'] = group['stop_id'].iat[-1]
        dist = geometry_by_stop[d['start_stop_id']].distance(
          geometry_by_stop[d['end_stop_id']])
        d['is_loop'] = int(dist < 400)
        d['duration'] = (d['end_time'] - d['start_time'])/3600
        return pd.Series(d)

    # Apply my_agg, but don't reset index yet.
    # Need trip ID as index to line up the results of the 
    # forthcoming distance calculation
    h = g.apply(my_agg)  

    # Compute distance
    if 'shape_dist_traveled' in f.columns and not compute_dist_from_shapes:
        # Compute distances using shape_dist_traveled column
        h['distance'] = g.apply(
          lambda group: group['shape_dist_traveled'].max())
    elif feed.shapes is not None:
        # Compute distances using the shapes and Shapely
        geometry_by_shape = feed.build_geometry_by_shape()
        geometry_by_stop = feed.build_geometry_by_stop()
        m_to_dist = utils.get_convert_dist('m', feed.dist_units_out)

        def compute_dist(group):
            """
            Return the distance traveled along the trip between the first
            and last stops.
            If that distance is negative or if the trip's linestring 
            intersects itfeed, then return the length of the trip's 
            linestring instead.
            """
            shape = group['shape_id'].iat[0]
            try:
                # Get the linestring for this trip
                linestring = geometry_by_shape[shape]
            except KeyError:
                # Shape ID is NaN or doesn't exist in shapes.
                # No can do.
                return np.nan 
            
            # If the linestring intersects itfeed, then that can cause
            # errors in the computation below, so just 
            # return the length of the linestring as a good approximation
            if not linestring.is_simple:
                return linestring.length

            # Otherwise, return the difference of the distances along
            # the linestring of the first and last stop
            start_stop = group['stop_id'].iat[0]
            end_stop = group['stop_id'].iat[-1]
            try:
                start_point = geometry_by_stop[start_stop]
                end_point = geometry_by_stop[end_stop]
            except KeyError:
                # One of the two stop IDs is NaN, so just
                # return the length of the linestring
                return linestring.length
            d1 = linestring.project(start_point)
            d2 = linestring.project(end_point)
            d = d2 - d1
            if d > 0:
                return d
            else:
                # Something is probably wrong, so just
                # return the length of the linestring
                return linestring.length

        h['distance'] = g.apply(compute_dist)
        # Convert from meters
        h['distance'] = h['distance'].map(m_to_dist)
    else:
        h['distance'] = np.nan

    # Reset index and compute final stats
    h = h.reset_index()
    h['speed'] = h['distance']/h['duration']
    h[['start_time', 'end_time']] = h[['start_time', 'end_time']].\
      applymap(lambda x: utils.timestr_to_seconds(x, inverse=True))
    
    return h.sort_values(['route_id', 'direction_id', 'start_time'])

def compute_trips_locations(feed, date, times):
    """
    Return a  data frame of the positions of all trips
    active on the given date and times 
    Include the columns:

    - trip_id
    - route_id
    - direction_id
    - time
    - rel_dist: number between 0 (start) and 1 (end) indicating 
      the relative distance of the trip along its path
    - lon: longitude of trip at given time
    - lat: latitude of trip at given time

    Assume ``feed.stop_times`` has an accurate ``shape_dist_traveled``
    column.
    """
    if 'shape_dist_traveled' not in feed.stop_times.columns:
        raise ValueError(
          "The shape_dist_traveled column is required "\
          "in feed.stop_times. "\
          "You can create it, possibly with some inaccuracies, "\
          "via feed.stop_times = feed.add_dist_to_stop_times().")
    
    # Start with stop times active on date
    f = feed.get_stop_times(date)
    f['departure_time'] = f['departure_time'].map(
      utils.timestr_to_seconds)

    # Compute relative distance of each trip along its path
    # at the given time times.
    # Use linear interpolation based on stop departure times and
    # shape distance traveled.
    geometry_by_shape = feed.build_geometry_by_shape(use_utm=False)
    sample_times = np.array([utils.timestr_to_seconds(s) 
      for s in times])
    
    def compute_rel_dist(group):
        dists = sorted(group['shape_dist_traveled'].values)
        times = sorted(group['departure_time'].values)
        ts = sample_times[(sample_times >= times[0]) &\
          (sample_times <= times[-1])]
        ds = np.interp(ts, times, dists)
        return pd.DataFrame({'time': ts, 'rel_dist': ds/dists[-1]})
    
    # return f.groupby('trip_id', group_keys=False).\
    #   apply(compute_rel_dist).reset_index()
    g = f.groupby('trip_id').apply(compute_rel_dist).reset_index()
    
    # Delete extraneous multi-index column
    del g['level_1']
    
    # Convert times back to time strings
    g['time'] = g['time'].map(
      lambda x: utils.timestr_to_seconds(x, inverse=True))

    # Merge in more trip info and
    # compute longitude and latitude of trip from relative distance
    h = g.merge(feed.trips[['trip_id', 'route_id', 'direction_id', 
      'shape_id']])
    if not h.shape[0]:
        # Return a data frame with the promised headers but no data.
        # Without this check, result below could be an empty data frame.
        h['lon'] = pd.Series()
        h['lat'] = pd.Series()
        return h

    def get_lonlat(group):
        shape = group['shape_id'].iat[0]
        linestring = geometry_by_shape[shape]
        lonlats = [linestring.interpolate(d, normalized=True).coords[0]
          for d in group['rel_dist'].values]
        group['lon'], group['lat'] = zip(*lonlats)
        return group
    
    return h.groupby('shape_id').apply(get_lonlat)
    
# -------------------------------------
# Functions about routes
# -------------------------------------
def get_routes(feed, date=None, time=None):
    """
    Return the section of ``feed.routes`` that contains
    only routes active on the given date.
    If no date is given, then return all routes.
    If a date and time are given, then return only those routes with
    trips active at that date and time.
    Do not take times modulo 24.
    """
    if date is None:
        return feed.routes.copy()

    trips = feed.get_trips(date, time)
    R = trips['route_id'].unique()
    return feed.routes[feed.routes['route_id'].isin(R)]

def compute_routes_stats_base(trips_stats_subset, split_directions=False,
    headway_start_time='07:00:00', headway_end_time='19:00:00'):
    """
    Given a subset of the output of ``Feed.compute_trips_stats()``, 
    calculate stats for the routes in that subset.
    
    Return a data frame with the following columns:

    - route_id
    - route_short_name
    - direction_id
    - num_trips: number of trips
    - is_loop: 1 if at least one of the trips on the route has its
      ``is_loop`` field equal to 1; 0 otherwise
    - is_bidirectional: 1 if the route has trips in both directions;
      0 otherwise
    - start_time: start time of the earliest trip on 
      the route
    - end_time: end time of latest trip on the route
    - max_headway: maximum of the durations (in minutes) between 
      trip starts on the route between ``headway_start_time`` and 
      ``headway_end_time`` on the given dates
    - min_headway: minimum of the durations (in minutes) mentioned above
    - mean_headway: mean of the durations (in minutes) mentioned above
    - peak_num_trips: maximum number of simultaneous trips in service
      (for the given direction, or for both directions when 
      ``split_directions==False``)
    - peak_start_time: start time of first longest period during which
      the peak number of trips occurs
    - peak_end_time: end time of first longest period during which
      the peak number of trips occurs
    - service_duration: total of the duration of each trip on 
      the route in the given subset of trips; measured in hours
    - service_distance: total of the distance traveled by each trip on 
      the route in the given subset of trips;
      measured in wunits, that is, 
      whatever distance units are present in trips_stats_subset; 
      contains all ``np.nan`` entries if ``feed.shapes is None``  
    - service_speed: service_distance/service_duration;
      measured in wunits per hour
    - mean_trip_distance: service_distance/num_trips
    - mean_trip_duration: service_duration/num_trips

    If ``split_directions == False``, then remove the direction_id column
    and compute each route's stats, except for headways, using its trips
    running in both directions. 
    In this case, (1) compute max headway by taking the max of the max 
    headways in both directions; 
    (2) compute mean headway by taking the weighted mean of the mean
    headways in both directions. 

    If ``trips_stats_subset`` is empty, return an empty data frame with
    the columns specified above.
    """        
    cols = [
      'route_id',
      'route_short_name',
      'num_trips',
      'is_loop',
      'is_bidirectional',
      'start_time',
      'end_time',
      'max_headway',
      'min_headway',
      'mean_headway',
      'peak_num_trips',
      'peak_start_time',
      'peak_end_time',
      'service_duration',
      'service_distance',
      'service_speed',
      'mean_trip_distance',
      'mean_trip_duration',
      ]

    if split_directions:
        cols.append('direction_id')

    if trips_stats_subset.empty:
        return pd.DataFrame(columns=cols)

    # Convert trip start and end times to seconds to ease calculations below
    f = trips_stats_subset.copy()
    f[['start_time', 'end_time']] = f[['start_time', 'end_time']].\
      applymap(utils.timestr_to_seconds)

    headway_start = utils.timestr_to_seconds(headway_start_time)
    headway_end = utils.timestr_to_seconds(headway_end_time)

    def compute_route_stats_split_directions(group):
        # Take this group of all trips stats for a single route
        # and compute route-level stats.
        d = OrderedDict()
        d['route_short_name'] = group['route_short_name'].iat[0]
        d['num_trips'] = group.shape[0]
        d['is_loop'] = int(group['is_loop'].any())
        d['start_time'] = group['start_time'].min()
        d['end_time'] = group['end_time'].max()

        # Compute max and mean headway
        stimes = group['start_time'].values
        stimes = sorted([stime for stime in stimes 
          if headway_start <= stime <= headway_end])
        headways = np.diff(stimes)
        if headways.size:
            d['max_headway'] = np.max(headways)/60  # minutes 
            d['min_headway'] = np.min(headways)/60  # minutes 
            d['mean_headway'] = np.mean(headways)/60  # minutes 
        else:
            d['max_headway'] = np.nan
            d['min_headway'] = np.nan
            d['mean_headway'] = np.nan

        # Compute peak num trips
        times = np.unique(group[['start_time', 'end_time']].values)
        counts = [count_active_trips(group, t) for t in times]
        start, end = utils.get_peak_indices(times, counts)
        d['peak_num_trips'] = counts[start]
        d['peak_start_time'] = times[start]
        d['peak_end_time'] = times[end]

        d['service_distance'] = group['distance'].sum()
        d['service_duration'] = group['duration'].sum()
        return pd.Series(d)

    def compute_route_stats(group):
        d = OrderedDict()
        d['route_short_name'] = group['route_short_name'].iat[0]
        d['num_trips'] = group.shape[0]
        d['is_loop'] = int(group['is_loop'].any())
        d['is_bidirectional'] = int(group['direction_id'].unique().size > 1)
        d['start_time'] = group['start_time'].min()
        d['end_time'] = group['end_time'].max()

        # Compute headway stats
        headways = np.array([])
        for direction in [0, 1]:
            stimes = group[group['direction_id'] == direction][
              'start_time'].values
            stimes = sorted([stime for stime in stimes 
              if headway_start <= stime <= headway_end])
            headways = np.concatenate([headways, np.diff(stimes)])
        if headways.size:
            d['max_headway'] = np.max(headways)/60  # minutes 
            d['min_headway'] = np.min(headways)/60  # minutes 
            d['mean_headway'] = np.mean(headways)/60  # minutes
        else:
            d['max_headway'] = np.nan
            d['min_headway'] = np.nan
            d['mean_headway'] = np.nan

        # Compute peak num trips
        times = np.unique(group[['start_time', 'end_time']].values)
        counts = [count_active_trips(group, t) for t in times]
        start, end = utils.get_peak_indices(times, counts)
        d['peak_num_trips'] = counts[start]
        d['peak_start_time'] = times[start]
        d['peak_end_time'] = times[end]

        d['service_distance'] = group['distance'].sum()
        d['service_duration'] = group['duration'].sum()

        return pd.Series(d)

    if split_directions:
        g = f.groupby(['route_id', 'direction_id']).apply(
          compute_route_stats_split_directions).reset_index()
        
        # Add the is_bidirectional column
        def is_bidirectional(group):
            d = {}
            d['is_bidirectional'] = int(
              group['direction_id'].unique().size > 1)
            return pd.Series(d)   

        gg = g.groupby('route_id').apply(is_bidirectional).reset_index()
        g = g.merge(gg)
    else:
        g = f.groupby('route_id').apply(
          compute_route_stats).reset_index()

    # Compute a few more stats
    g['service_speed'] = g['service_distance']/g['service_duration']
    g['mean_trip_distance'] = g['service_distance']/g['num_trips']
    g['mean_trip_duration'] = g['service_duration']/g['num_trips']

    # Convert route times to time strings
    g[['start_time', 'end_time', 'peak_start_time', 'peak_end_time']] =\
      g[['start_time', 'end_time', 'peak_start_time', 'peak_end_time']].\
      applymap(lambda x: utils.timestr_to_seconds(x, inverse=True))

    return g

def compute_routes_stats(feed, trips_stats, date, split_directions=False,
    headway_start_time='07:00:00', headway_end_time='19:00:00'):
    """
    Take ``trips_stats``, which is the output of 
    ``compute_trips_stats()``, cut it down to the subset ``S`` of trips
    that are active on the given date, and then call
    ``compute_routes_stats_base()`` with ``S`` and the keyword arguments
    ``split_directions``, ``headway_start_time``, and 
    ``headway_end_time``.

    See ``compute_routes_stats_base()`` for a description of the output.

    NOTES:

    This is a more user-friendly version of ``compute_routes_stats_base()``.
    The latter function works without a feed, though.

    Return ``None`` if the date does not lie in this feed's date range.
    """
    # Get the subset of trips_stats that contains only trips active
    # on the given date
    trips_stats_subset = trips_stats.merge(feed.get_trips(date))
    return compute_routes_stats_base(trips_stats_subset, 
      split_directions=split_directions,
      headway_start_time=headway_start_time, 
      headway_end_time=headway_end_time)


def compute_routes_time_series_base(trips_stats_subset,
  split_directions=False, freq='5Min', date_label='20010101'):
    """
    Given a subset of the output of ``Feed.compute_trips_stats()``, 
    calculate time series for the routes in that subset.

    Return a time series version of the following route stats:
    
    - number of trips in service by route ID
    - number of trip starts by route ID
    - service duration in hours by route ID
    - service distance in kilometers by route ID
    - service speed in kilometers per hour

    The time series is a data frame with a timestamp index 
    for a 24-hour period sampled at the given frequency.
    The maximum allowable frequency is 1 minute.
    ``date_label`` is used as the date for the timestamp index.

    The columns of the data frame are hierarchical (multi-index) with

    - top level: name = 'indicator', values = ['service_distance',
      'service_duration', 'num_trip_starts', 'num_trips', 'service_speed']
    - middle level: name = 'route_id', values = the active routes
    - bottom level: name = 'direction_id', values = 0s and 1s

    If ``split_directions == False``, then don't include the bottom level.
    
    If ``trips_stats_subset`` is empty, then return an empty data frame
    with the indicator columns.

    NOTES:

    - To resample the resulting time series use the following methods:
        - for 'num_trips' series, use ``how=np.mean``
        - for the other series, use ``how=np.sum`` 
        - 'service_speed' can't be resampled and must be recalculated
          from 'service_distance' and 'service_duration' 
    - To remove the date and seconds from the 
      time series f, do ``f.index = [t.time().strftime('%H:%M') 
      for t in f.index.to_datetime()]``
    """  
    cols = [
      'service_distance',
      'service_duration', 
      'num_trip_starts', 
      'num_trips', 
      'service_speed',
      ]
    if trips_stats_subset.empty:
        return pd.DataFrame(columns=cols)

    tss = trips_stats_subset.copy()
    if split_directions:
        # Alter route IDs to encode direction: 
        # <route ID>-0 and <route ID>-1
        tss['route_id'] = tss['route_id'] + '-' +\
          tss['direction_id'].map(str)
        
    routes = tss['route_id'].unique()
    # Build a dictionary of time series and then merge them all
    # at the end
    # Assign a uniform generic date for the index
    date_str = date_label
    day_start = pd.to_datetime(date_str + ' 00:00:00')
    day_end = pd.to_datetime(date_str + ' 23:59:00')
    rng = pd.period_range(day_start, day_end, freq='Min')
    indicators = [
      'num_trip_starts', 
      'num_trips', 
      'service_duration', 
      'service_distance',
      ]
    
    bins = [i for i in range(24*60)] # One bin for each minute
    num_bins = len(bins)

    # Bin start and end times
    def F(x):
        return (utils.timestr_to_seconds(x)//60) % (24*60)

    tss[['start_index', 'end_index']] =\
      tss[['start_time', 'end_time']].applymap(F)
    routes = sorted(set(tss['route_id'].values))

    # Bin each trip according to its start and end time and weight
    series_by_route_by_indicator = {indicator: 
      {route: [0 for i in range(num_bins)] for route in routes} 
      for indicator in indicators}
    for index, row in tss.iterrows():
        trip = row['trip_id']
        route = row['route_id']
        start = row['start_index']
        end = row['end_index']
        distance = row['distance']

        if start is None or np.isnan(start) or start == end:
            continue

        # Get bins to fill
        if start <= end:
            bins_to_fill = bins[start:end]
        else:
            bins_to_fill = bins[start:] + bins[:end] 

        # Bin trip
        # Do num trip starts
        series_by_route_by_indicator['num_trip_starts'][route][start] += 1
        # Do rest of indicators
        for indicator in indicators[1:]:
            if indicator == 'num_trips':
                weight = 1
            elif indicator == 'service_duration':
                weight = 1/60
            else:
                weight = distance/len(bins_to_fill)
            for bin in bins_to_fill:
                series_by_route_by_indicator[indicator][route][bin] += weight

    # Create one time series per indicator
    rng = pd.date_range(date_str, periods=24*60, freq='Min')
    series_by_indicator = {indicator:
      pd.DataFrame(series_by_route_by_indicator[indicator],
        index=rng).fillna(0)
      for indicator in indicators}

    # Combine all time series into one time series
    g = combine_time_series(series_by_indicator, kind='route',
      split_directions=split_directions)
    return downsample(g, freq=freq)

def compute_routes_time_series(feed, trips_stats, date, 
  split_directions=False, freq='5Min'):
    """
    Take ``trips_stats``, which is the output of 
    ``compute_trips_stats()``, cut it down to the subset ``S`` of trips
    that are active on the given date, and then call
    ``compute_routes_time_series_base()`` with ``S`` and the given 
    keyword arguments ``split_directions`` and ``freq``
    and with ``date_label = utils.date_to_str(date)``.

    See ``compute_routes_time_series_base()`` for a description of the output.

    If there are no active trips on the date, then return ``None``.

    NOTES:

    This is a more user-friendly version of 
    ``compute_routes_time_series_base()``.
    The latter function works without a feed, though.
    """  
    trips_stats_subset = trips_stats.merge(feed.get_trips(date))
    return compute_routes_time_series_base(trips_stats_subset, 
      split_directions=split_directions, freq=freq, 
      date_label=date)

def get_route_timetable(feed, route_id, date):
    """
    Return a data frame encoding the timetable
    for the given route ID on the given date.
    The columns are all those in ``feed.trips`` plus those in 
    ``feed.stop_times``.
    The result is sorted by grouping by trip ID and
    sorting the groups by their first departure time.
    """
    f = feed.get_trips(date)
    f = f[f['route_id'] == route_id].copy()
    f = f.merge(feed.stop_times)
    # Groupby trip ID and sort groups by their minimum departure time.
    # For some reason NaN departure times mess up the transform below.
    # So temporarily fill NaN departure times as a workaround.
    f['dt'] = f['departure_time'].fillna(method='ffill')
    f['min_dt'] = f.groupby('trip_id')['dt'].transform(min)
    return f.sort_values(['min_dt', 'stop_sequence']).drop(['min_dt', 'dt'], 
      axis=1)

# -------------------------------------
# Functions about stops
# -------------------------------------
def get_stops(feed, date=None):
    """
    Return the section of ``feed.stops`` that contains
    only stops that have visiting trips active on the given date.
    If no date is given, then return all stops.
    """
    if date is None:
        return feed.stops.copy()

    stop_times = feed.get_stop_times(date)
    S = stop_times['stop_id'].unique()
    return feed.stops[feed.stops['stop_id'].isin(S)]

def build_geometry_by_stop(feed, use_utm=True):
    """
    Return a dictionary with structure
    stop_id -> Shapely point object.
    If ``use_utm == True``, then return each point in
    in UTM coordinates.
    Otherwise, return each point in WGS84 longitude-latitude
    coordinates.
    """
    geometry_by_stop = {}
    if use_utm:
        for stop, group in feed.stops.groupby('stop_id'):
            lat, lon = group[['stop_lat', 'stop_lon']].values[0]
            geometry_by_stop[stop] = Point(utm.from_latlon(lat, lon)[:2]) 
    else:
        for stop, group in feed.stops.groupby('stop_id'):
            lat, lon = group[['stop_lat', 'stop_lon']].values[0]
            geometry_by_stop[stop] = Point([lon, lat]) 
    return geometry_by_stop

def compute_stops_activity(feed, dates):
    """
    Return a  data frame with the columns

    - stop_id
    - ``dates[0]``: 1 if the stop has at least one trip visiting it 
      on ``dates[0]``; 0 otherwise 
    - ``dates[1]``: 1 if the stop has at least one trip visiting it 
      on ``dates[1]``; 0 otherwise 
    - etc.
    - ``dates[-1]``: 1 if the stop has at least one trip visiting it 
      on ``dates[-1]``; 0 otherwise 

    If ``dates`` is ``None`` or the empty list, 
    then return an empty data frame with the column 'stop_id'.
    """
    if not dates:
        return pd.DataFrame(columns=['stop_id'])

    trips_activity = feed.compute_trips_activity(dates)
    g = trips_activity.merge(feed.stop_times).groupby('stop_id')
    # Pandas won't allow me to simply return g[dates].max().reset_index().
    # I get ``TypeError: unorderable types: datetime.date() < str()``.
    # So here's a workaround.
    for (i, date) in enumerate(dates):
        if i == 0:
            f = g[date].max().reset_index()
        else:
            f = f.merge(g[date].max().reset_index())
    return f

def compute_stops_stats_base(stop_times, trips_subset, split_directions=False,
    headway_start_time='07:00:00', headway_end_time='19:00:00'):
    """
    Given a stop times data frame and a subset of a trips data frame,
    return a data frame that provides summary stats about
    the stops in the (inner) join of the two data frames.

    The columns of the output data frame are:

    - stop_id
    - direction_id: present iff ``split_directions == True``
    - num_routes: number of routes visiting stop (in the given direction)
    - num_trips: number of trips visiting stop (in the givin direction)
    - max_headway: maximum of the durations (in minutes) between 
      trip departures at the stop between ``headway_start_time`` and 
      ``headway_end_time`` on the given date
    - min_headway: minimum of the durations (in minutes) mentioned above
    - mean_headway: mean of the durations (in minutes) mentioned above
    - start_time: earliest departure time of a trip from this stop
      on the given date
    - end_time: latest departure time of a trip from this stop
      on the given date

    If ``split_directions == False``, then compute each stop's stats
    using trips visiting it from both directions.

    If ``trips_subset`` is empty, then return an empty data frame
    with the columns specified above.
    """
    cols = [
      'stop_id',
      'num_routes',
      'num_trips',
      'max_headway',
      'min_headway',
      'mean_headway',
      'start_time',
      'end_time',
      ]

    if split_directions:
        cols.append('direction_id')

    if trips_subset.empty:
        return pd.DataFrame(columns=cols)

    f = stop_times.merge(trips_subset)

    # Convert departure times to seconds to ease headway calculations
    f['departure_time'] = f['departure_time'].map(utils.timestr_to_seconds)

    headway_start = utils.timestr_to_seconds(headway_start_time)
    headway_end = utils.timestr_to_seconds(headway_end_time)

    # Compute stats for each stop
    def compute_stop_stats(group):
        # Operate on the group of all stop times for an individual stop
        d = OrderedDict()
        d['num_routes'] = group['route_id'].unique().size
        d['num_trips'] = group.shape[0]
        d['start_time'] = group['departure_time'].min()
        d['end_time'] = group['departure_time'].max()
        headways = []
        dtimes = sorted([dtime for dtime in group['departure_time'].values
          if headway_start <= dtime <= headway_end])
        headways.extend([dtimes[i + 1] - dtimes[i] 
          for i in range(len(dtimes) - 1)])
        if headways:
            d['max_headway'] = np.max(headways)/60  # minutes
            d['min_headway'] = np.min(headways)/60  # minutes
            d['mean_headway'] = np.mean(headways)/60  # minutes
        else:
            d['max_headway'] = np.nan
            d['min_headway'] = np.nan
            d['mean_headway'] = np.nan
        return pd.Series(d)

    if split_directions:
        g = f.groupby(['stop_id', 'direction_id'])
    else:
        g = f.groupby('stop_id')

    result = g.apply(compute_stop_stats).reset_index()

    # Convert start and end times to time strings
    result[['start_time', 'end_time']] =\
      result[['start_time', 'end_time']].applymap(
      lambda x: utils.timestr_to_seconds(x, inverse=True))

    return result

def compute_stops_stats(feed, date, split_directions=False,
    headway_start_time='07:00:00', headway_end_time='19:00:00'):
    """
    Call ``compute_stops_stats_base()`` with the subset of trips active on 
    the given date and with the keyword arguments ``split_directions``,
    ``headway_start_time``, and ``headway_end_time``.

    See ``compute_stops_stats_base()`` for a description of the output.

    NOTES:

    This is a more user-friendly version of ``compute_stops_stats_base()``.
    The latter function works without a feed, though.
    """
    # Get stop times active on date and direction IDs
    return compute_stops_stats_base(feed.stop_times, feed.get_trips(date),
      split_directions=split_directions,
      headway_start_time=headway_start_time, 
      headway_end_time=headway_end_time)

def compute_stops_time_series_base(stop_times, trips_subset, 
  split_directions=False, freq='5Min', date_label='20010101'):
    """
    Given a stop times data frame and a subset of a trips data frame,
    return a data frame that provides summary stats about
    the stops in the (inner) join of the two data frames.

    The time series is a data frame with a timestamp index 
    for a 24-hour period sampled at the given frequency.
    The maximum allowable frequency is 1 minute.
    The timestamp includes the date given by ``date_label``,
    a date string of the form '%Y%m%d'.
    
    The columns of the data frame are hierarchical (multi-index) with

    - top level: name = 'indicator', values = ['num_trips']
    - middle level: name = 'stop_id', values = the active stop IDs
    - bottom level: name = 'direction_id', values = 0s and 1s

    If ``split_directions == False``, then don't include the bottom level.
    
    If ``trips_subset`` is empty, then return an empty data frame
    with the indicator columns.

    NOTES:

    - 'num_trips' should be resampled with ``how=np.sum``
    - To remove the date and seconds from 
      the time series f, do ``f.index = [t.time().strftime('%H:%M') 
      for t in f.index.to_datetime()]``
    """  
    cols = ['num_trips']
    if trips_subset.empty:
        return pd.DataFrame(columns=cols)

    f = stop_times.merge(trips_subset)

    if split_directions:
        # Alter stop IDs to encode trip direction: 
        # <stop ID>-0 and <stop ID>-1
        f['stop_id'] = f['stop_id'] + '-' +\
          f['direction_id'].map(str)            
    stops = f['stop_id'].unique()   

    # Create one time series for each stop. Use a list first.    
    bins = [i for i in range(24*60)] # One bin for each minute
    num_bins = len(bins)

    # Bin each stop departure time
    def F(x):
        return (utils.timestr_to_seconds(x)//60) % (24*60)

    f['departure_index'] = f['departure_time'].map(F)

    # Create one time series for each stop
    series_by_stop = {stop: [0 for i in range(num_bins)] 
      for stop in stops} 

    for stop, group in f.groupby('stop_id'):
        counts = Counter((bin, 0) for bin in bins) +\
          Counter(group['departure_index'].values)
        series_by_stop[stop] = [counts[bin] for bin in bins]

    # Combine lists into one time series.
    # Actually, a dictionary indicator -> time series.
    # Only one indicator in this case, but could add more
    # in the future as was done with routes time series.
    rng = pd.date_range(date_label, periods=24*60, freq='Min')
    series_by_indicator = {'num_trips':
      pd.DataFrame(series_by_stop, index=rng).fillna(0)}

    # Combine all time series into one time series
    g = combine_time_series(series_by_indicator, kind='stop',
      split_directions=split_directions)
    return downsample(g, freq=freq)

def compute_stops_time_series(feed, date, split_directions=False,
  freq='5Min'):
    """
    Call ``compute_stops_times_series_base()`` with the subset of trips 
    active on the given date and with the keyword arguments
    ``split_directions``and ``freq`` and with ``date_label`` equal to ``date``.
    See ``compute_stops_time_series_base()`` for a description of the output.

    NOTES:

    This is a more user-friendly version of 
    ``compute_stops_time_series_base()``.
    The latter function works without a feed, though.
    """  
    return compute_stops_time_series(feed.stop_times, feed.get_trips(date),
      split_directions=split_directions, freq=freq, date_label=date)

def get_stop_timetable(feed, stop_id, date):
    """
    Return a  data frame encoding the timetable
    for the given stop ID on the given date.
    The columns are all those in ``feed.trips`` plus those in
    ``feed.stop_times``.
    The result is sorted by departure time.
    """
    f = feed.get_stop_times(date)
    f = f.merge(feed.trips)
    f = f[f['stop_id'] == stop_id]
    return f.sort_values('departure_time')

def get_stops_in_stations(feed):
    """
    If this feed has station data, that is, ``location_type`` and
    ``parent_station`` columns in ``feed.stops``, then return a 
    data frame that has the same columns as ``feed.stops``
    but only includes stops with parent stations, that is, stops with
    location type 0 or blank and nonblank parent station.
    Otherwise, return an empty data frame with the specified columns.
    """
    f = feed.stops
    return f[(f['location_type'] != 1) & (f['parent_station'].notnull())]

def compute_stations_stats(feed, date, split_directions=False,
    headway_start_time='07:00:00', headway_end_time='19:00:00'):
    """
    If this feed has station data, that is, ``location_type`` and
    ``parent_station`` columns in ``feed.stops``, then compute
    the same stats that ``feed.compute_stops_stats()`` does, but for
    stations.
    Otherwise, return an empty data frame with the specified columns.
    """
    # Get stop times of active trips that visit stops in stations
    sis = feed.get_stops_in_stations()
    if sis.empty:
        return sis

    f = feed.get_stop_times(date)
    f = f.merge(sis)

    # Convert departure times to seconds to ease headway calculations
    f['departure_time'] = f['departure_time'].map(utils.timestr_to_seconds)

    headway_start = utils.timestr_to_seconds(headway_start_time)
    headway_end = utils.timestr_to_seconds(headway_end_time)

    # Compute stats for each station
    def compute_station_stats(group):
        # Operate on the group of all stop times for an individual stop
        d = OrderedDict()
        d['num_trips'] = group.shape[0]
        d['start_time'] = group['departure_time'].min()
        d['end_time'] = group['departure_time'].max()
        headways = []
        dtimes = sorted([dtime for dtime in group['departure_time'].values
          if headway_start <= dtime <= headway_end])
        headways.extend([dtimes[i + 1] - dtimes[i] 
          for i in range(len(dtimes) - 1)])
        if headways:
            d['max_headway'] = np.max(headways)/60
            d['mean_headway'] = np.mean(headways)/60
        else:
            d['max_headway'] = np.nan
            d['mean_headway'] = np.nan
        return pd.Series(d)

    if split_directions:
        g = f.groupby(['parent_station', 'direction_id'])
    else:
        g = f.groupby('parent_station')

    result = g.apply(compute_station_stats).reset_index()

    # Convert start and end times to time strings
    result[['start_time', 'end_time']] =\
      result[['start_time', 'end_time']].applymap(
      lambda x: utils.timestr_to_seconds(x, inverse=True))

    return result

# -------------------------------------
# Functions about shapes
# -------------------------------------
def build_geometry_by_shape(feed, use_utm=True):
    """
    Return a dictionary with structure
    shape_id -> Shapely linestring of shape.
    If ``feed.shapes is None``, then return ``None``.
    If ``use_utm == True``, then return each linestring in
    in UTM coordinates.
    Otherwise, return each linestring in WGS84 longitude-latitude
    coordinates.
    """
    if feed.shapes is None:
        return

    # Note the output for conversion to UTM with the utm package:
    # >>> u = utm.from_latlon(47.9941214, 7.8509671)
    # >>> print u
    # (414278, 5316285, 32, 'T')
    geometry_by_shape = {}
    if use_utm:
        for shape, group in feed.shapes.groupby('shape_id'):
            lons = group['shape_pt_lon'].values
            lats = group['shape_pt_lat'].values
            xys = [utm.from_latlon(lat, lon)[:2] 
              for lat, lon in zip(lats, lons)]
            geometry_by_shape[shape] = LineString(xys)
    else:
        for shape, group in feed.shapes.groupby('shape_id'):
            lons = group['shape_pt_lon'].values
            lats = group['shape_pt_lat'].values
            lonlats = zip(lons, lats)
            geometry_by_shape[shape] = LineString(lonlats)
    return geometry_by_shape

def build_shapes_geojson(feed):
    """
    Return a string that is a GeoJSON feature collection of 
    linestring features representing ``feed.shapes``.
    Each feature will have a ``shape_id`` property. 
    If ``feed.shapes`` is ``None``, then return ``None``.
    The coordinates reference system is the default one for GeoJSON,
    namely WGS84.
    """

    geometry_by_shape = feed.build_geometry_by_shape(use_utm=False)
    if geometry_by_shape is None:
        return

    d = {
      'type': 'FeatureCollection', 
      'features': [{
        'properties': {'shape_id': shape},
        'type': 'Feature',
        'geometry': mapping(linestring),
        }
        for shape, linestring in geometry_by_shape.items()]
      }
    return json.dumps(d)

def add_dist_to_shapes(feed):
    """
    Add/overwrite the optional ``shape_dist_traveled`` GTFS field for
    ``feed.shapes``.
    Return ``None``.

    NOTE: 

    All of the calculated ``shape_dist_traveled`` values 
    for the Portland feed differ by at most 0.016 km in absolute values
    from of the original values. 
    """
    if feed.shapes is None:
        raise ValueError(
          "This function requires the feed to have a shapes.txt file")

    f = feed.shapes
    m_to_dist = utils.get_convert_dist('m', feed.dist_units_out)

    def compute_dist(group):
        # Compute the distances of the stops along this trip
        group = group.sort_values('shape_pt_sequence')
        shape = group['shape_id'].iat[0]
        if not isinstance(shape, str):
            print(trip, 'no shape_id:', shape)
            group['shape_dist_traveled'] = np.nan 
            return group
        points = [Point(utm.from_latlon(lat, lon)[:2]) 
          for lon, lat in group[['shape_pt_lon', 'shape_pt_lat']].values]
        p_prev = points[0]
        d = 0
        distances = [0]
        for  p in points[1:]:
            d += p.distance(p_prev)
            distances.append(d)
            p_prev = p
        group['shape_dist_traveled'] = distances
        return group

    g = f.groupby('shape_id', group_keys=False).apply(compute_dist)
    # Convert from meters
    g['shape_dist_traveled'] = g['shape_dist_traveled'].map(m_to_dist)
    feed.shapes = g

# -------------------------------------
# Functions about stop times
# -------------------------------------
def get_stop_times(feed, date=None):
    """
    Return the section of ``feed.stop_times`` that contains
    only trips active on the given date.
    If no date is given, then return all stop times.
    """
    f = feed.stop_times.copy()
    if date is None:
        return f

    g = feed.get_trips(date)
    return f[f['trip_id'].isin(g['trip_id'])]

def add_dist_to_stop_times(feed, trips_stats):
    """
    Add/overwrite the optional ``shape_dist_traveled`` GTFS field in
    ``feed.stop_times``.
    Doesn't always give accurate results, as described below.

    ALGORITHM:

    Compute the ``shape_dist_traveled`` field by using Shapely to measure 
    the distance of a stop along its trip linestring.
    If for a given trip this process produces a non-monotonically 
    increasing, hence incorrect, list of (cumulative) distances, then
    fall back to estimating the distances as follows.
    
    Get the average speed of the trip via ``trips_stats`` and
    use is to linearly interpolate distances for stop times, 
    assuming that the first stop is at shape_dist_traveled = 0
    (the start of the shape) and the last stop is 
    at shape_dist_traveled = the length of the trip 
    (taken from trips_stats and equal to the length of the shape,
    unless trips_stats was called with ``get_dist_from_shapes == False``).
    This fallback method usually kicks in on trips with feed-intersecting
    linestrings.
    Unfortunately, this fallback method will produce incorrect results
    when the first stop does not start at the start of its shape
    (so shape_dist_traveled != 0).
    This is the case for several trips in the Portland feed, for example. 
    """
    geometry_by_shape = feed.build_geometry_by_shape()
    geometry_by_stop = feed.build_geometry_by_stop()

    # Initialize data frame
    f = feed.stop_times.merge(
      trips_stats[['trip_id', 'shape_id', 'distance', 'duration']]).\
      sort(['trip_id', 'stop_sequence'])

    # Convert departure times to seconds past midnight to ease calculations
    f['departure_time'] = f['departure_time'].map(utils.timestr_to_seconds)
    dist_by_stop_by_shape = {shape: {} for shape in geometry_by_shape}
    m_to_dist = utils.get_convert_dist('m', feed.dist_units_out)

    def compute_dist(group):
        # Compute the distances of the stops along this trip
        trip = group['trip_id'].iat[0]
        shape = group['shape_id'].iat[0]
        if not isinstance(shape, str):
            print(trip, 'has no shape_id')
            group['shape_dist_traveled'] = np.nan 
            return group
        elif np.isnan(group['distance'].iat[0]):
            group['shape_dist_traveled'] = np.nan 
            return group
        linestring = geometry_by_shape[shape]
        distances = []
        for stop in group['stop_id'].values:
            if stop in dist_by_stop_by_shape[shape]:
                d = dist_by_stop_by_shape[shape][stop]
            else:
                d = m_to_dist(utils.get_segment_length(linestring, 
                  geometry_by_stop[stop]))
                dist_by_stop_by_shape[shape][stop] = d
            distances.append(d)
        s = sorted(distances)
        if s == distances:
            # Good
            pass
        elif s == distances[::-1]:
            # Reverse. This happens when the direction of a linestring
            # opposes the direction of the bus trip.
            distances = distances[::-1]
        else:
            # Totally redo using trip lengths and linear interpolation.
            dt = group['departure_time']
            times = dt.values # seconds
            t0, t1 = times[0], times[-1]                  
            d0, d1 = 0, group['distance'].iat[0]
            # Get indices of nan departure times and 
            # temporarily forward fill them
            # for the purposes of using np.interp smoothly
            nan_indices = np.where(dt.isnull())[0]
            dt.fillna(method='ffill')
            # Interpolate
            distances = np.interp(times, [t0, t1], [d0, d1])
            # Nullify distances with nan departure times
            for i in nan_indices:
                distances[i] = np.nan

        group['shape_dist_traveled'] = distances
        return group

    result = f.groupby('trip_id', group_keys=False).apply(compute_dist)
    # Convert departure times back to time strings
    result['departure_time'] = result['departure_time'].map(lambda x: 
      utils.timestr_to_seconds(x, inverse=True))
    del result['shape_id']
    del result['distance']
    del result['duration']
    feed.stop_times = result

# -------------------------------------
# Functions about feeds
# -------------------------------------
def compute_feed_stats(feed, trips_stats, date):
    """
    Given ``trips_stats``, which is the output of 
    ``feed.compute_trips_stats()`` and a date,
    return a  data frame including the following feed
    stats for the date.

    - num_trips: number of trips active on the given date
    - num_routes: number of routes active on the given date
    - num_stops: number of stops active on the given date
    - peak_num_trips: maximum number of simultaneous trips in service
    - peak_start_time: start time of first longest period during which
      the peak number of trips occurs
    - peak_end_time: end time of first longest period during which
      the peak number of trips occurs
    - service_distance: sum of the service distances for the active routes
    - service_duration: sum of the service durations for the active routes
    - service_speed: service_distance/service_duration

    If there are no stats for the given date, return an empty data frame
    with the specified columns.
    """
    cols = [
      'num_trips',
      'num_routes',
      'num_stops',
      'peak_num_trips',
      'peak_start_time',
      'peak_end_time',
      'service_distance',
      'service_duration',
      'service_speed',
      ]
    d = OrderedDict()
    trips = feed.get_trips(date)
    if trips.empty:
        return pd.DataFrame(columns=cols)

    d['num_trips'] = trips.shape[0]
    d['num_routes'] = feed.get_routes(date).shape[0]
    d['num_stops'] = feed.get_stops(date).shape[0]

    # Compute peak stats
    f = trips.merge(trips_stats)
    f[['start_time', 'end_time']] =\
      f[['start_time', 'end_time']].applymap(utils.timestr_to_seconds)

    times = np.unique(f[['start_time', 'end_time']].values)
    counts = [count_active_trips(f, t) for t in times]
    start, end = utils.get_peak_indices(times, counts)
    d['peak_num_trips'] = counts[start]
    d['peak_start_time'] =\
      utils.timestr_to_seconds(times[start], inverse=True)
    d['peak_end_time'] =\
      utils.timestr_to_seconds(times[end], inverse=True)

    # Compute remaining stats
    d['service_distance'] = f['distance'].sum()
    d['service_duration'] = f['duration'].sum()
    d['service_speed'] = d['service_distance']/d['service_duration']

    return pd.DataFrame(d, index=[0])

def compute_feed_time_series(feed, trips_stats, date, freq='5Min'):
    """
    Given trips stats (output of ``feed.compute_trips_stats()``),
    a date, and a Pandas frequency string,
    return a time series of stats for this feed on the given date
    at the given frequency with the following columns

    - num_trip_starts: number of trips starting at this time
    - num_trips: number of trips in service during this time period
    - service_distance: distance traveled by all active trips during
      this time period
    - service_duration: duration traveled by all active trips during this
      time period
    - service_speed: service_distance/service_duration

    If there is no time series for the given date, 
    return an empty data frame with specified columns.
    """
    cols = [
      'num_trip_starts',
      'num_trips',
      'service_distance',
      'service_duration',
      'service_speed',
      ]
    rts = feed.compute_routes_time_series(trips_stats, date, freq=freq)
    if rts.empty:
        return pd.DataFrame(columns=cols)

    stats = rts.columns.levels[0].tolist()
    # split_directions = 'direction_id' in rts.columns.names
    # if split_directions:
    #     # For each stat and each direction, sum across routes.
    #     frames = []
    #     for stat in stats:
    #         f0 = rts.xs((stat, '0'), level=('indicator', 'direction_id'), 
    #           axis=1).sum(axis=1)
    #         f1 = rts.xs((stat, '1'), level=('indicator', 'direction_id'), 
    #           axis=1).sum(axis=1)
    #         f = pd.concat([f0, f1], axis=1, keys=['0', '1'])
    #         frames.append(f)
    #     F = pd.concat(frames, axis=1, keys=stats, names=['indicator', 
    #       'direction_id'])
    #     # Fix speed
    #     F['service_speed'] = F['service_distance'].divide(
    #       F['service_duration'])
    #     result = F
    f = pd.concat([rts[stat].sum(axis=1) for stat in stats], axis=1, 
      keys=stats)
    f['service_speed'] = f['service_distance']/f['service_duration']
    return f


# -------------------------------------
# Miscellaneous functions
# -------------------------------------
def downsample(time_series, freq):
    """
    Downsample the given route, stop, or feed time series, 
    (outputs of ``Feed.compute_routes_time_series()``, 
    ``Feed.compute_stops_time_series()``, or ``Feed.compute_feed_time_series()``,
    respectively) to the given Pandas frequency.
    Return the given time series unchanged if the given frequency is 
    shorter than the original frequency.
    """    
    # Can't downsample to a shorter frequency
    if time_series.empty or\
      pd.tseries.frequencies.to_offset(freq) < time_series.index.freq:
        return time_series

    result = None
    if 'route_id' in time_series.columns.names:
        # It's a routes time series
        has_multiindex = True
        # Sums
        how = OrderedDict((col, 'sum') for col in time_series.columns
          if col[0] in ['num_trip_starts', 'service_distance', 
          'service_duration'])
        # Means
        how.update(OrderedDict((col, 'mean') for col in time_series.columns
          if col[0] in ['num_trips']))
        f = time_series.resample(freq, how=how)
        # Calculate speed and add it to f. Can't resample it.
        speed = f['service_distance']/f['service_duration']
        speed = pd.concat({'service_speed': speed}, axis=1)
        result = pd.concat([f, speed], axis=1)
    elif 'stop_id' in time_series.columns.names:
        # It's a stops time series
        has_multiindex = True
        how = OrderedDict((col, 'sum') for col in time_series.columns)
        result = time_series.resample(freq, how=how)
    else:
        # It's a feed time series
        has_multiindex = False
        # Sums
        how = OrderedDict((col, 'sum') for col in time_series.columns
          if col in ['num_trip_starts', 'service_distance', 
          'service_duration'])
        # Means
        how.update(OrderedDict((col, 'mean') for col in time_series.columns
          if col in ['num_trips']))
        f = time_series.resample(freq, how=how)
        # Calculate speed and add it to f. Can't resample it.
        speed = f['service_distance']/f['service_duration']
        speed = pd.concat({'service_speed': speed}, axis=1)
        result = pd.concat([f, speed], axis=1)

    # Reset column names in result, because they disappear after resampling.
    # Pandas 0.14.0 bug?
    result.columns.names = time_series.columns.names
    # Sort the multiindex column to make slicing possible;
    # see http://pandas.pydata.org/pandas-docs/stable/indexing.html#multiindexing-using-slicers
    if has_multiindex:
        result = result.sortlevel(axis=1)
    return result

def combine_time_series(time_series_dict, kind, split_directions=False):
    """
    Given a dictionary of time series data frames, combine the time series
    into one time series data frame with multi-index (hierarchical) columns
    and return the result.
    The top level columns are the keys of the dictionary and
    the second and third level columns are 'route_id' and 'direction_id',
    if ``kind == 'route'``, or 'stop_id' and 'direction_id', 
    if ``kind == 'stop'``.
    If ``split_directions == False``, then there is no third column level,
    no 'direction_id' column.
    """
    if kind not in ['stop', 'route']:
        raise ValueError(
          "kind must be 'stop' or 'route'")

    subcolumns = ['indicator']
    if kind == 'stop':
        subcolumns.append('stop_id')
    else:
        subcolumns.append('route_id')

    if split_directions:
        subcolumns.append('direction_id')

    def process_index(k):
        return tuple(k.rsplit('-', 1))

    frames = list(time_series_dict.values())
    new_frames = []
    if split_directions:
        for f in frames:
            ft = f.T
            ft.index = pd.MultiIndex.from_tuples([process_index(k) 
              for (k, v) in ft.iterrows()])
            new_frames.append(ft.T)
    else:
        new_frames = frames
    return pd.concat(new_frames, axis=1, keys=list(time_series_dict.keys()),
      names=subcolumns)

def plot_headways(stats, max_headway_limit=60):
    """
    Given a stops or routes stats data frame, 
    return bar charts of the max and mean headways as a MatplotLib figure.
    Only include the stops/routes with max headways at most 
    ``max_headway_limit`` minutes.
    If ``max_headway_limit is None``, then include them all in a giant plot. 
    If there are no stops/routes within the max headway limit, then return 
    ``None``.

    NOTES:

    Take the resulting figure ``f`` and do ``f.tight_layout()``
    for a nice-looking plot.
    """
    import matplotlib.pyplot as plt

    # Set Pandas plot style
    pd.options.display.mpl_style = 'default'

    if 'stop_id' in stats.columns:
        index = 'stop_id'
    elif 'route_id' in stats.columns:
        index = 'route_id'
    split_directions = 'direction_id' in stats.columns
    if split_directions:
        # Move the direction_id column to a hierarchical column,
        # select the headway columns, and convert from seconds to minutes
        f = stats.pivot(index=index, columns='direction_id')[['max_headway', 
          'mean_headway']]
        # Only take the stops/routes within the max headway limit
        if max_headway_limit is not None:
            f = f[(f[('max_headway', 0)] <= max_headway_limit) |
              (f[('max_headway', 1)] <= max_headway_limit)]
        # Sort by max headway
        f = f.sort_values(columns=[('max_headway', 0)], ascending=False)
    else:
        f = stats.set_index(index)[['max_headway', 'mean_headway']]
        if max_headway_limit is not None:
            f = f[f['max_headway'] <= max_headway_limit]
        f = f.sort_values(columns=['max_headway'], ascending=False)
    if f.empty:
        return

    # Plot max and mean headway separately
    n = f.shape[0]
    data_frames = [f['max_headway'], f['mean_headway']]
    titles = ['Max Headway','Mean Headway']
    ylabels = [index, index]
    xlabels = ['minutes', 'minutes']
    fig, axes = plt.subplots(nrows=1, ncols=2)
    for (i, f) in enumerate(data_frames):
        f.plot(kind='barh', ax=axes[i], figsize=(10, max(n/9, 10)))
        axes[i].set_title(titles[i])
        axes[i].set_xlabel(xlabels[i])
        axes[i].set_ylabel(ylabels[i])
    return fig

def plot_routes_time_series(routes_time_series):
    """
    Given a routes time series data frame,
    sum each time series indicator over all routes, 
    plot each series indicator using MatplotLib, 
    and return the resulting figure of subplots.

    NOTES:

    Take the resulting figure ``f`` and do ``f.tight_layout()``
    for a nice-looking plot.
    """
    import matplotlib.pyplot as plt

    rts = routes_time_series
    if 'route_id' not in rts.columns.names:
        return

    # Aggregate time series
    f = compute_feed_time_series(rts)

    # Reformat time periods
    f.index = [t.time().strftime('%H:%M') 
      for t in rts.index.to_datetime()]
    
    #split_directions = 'direction_id' in rts.columns.names

    # Split time series by into its component time series by indicator type
    # stats = rts.columns.levels[0].tolist()
    stats = [
      'num_trip_starts',
      'num_trips',
      'service_distance',
      'service_duration',
      'service_speed',
      ]
    ts_dict = {stat: f[stat] for stat in stats}

    # Create plots  
    pd.options.display.mpl_style = 'default'
    titles = [stat.capitalize().replace('_', ' ') for stat in stats]
    units = ['','','km','h', 'kph']
    alpha = 1
    fig, axes = plt.subplots(nrows=len(stats), ncols=1)
    for (i, stat) in enumerate(stats):
        if stat == 'service_speed':
            stacked = False
        else:
            stacked = True
        ts_dict[stat].plot(ax=axes[i], alpha=alpha, 
          kind='bar', figsize=(8, 10), stacked=stacked, width=1)
        axes[i].set_title(titles[i])
        axes[i].set_ylabel(units[i])

    return fig