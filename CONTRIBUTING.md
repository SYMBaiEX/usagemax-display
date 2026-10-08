# Contributing

Thanks for helping improve UsageMax Display. Start with a focused issue or pull
request and keep changes easy to review.

## Development setup

Use Python 3.10 or newer. From a checkout, create and activate a virtual
environment, then install the project with its development dependencies:

```sh
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Run the checks relevant to your change before opening a pull request:

```sh
python -m pytest
```

Include the exact checks you ran and their results. If a check needs physical
hardware or a platform that you do not have, say so; do not describe preview
results as USB validation.

## Changes and plugins

- Keep pull requests focused and explain the user need they address.
- For telemetry changes, describe the input shape, privacy implications, and
  whether values can contain personal or customer data.
- For a transport or renderer plugin, follow the published entry-point contract,
  validate plugin configuration, and state which operating systems and devices
  you verified.
- Do not add credentials, private hostnames or paths, raw telemetry, generated
  previews, build output, or assets without confirmed redistribution rights.
- Keep examples synthetic and use loopback addresses for local receiver examples.

## Pull requests

Use the pull-request template. Include the behavior changed, verification
performed, supported platforms tested, and any remaining hardware or environment
checks. Follow the [Code of Conduct](CODE_OF_CONDUCT.md). For suspected security
issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
