# User scoped installs and services

These examples run the installed `usagemax-display` command as the logged-in
user. They do not request administrator/root privileges. Install the CLI and
create a private config before enabling a service. Replace every placeholder
path with the actual path on the target machine.

The service command is always:

```text
usagemax-display --config <path-to-your-config.json>
```

The config should set `native_transport` to the user-local helper path if you
build the optional Rust transport. Keep config, telemetry, state, and logs in
user-owned directories. Do not commit machine-specific config or credentials.

## macOS

Copy `macos/com.usagemax.display.plist` to
`~/Library/LaunchAgents/com.usagemax.display.plist`. Replace every
`__HOME_DIRECTORY__` token with your absolute home path and edit the executable,
config, working-directory, and log paths. launchd does not expand `~` or shell variables inside
`ProgramArguments`.

Load and unload it from the logged-in user's LaunchAgents domain:

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.usagemax.display.plist"
launchctl bootout "gui/$(id -u)/com.usagemax.display"
```

The service is per-user and starts at login. To roll back, boot it out and
remove the copied plist. Inspect the configured log files if it exits early.

### USB support

The renderer preview path can run without the display attached. Physical USB
support has not been validated across macOS versions or hardware. PyUSB needs a
working libusb backend, and another driver or process may prevent claiming the
panel interface. Verify device discovery, interface claim, handshake, frame
acknowledgement, and reconnect behavior on the target Mac before relying on the
service.

## Linux

Copy `linux/usagemax-display.service` to
`~/.config/systemd/user/usagemax-display.service`. Edit `ExecStart` if the CLI
is not installed at `~/.local/bin/usagemax-display`, and edit `--config` to
point to your user-owned JSON file.

Enable or disable the user service:

```sh
systemctl --user daemon-reload
systemctl --user enable --now usagemax-display.service
systemctl --user status usagemax-display.service
systemctl --user disable --now usagemax-display.service
```

The unit runs only in the current user's session. Keep it that way; do not run
the renderer as root to work around USB permissions. PyUSB needs libusb and a
udev rule that grants the logged-in user access to this device's vendor/product
ID (currently `0416:5408`). Rule syntax and group policy vary by distribution.
Install a narrowly scoped rule from your distribution/vendor guidance, reload
udev, reconnect the panel, then verify access as the same user. The project does
not yet ship or validate a distribution-specific udev rule.

Renderer preview support is separate from physical USB support. A successful
preview does not establish libusb discovery, udev permissions, endpoint access,
or frame delivery on any Linux distribution.

## Windows

Run `windows/Register-UsageMaxDisplayTask.ps1` from PowerShell as the intended
user, passing the full path to the installed `usagemax-display.exe` and your
config file. The script creates a Task Scheduler task triggered at that user's
logon, with limited privileges. It does not install drivers or elevate.

To remove the task, run this as the same user:

```powershell
Unregister-ScheduledTask -TaskName UsageMaxDisplay -Confirm:$false
```

Physical support for this panel currently requires a WinUSB-compatible driver
binding for its USB interface. Driver installation may require an administrator
and can affect other software using the device; follow the device/vendor
instructions and verify the binding before starting the task. Preview mode does
not access USB and is not evidence that WinUSB, interface claiming, the LY
handshake, or frame delivery works on a Windows machine.

## Optional native Rust transport

`../native/trofeo-pump` is an optional host-native helper for the existing LY
transport. The Python renderer retains a PyUSB fallback. Build it on each target
OS and architecture with the matching script:

- macOS/Linux: `sh packaging/build-native-transport.sh [output-path]`
- Windows: `packaging/build-native-transport.ps1 [-OutputPath <path>]`

Each script builds into a temporary directory and copies the result to a
user-local destination (or the provided output path); it does not put compiled
binaries in the source tree. Point `native_transport` in the user's config to
that copied executable. `cargo` and a supported Rust toolchain must already be
installed. The scripts do not cross-compile or prove USB support. Successful
compilation only proves that the helper built for that host; physical device
validation remains required.

## Validation boundary

Preview/render support is host-side image generation. USB support adds driver,
permission, device discovery, interface claim, LY handshake, acknowledgement,
and sustained frame delivery. Those are separate acceptance checks. No
macOS/Linux physical-device result is implied by the Windows-oriented source
history or by a successful software preview/build.
