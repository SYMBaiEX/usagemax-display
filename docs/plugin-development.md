# Display plugin development

UsageMax Display discovers installed plugins with Python package entry points.
Install the plugin package into the same environment as UsageMax Display, then
select its registered name in `config.json` or use the matching CLI option.

There are two plugin groups:

| Group | Contract | Config key |
| --- | --- | --- |
| `usagemax_display.frame_sinks` | `usagemax_display.FrameSink` | `transport` / `--transport` |
| `usagemax_display.renderers` | `usagemax_display.Renderer` | `renderer` / `--renderer` |

Names must be unique within a group. The registered target must be a callable
factory that accepts one mapping-like configuration value and returns an
implementation of the relevant protocol. Avoid opening hardware or starting
threads in the factory; the runtime calls `connect()` only when it is ready to
send frames and always calls `close()` during shutdown.

## Frame sink contract

A frame sink implements `connect()`, `send(frame: bytes)`, and `close()`. The
host passes a JPEG-encoded frame to `send`. It also reads these properties:

- `connected`: whether a display is currently available;
- `mode`: a short status string shown in diagnostics;
- `detail`: a concise description shown in the dashboard;
- `rotation`: `0` or `180`, applied before JPEG encoding.

Example package metadata:

```toml
[project.entry-points."usagemax_display.frame_sinks"]
my-panel = "my_panel:make_sink"
```

```python
class MyPanel:
    def __init__(self, config):
        self.config = config
        self.connected = False
        self.mode = "MY-PANEL"
        self.detail = "not connected"
        self.rotation = 0

    def connect(self):
        # Open the device and set `connected` / `detail`.
        ...

    def send(self, frame: bytes):
        # Transfer one complete JPEG frame.
        ...

    def close(self):
        # Release the device; safe to call more than once.
        self.connected = False


def make_sink(config):
    return MyPanel(config)
```

Select it with `"transport": "my-panel"` in the config, or run
`usagemax-display --transport my-panel`. A sink should recover from transient
device errors where possible and must not log frame contents or telemetry.

## Renderer contract

A renderer exposes positive integer `width` and `height` values, a
`render(snapshot, now)` method that returns a Pillow image, and `close()`. The
host applies configured panel protection, rotates the image if requested by the
sink, encodes JPEG, then sends it through the sink.

Example metadata:

```toml
[project.entry-points."usagemax_display.renderers"]
my-layout = "my_layout:make_renderer"
```

`snapshot` is a duck-typed object. Its initial fields include:

- `agents`: rows with `name`, `state`, `detail`, `events`, `errors`, `model`,
  `token_rate`, and `session_tokens`;
- `events`: rows with `timestamp`, `source`, `message`, and `level`;
- `stats`: host metrics such as `cpu`, `ram`, `gpu`, and `gpu_temp`;
- `live_usages`: normalized session counters and timestamps;
- `agent_relations`: observed parent/child session links;
- `otel_records`: compact normalized OTLP records;
- `token_stats`, `usagemax_stats`, and `daily_usages`: optional account/local
  usage summaries.

These fields are currently stable for the 0.x plugin API but may gain fields.
Plugins should read only the values they use and tolerate missing optional
values. The built-in renderer currently uses a 1920×462 layout; a custom
renderer can choose its own dimensions.

## Telemetry adapters

Third-party harnesses can publish normalized JSON Lines without writing a
Python plugin. Configure a source name and path in `telemetry_files`; relative
paths resolve beside the config file. Each usage line uses cumulative counters:

```json
{"kind":"usage","timestamp":"2026-01-02T03:04:05Z","harness":"My runtime","provider":"Example","model":"Example model","session_id":"session-001","session_tokens":12000,"input_tokens":9000,"output_tokens":3000}
```

Real delegation is represented by a separate topology line:

```json
{"kind":"topology","timestamp":"2026-01-02T03:04:05Z","parent_id":"session-001","child_id":"session-002","parent_name":"Coordinator","child_name":"Research","parent_harness":"My runtime","child_harness":"My runtime","parent_model":"Example model","child_model":"Example model","status":"OPEN"}
```

Use full stable session IDs. The runtime derives rates from successive
cumulative counters and drops stale activity; it does not infer delegation
from event logs. OTLP file inputs and the optional OTLP/HTTP receiver support
event/span summaries separately from session usage.

## Support expectations

Each plugin package should document its hardware and OS support, permissions,
configuration, error behavior, and test evidence. Preview rendering validates
only the host-side image; it does not validate device discovery, driver access,
frame transfer, or sustained operation.
