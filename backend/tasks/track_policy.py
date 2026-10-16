"""Keep legacy definitions for recovery; task workers cannot customize them."""
import re


def is_legacy_track_mutation(path, method):
    if method in {'GET', 'HEAD', 'OPTIONS'}:
        return False
    return bool(re.fullmatch(r'/api/account/(?:track-library|track-templates|fixed-track-templates)(?:/.*)?', path)
                or re.fullmatch(r'/api/projects/[^/]+/track-selection', path))
