GTFS Toolkit
============
This is a set of Python 3.4 tools for processing `General Transit Feed Specification (GTFS) <https://en.wikipedia.org/wiki/GTFS>`_ data in memory without a database.
Currently, it's mostly for computing network-level statistics, such as mean daily service distance per route and mean daily number of vehicles per stop.
It uses Pandas and Shapely to do the heavy lifting.

Warning
--------
Currently, this package is experimental and needs more testing.
Use it at your own risk.

Installation
-------------
``pip install gtfs-toolkit``

Examples
--------
Play with ``examples/gtfs_toolkit_examples.ipynb`` in an iPython notebook

Todo
----
- Add more tests