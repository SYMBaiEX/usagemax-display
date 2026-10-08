# UsageMax Display

UsageMax Display is a Python runtime for building custom dashboards for small
displays. It includes a built-in USB transport for the Trofeo device identified
as `0416:5408`, and is designed to let third parties add transports and renderers
through Python entry-point plugins. Generic telemetry can be read from documented
JSON Lines files or received as OTLP.

The project can be installed from this checkout. A PyPI release and
platform-specific USB support have not been verified.

## Platform support

Preview rendering is intended to work on macOS, Linux, and Windows. Physical USB
operation depends on host libraries, device permissions, and drivers. The built-in
Trofeo transport has not been validated on every operating system; check the
platform notes before relying on USB output. Third-party transports can provide
support for other hardware.

| Capability | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Preview mode | Intended | Intended | Intended |
| Trofeo USB transport (`0416:5408`) | Needs validation | Needs validation | Needs validation |

No character artwork is included. Asset redistribution terms for the original
display project were not established. Contributors can provide their own
artwork under terms they are authorized to share.

## Install from a checkout

Use Python 3.10 or newer and install the project:

```sh
python -m pip install .
```

Once installed, the intended CLI examples are:

```sh
# Render a preview without connecting to a display.
usagemax-display --preview

# Start the runtime and send frames through the built-in Trofeo USB transport.
# macOS/Linux
cp config.example.json config.json
# Windows PowerShell: Copy-Item config.example.json config.json
python -m pip install ".[usb]"
usagemax-display --config config.json --transport trofeo
```

USB operation requires the device to be connected and its OS-specific driver and
permissions configured. Preview mode does not establish physical USB support.
The exact hardware path remains subject to platform validation.

## Telemetry inputs and privacy

The runtime supports generic telemetry from documented JSON Lines inputs and
OTLP. Local Codex and Claude session discovery is disabled by default; enable
`local_session_sources` only after reviewing which local sources you want the
display to read. Keep input files local and configure only the sources you
intend to display. Do not commit real telemetry, credentials, or customer data.

The optional OTLP/HTTP receiver is disabled by default and binds to loopback
(`127.0.0.1`) when enabled. It has no authentication or TLS. If you explicitly
bind it to a network interface, restrict access with host firewall rules and a
trusted network boundary; anyone who can reach it may submit telemetry to the
receiver.

To accept local OTLP/HTTP requests, set `otel_http_enabled` to `true` in the
config and keep `otel_http_bind` at `127.0.0.1`. File-based OTLP input can be
configured separately with `otel_files`; those relative paths are resolved under
the user data directory.

The optional UsageMax profile panel is disabled by default. When enabled, it
reads the configured public profile over the selected HTTP(S) endpoint and does
not require an API key. Local telemetry is not uploaded by this integration.

## Extending the runtime

Third-party packages can add transport or renderer implementations through the
documented Python entry-point groups. See [the plugin guide](docs/plugin-development.md)
for group names, contracts, and a minimal package example. A plugin should
declare its supported operating systems and hardware, validate configuration,
and document how it handles telemetry and credentials.

The built-in Trofeo protocol observations are summarized in
[the hardware protocol notes](docs/trofeo-protocol.md).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Please
report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Project status

This repository is under active development. CI is configured for macOS, Linux,
and Windows. The project has no release or PyPI publication, and cross-platform
USB operation still requires physical-device validation.
