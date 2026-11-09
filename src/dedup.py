"""按独立格子去重，展示合并不参与记账。"""
from .matcher import Hit, Slot, select
from .state import State


def pick_fresh(hits: list[Hit], all_slots: list[Slot], state: State,
               hall_id: str | int) -> list[Hit]:
    for slot in all_slots:
        if not slot.available:
            state.notified.pop(slot.key(hall_id), None)
    # 先按完整空场匹配连续场次要求，再取尚未通知的格子重新合并展示。
    fresh: list[Hit] = []
    for hit in hits:
        slots = [s for s in hit.slots if s.key(hall_id) not in state.notified]
        fresh.extend(select(slots, target_dates=[hit.date], windows=[(hit.start, hit.end)]))
    return sorted(fresh, key=lambda hit: (hit.date, hit.start, hit.resource_name))
