//! Byte-exact audio kernels and a bounded FIFO, independent of Python/GIL.
pub const MAX_AUDIO_BYTES: usize = 64 * 1024 * 1024;
pub fn convert_into(raw: &[u8], out: &mut [u8]) -> Result<f64, &'static str> {
    if !raw.len().is_multiple_of(4) || out.len() < raw.len() / 2 {
        return Err("partial float32 sample or short output");
    }
    let mut sum = 0.0f64;
    for (input, output) in raw
        .as_chunks::<4>()
        .0
        .iter()
        .zip(out.as_chunks_mut::<2>().0)
    {
        let original = f32::from_le_bytes(*input) as f64;
        sum += original * original;
        let value = if original.is_nan() {
            0.0
        } else {
            original.clamp(-1.0, 1.0)
        };
        let scaled = value * 32767.0;
        let sample = if scaled >= 0.0 {
            (scaled + 0.5) as i16
        } else {
            (scaled - 0.5) as i16
        };
        output.copy_from_slice(&sample.to_le_bytes());
    }
    Ok(if raw.is_empty() {
        0.0
    } else {
        (sum / (raw.len() / 4) as f64).sqrt()
    })
}
pub fn rms(raw: &[u8]) -> f64 {
    let mut count = 0usize;
    let mut sum = 0.0f64;
    for input in raw.as_chunks::<4>().0 {
        let value = f32::from_le_bytes(*input) as f64;
        sum += value * value;
        count += 1;
    }
    if count == 0 {
        0.0
    } else {
        (sum / count as f64).sqrt()
    }
}
pub struct PcmBuffer {
    bytes: Vec<u8>,
    head: usize,
    used: usize,
}
impl PcmBuffer {
    pub fn new(capacity: usize) -> Result<Self, &'static str> {
        if capacity == 0 || !capacity.is_multiple_of(2) || capacity > MAX_AUDIO_BYTES {
            return Err("invalid PCM capacity");
        }
        let mut bytes = Vec::new();
        bytes
            .try_reserve_exact(capacity)
            .map_err(|_| "PCM allocation failed")?;
        bytes.resize(capacity, 0);
        Ok(Self {
            bytes,
            head: 0,
            used: 0,
        })
    }
    pub fn len(&self) -> usize {
        self.used
    }
    pub fn is_empty(&self) -> bool {
        self.used == 0
    }
    pub fn push(&mut self, pcm: &[u8]) -> Result<(), &'static str> {
        if !pcm.len().is_multiple_of(2) || pcm.len() > self.bytes.len() - self.used {
            return Err("partial PCM sample or bounded buffer full");
        }
        let tail = (self.head + self.used) % self.bytes.len();
        let first = pcm.len().min(self.bytes.len() - tail);
        self.bytes[tail..tail + first].copy_from_slice(&pcm[..first]);
        self.bytes[..pcm.len() - first].copy_from_slice(&pcm[first..]);
        self.used += pcm.len();
        Ok(())
    }
    pub fn pop_into(&mut self, out: &mut [f32]) -> Result<(), &'static str> {
        if out.len() > self.used / 2 {
            return Err("insufficient PCM samples");
        }
        for sample in out.iter_mut() {
            let value = i16::from_le_bytes([self.bytes[self.head], self.bytes[self.head + 1]]);
            *sample = value as f32 / 32768.0;
            self.head = (self.head + 2) % self.bytes.len();
        }
        self.used -= out.len() * 2;
        Ok(())
    }
    pub fn clear(&mut self) {
        self.head = 0;
        self.used = 0;
    }
}
