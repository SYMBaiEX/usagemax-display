use rusb::{DeviceHandle, GlobalContext};
use std::io::{self, BufReader, BufWriter, Read, Write};
use std::time::{Duration, Instant};

const VID: u16 = 0x0416;
const PID: u16 = 0x5408;
const EP_OUT: u8 = 0x09;
const EP_IN: u8 = 0x81;
const CHUNK_SIZE: usize = 512;
const CHUNK_DATA: usize = 496;
const WRITE_BURST: usize = 4096;
const MAX_JPEG: usize = 320_000;

struct TrofeoPump {
    handle: DeviceHandle<GlobalContext>,
    pm: u8,
    sub: u8,
    frame: Vec<u8>,
    ack: [u8; CHUNK_SIZE],
}

impl TrofeoPump {
    fn connect() -> Result<Self, String> {
        let handle = rusb::open_device_with_vid_pid(VID, PID)
            .ok_or_else(|| "Trofeo 0416:5408 not found".to_string())?;
        let _ = handle.set_auto_detach_kernel_driver(true);
        if handle.active_configuration().unwrap_or(0) != 1 {
            handle
                .set_active_configuration(1)
                .map_err(|error| format!("configuration failed: {error}"))?;
        }
        handle
            .claim_interface(0)
            .map_err(|error| format!("interface claim failed: {error}"))?;

        let mut stale = [0_u8; 64];
        let _ = handle.read_bulk(EP_IN, &mut stale, Duration::from_millis(100));

        let mut request = [0_u8; 2048];
        request[0] = 0x02;
        request[1] = 0xff;
        request[8] = 0x01;
        write_all_bulk(&handle, EP_OUT, &request, Duration::from_secs(2))?;

        let mut response = [0_u8; CHUNK_SIZE];
        let received = handle
            .read_bulk(EP_IN, &mut response, Duration::from_secs(2))
            .map_err(|error| format!("handshake read failed: {error}"))?;
        if received < 37
            || response[0] != 0x03
            || response[1] != 0xff
            || response[8] != 0x01
        {
            return Err("Trofeo handshake rejected".to_string());
        }
        let pm = 64_u8.saturating_add(response[20].max(1));
        let sub = response[22].saturating_add(1);
        Ok(Self {
            handle,
            pm,
            sub,
            frame: Vec::with_capacity(384 * 1024),
            ack: [0_u8; CHUNK_SIZE],
        })
    }

    fn send_frame(&mut self, jpeg: &[u8]) -> Result<Duration, String> {
        if jpeg.is_empty() || jpeg.len() > MAX_JPEG {
            return Err(format!("invalid JPEG length {}", jpeg.len()));
        }
        let started = Instant::now();
        let chunks = jpeg.len() / CHUNK_DATA + 1;
        let padded_chunks = chunks.div_ceil(4) * 4;
        let frame_size = padded_chunks * CHUNK_SIZE;
        self.frame.resize(frame_size, 0);
        self.frame.fill(0);

        for index in 0..chunks {
            let frame_offset = index * CHUNK_SIZE;
            let jpeg_offset = index * CHUNK_DATA;
            let jpeg_end = (jpeg_offset + CHUNK_DATA).min(jpeg.len());
            let part = &jpeg[jpeg_offset..jpeg_end];
            let header = &mut self.frame[frame_offset..frame_offset + 16];
            header[0] = 0x01;
            header[1] = 0xff;
            header[2..6].copy_from_slice(&(jpeg.len() as u32).to_le_bytes());
            header[6..8].copy_from_slice(&(part.len() as u16).to_le_bytes());
            header[8] = 0x01;
            header[9..11].copy_from_slice(&(chunks as u16).to_le_bytes());
            header[11..13].copy_from_slice(&(index as u16).to_le_bytes());
            self.frame[frame_offset + 16..frame_offset + 16 + part.len()]
                .copy_from_slice(part);
        }

        for burst in self.frame.chunks(WRITE_BURST) {
            write_all_bulk(&self.handle, EP_OUT, burst, Duration::from_secs(5))?;
        }
        self.handle
            .read_bulk(EP_IN, &mut self.ack, Duration::from_millis(1500))
            .map_err(|error| format!("frame acknowledgement failed: {error}"))?;
        Ok(started.elapsed())
    }
}

impl Drop for TrofeoPump {
    fn drop(&mut self) {
        let _ = self.handle.release_interface(0);
    }
}

fn write_all_bulk(
    handle: &DeviceHandle<GlobalContext>,
    endpoint: u8,
    mut payload: &[u8],
    timeout: Duration,
) -> Result<(), String> {
    while !payload.is_empty() {
        let written = handle
            .write_bulk(endpoint, payload, timeout)
            .map_err(|error| format!("USB write failed: {error}"))?;
        if written == 0 {
            return Err("USB write made no progress".to_string());
        }
        payload = &payload[written..];
    }
    Ok(())
}

fn run() -> Result<(), String> {
    let mut pump = TrofeoPump::connect()?;
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut input = BufReader::new(stdin.lock());
    let mut output = BufWriter::new(stdout.lock());
    writeln!(output, "READY {} {}", pump.pm, pump.sub)
        .map_err(|error| error.to_string())?;
    output.flush().map_err(|error| error.to_string())?;
    let mut jpeg = Vec::with_capacity(MAX_JPEG);

    loop {
        let mut length_bytes = [0_u8; 4];
        match input.read_exact(&mut length_bytes) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
            Err(error) => return Err(format!("frame length read failed: {error}")),
        }
        let length = u32::from_le_bytes(length_bytes) as usize;
        if length == 0 {
            return Ok(());
        }
        if length > MAX_JPEG {
            return Err(format!("frame exceeds {MAX_JPEG} byte limit"));
        }
        jpeg.resize(length, 0);
        input
            .read_exact(&mut jpeg)
            .map_err(|error| format!("JPEG read failed: {error}"))?;
        let elapsed = pump.send_frame(&jpeg)?;
        writeln!(output, "OK {}", elapsed.as_micros())
            .map_err(|error| error.to_string())?;
        output.flush().map_err(|error| error.to_string())?;
    }
}

fn main() {
    if let Err(error) = run() {
        println!("ERR {error}");
        let _ = io::stdout().flush();
        std::process::exit(1);
    }
}
