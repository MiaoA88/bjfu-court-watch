"""Observe availability separately from acknowledging notification delivery."""
from datetime import datetime

from .matcher import Hit, Slot
from .state import State


def pick_fresh(hits: list[Hit], all_slots: list[Slot], state: State,
               hall_id: str | int, moment: datetime) -> tuple[list[Hit], int]:
    released = 0
    for slot in all_slots:
        key = slot.key(hall_id)
        if slot.available:
            if state.was_occupied(key):
                released += 1
                state.pending[key] = moment.isoformat(timespec="seconds")
            if not state.already_notified(key):
                state.pending.setdefault(key, moment.isoformat(timespec="seconds"))
            state.mark_free(key)
        else:
            state.mark_occupied(key, moment)
            # Retry only opportunities which are still available.
            state.pending.pop(key, None)
    return [hit for hit in hits if any(s.key(hall_id) in state.pending for s in hit.slots)], released
