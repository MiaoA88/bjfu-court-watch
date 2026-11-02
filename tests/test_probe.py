import json
from datetime import date

from src.availability import probe
from src.ehall import Envelope


class StubClient:
    """替身：不联网，直接返回固定信封。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        return Envelope(code="0", message="", data=self.payload)

    def data(self, method, path, **kwargs):
        return self.payload


PAYLOAD = [
    {
        "resourceId": "7",
        "resourceName": "7号场",
        "date": "2026-11-05",
        "startTime": "18:00",
        "endTime": "19:00",
        "surplus": 2,
    }
]


def test_probe_walks_every_candidate_and_saves_payloads(tmp_path):
    client = StubClient(PAYLOAD)
    summary = probe(client, 54, out_dir=tmp_path, day=date(2026, 11, 5), sleep_between=0)

    assert len(summary) == 36  # 6 个端点 × 6 组参数
    assert all(entry.get("slots") == 1 for entry in summary)
    assert client.calls[0]["path"] == "/site/reservation/resource-info-margin"

    saved = sorted(tmp_path.glob("*.json"))
    assert saved, "应当把响应落盘"
    assert json.loads(saved[0].read_text(encoding="utf-8")) == PAYLOAD


def test_probe_records_errors_without_raising(tmp_path):
    class Boom(StubClient):
        def request(self, method, path, **kwargs):
            raise RuntimeError("接口炸了")

    summary = probe(Boom([]), 54, out_dir=tmp_path, day=date(2026, 11, 5), sleep_between=0)
    assert len(summary) == 36
    assert all("error" in entry for entry in summary)
