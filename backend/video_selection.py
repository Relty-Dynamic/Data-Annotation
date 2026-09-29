"""Project-local exclusions preserve original source identities and coordinates."""


def active_videos(project: dict) -> list[dict]:
    skipped = project.get('_skipped_video_names', {})
    return [video for video in project['videos'] if video['name'] not in skipped]


def skipped_videos(project: dict) -> list[dict]:
    skipped = project.get('_skipped_video_names', {})
    return [{**{key: video[key] for key in ('id', 'name', 'start_ms', 'end_ms')},
             'reason': skipped[video['name']]}
            for video in project['videos'] if video['name'] in skipped]
