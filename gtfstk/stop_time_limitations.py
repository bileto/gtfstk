"""
Functions about stop external_ids.
"""

def get_stop_time_limitations(feed):
    """
    Return the section of ``feed.stop_time_limitations``

    Assume the following feed attributes are not ``None``:

    - ``feed.stop_time_limitations``

    """
    f = feed.stop_time_limitations.copy()
    return f
