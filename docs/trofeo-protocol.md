# Trofeo Vision 9.16 USB transport

The built-in transport targets the USB display identified as vendor/product
`0416:5408`. It is a USB framebuffer, not a regular HDMI/DisplayPort monitor.
The implementation is based on observed traffic and is not an official vendor
SDK.

## Frame path

The renderer creates a 1920×462 image, converts it to JPEG, and sends the bytes
to the display over bulk USB endpoints. The current transport uses endpoint
`0x09` for output and `0x81` for acknowledgements. The observed wire format is:

- each logical transfer is 512 bytes;
- each packet has a 16-byte header and up to 496 JPEG payload bytes;
- the header contains the marker, complete JPEG byte length, payload length,
  logical chunk count, and zero-based chunk index;
- transfers are padded to a multiple of four 512-byte packets;
- one acknowledgement is read after the full frame transfer.

The handshake and packet details are implemented in the Python PyUSB sink and
the optional Rust `rusb` helper under `native/trofeo-pump`. The Rust helper
builds for the current host and architecture; it does not cross-compile or
prove the USB driver is usable.

## Platform setup

The application and `--preview` path are intended to run on macOS, Linux, and
Windows. The hardware transport requires a working USB backend and permission
to claim the device interface. Windows needs a compatible WinUSB binding;
Linux needs libusb and an appropriate user-scoped udev permission rule; macOS
needs a libusb backend and exclusive interface access. Those configurations
must be validated on the target host and device before relying on USB output.

This document records protocol observations, not a guarantee that every panel
revision, firmware, driver, or OS combination is compatible.
