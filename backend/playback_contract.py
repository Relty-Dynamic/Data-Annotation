"""S3 upload cache versions; the NAS/local cache contract stays unchanged."""
LEGACY_PROFILE = 'playback-480x270-256k32k-v1'
SINGLE_VIDEO_PROFILE = 's3-playback-single-video-v1'
SINGLE_VIDEO_CAPABILITY = 's3-single-video-cache-v1'


def asset_kinds(profile):
    if profile == SINGLE_VIDEO_PROFILE:
        return ('normal', 'thumbnail')
    if profile == LEGACY_PROFILE:
        return ('normal', 'fast', 'thumbnail')
    raise ValueError('Unknown playback cache profile')


def source_profile(source):
    return source.get('r2_playback', {}).get('manifest', {}).get('cache_profile', LEGACY_PROFILE)
