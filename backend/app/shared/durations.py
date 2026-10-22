"""Durations in the operator's words: reconcile's tables and the operator alerts."""


def describe_seconds(seconds: float) -> str:
    if seconds < 120:
        return f"{int(seconds)} s"
    if seconds < 7_200:
        return f"{int(seconds // 60)} min"
    return f"{seconds / 3_600:.1f} h"
