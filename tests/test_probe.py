import json

from src.availability import LIST_PAGE, probe

PAYLOAD = {
    "list": [
        {
            "id": 156,
            "name": "羽毛球综合场馆（收费）",
            "table": {
                "595": [
                    {
                        "abscissa": "1号塑胶场地",
                        "yaxis": "18:00-19:00",
                        "row": {"status": 1, "total": 1, "margin": 1},
                        "date": "2026-11-03",
                    },
                    {
                        "abscissa": "1号塑胶场地",
                        "yaxis": "19:00-20:00",
                        "row": {"status": 5, "total": 1, "margin": 1},
                        "date": "2026-11-03",
                    },
                ]
            },
        }
    ]
}


class StubClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def data(self, method, path, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_probe_reports_each_day(tmp_path):
    client = StubClient(PAYLOAD)
    results = probe(client, 54, out_dir=tmp_path, days=3, sleep_between=0)

    assert len(results) == 3
    assert all(record["cells"] == 2 for record in results)
    assert all(record["available"] == 1 for record in results), "只有 status=1 的那格算可约"

    first = client.calls[0]
    assert first["path"] == LIST_PAGE
    assert first["params"]["hall_id"] == 54
    assert first["params"]["p"] == 1


def test_probe_saves_only_first_day_payload(tmp_path):
    probe(StubClient(PAYLOAD), 54, out_dir=tmp_path, days=2, sleep_between=0)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == PAYLOAD


def test_probe_records_errors_without_raising(tmp_path):
    results = probe(StubClient(RuntimeError("接口炸了")), 54, out_dir=tmp_path, days=2, sleep_between=0)
    assert len(results) == 2
    assert all("error" in record for record in results)
