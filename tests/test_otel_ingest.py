from usagemax_display.otel_ingest import parse_otlp_json


def test_parse_otlp_logs_to_compact_records() -> None:
    records = parse_otlp_json(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [{"key": "service.name", "value": {"stringValue": "example"}}]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "example.scope"},
                            "logRecords": [
                                {
                                    "timeUnixNano": "1767323045000000000",
                                    "severityText": "INFO",
                                    "body": {"stringValue": "tool.completed"},
                                    "attributes": [],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )

    assert len(records) == 1
    assert records[0].service == "example"
    assert records[0].name == "tool.completed"
    assert records[0].kind == "LOG"
