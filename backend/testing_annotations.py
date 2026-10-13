"""Explicit full-coverage fixtures for export tests; never used by application code."""
from backend.service import recording_layout, validate_annotations, track_labels


def complete_annotations(project, annotations=None):
    choices = track_labels(project)
    result = validate_annotations(annotations if annotations is not None else project["annotations"],
                                  project["duration_ms"], videos=project.get("videos"),
                                  custom_tracks=project.get("custom_tracks", []), fixed_label_options=choices)
    runs = recording_layout(project["videos"])[0] if "videos" in project else [{"start_ms": 0, "end_ms": project["duration_ms"]}]
    enabled = project.get("fixed_tracks", ["scene", "posture", "category", "habit"])
    for axis, label in (("scene", "室外"), ("posture", "站"), ("category", "其他")):
        if axis not in enabled:
            continue
        if label not in choices[axis]:
            label = choices[axis][-1]
        records = list(result[axis])
        gaps = []
        for run in runs:
            cursor = run["start_ms"]
            for record in records:
                if record["end_ms"] <= cursor or record["start_ms"] >= run["end_ms"]:
                    continue
                if cursor < record["start_ms"]:
                    gaps.append((cursor, record["start_ms"]))
                cursor = max(cursor, record["end_ms"])
            if cursor < run["end_ms"]:
                gaps.append((cursor, run["end_ms"]))
        for index, (start, end) in enumerate(gaps):
            result[axis].append({"id": f"fixture-fill-{axis}-{index}", "label": label,
                                 "kind": "interval", "start_ms": start, "end_ms": end,
                                 **({"mode": "state"} if axis == "category" else {})})
    return validate_annotations(result, project["duration_ms"], videos=project.get("videos"),
                                custom_tracks=project.get("custom_tracks", []), fixed_label_options=choices)


def complete_project(project):
    return {**project, "annotations": complete_annotations(project)}
