"""Filename must end in `presenters.py`: the no-model-dump rule is path-scoped to presenter modules."""


def to_response(row):
    return row.model_dump()
