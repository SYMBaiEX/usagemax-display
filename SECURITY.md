# Security policy

## Reporting a vulnerability

Please do not report security vulnerabilities in a public issue. Use the
repository host's private vulnerability reporting feature, if available. If it
is unavailable, contact the project maintainers privately through the repository
hosting platform and ask for a secure reporting channel. Do not include secrets,
customer data, or full telemetry payloads in an initial report.

Include the affected version or commit, the conditions needed to reproduce the
issue, its practical impact, and a minimal proof of concept. Maintainers will
acknowledge reports when feasible and coordinate a fix and disclosure with the
reporter.

## Supported versions

Until a first release is published, security fixes are handled on the default
branch. Once releases are available, this policy will list supported versions.

## Deployment notes

The OTLP/HTTP receiver has no authentication or TLS. Keep it bound to loopback
unless a deployment intentionally exposes it behind appropriate network access
controls. Do not put credentials in display configuration, JSONL input, logs, or
bug reports.
