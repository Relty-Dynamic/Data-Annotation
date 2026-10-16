"""Apply the editor's camera seam rules to frozen task material coordinates."""
from datetime import datetime

from fastapi import HTTPException

from ..service import MAX_CAMERA_ALIGNMENT_MS, parse_recording_start, recording_layout


def collection_recording_layout(collection):
    videos = []
    for member in collection['videos']:
        video = {**member, 'name': member.get('name', member.get('video_name', ''))}
        if 'alignment_offset_ms' not in video:
            # Older task snapshots retained corrected recording times but not
            # the offset. Recover that evidence instead of assuming zero.
            try:
                actual = datetime.fromisoformat(video['recording_start'])
                nominal = parse_recording_start(video['name'])
                video['alignment_offset_ms'] = round((actual - nominal).total_seconds() * 1000)
            except (HTTPException, KeyError, TypeError, ValueError, OverflowError):
                video['alignment_offset_ms'] = MAX_CAMERA_ALIGNMENT_MS + 1
        videos.append(video)
    return recording_layout(videos)


def with_recording_layout(task):
    collection = task.get('collection')
    if not collection:
        return task
    runs, bridges, gaps = collection_recording_layout(collection)
    return {**task, 'collection': {**collection, 'recording_runs': runs,
                                  'continuity_bridges': bridges, 'gaps': gaps}}
