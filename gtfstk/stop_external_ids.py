"""
Functions about stop external_ids.
"""

def get_stop_external_ids(feed):
    """
    Return the section of ``feed.stop_external_ids``

    Assume the following feed attributes are not ``None``:

    - ``feed.stop_external_ids``

    """
    f = feed.stop_external_ids.copy()
    return f
