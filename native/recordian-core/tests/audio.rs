use recordian_core::audio::{PcmBuffer, convert_into, rms};
fn raw(values: &[f32]) -> Vec<u8> {
    values.iter().flat_map(|v| v.to_le_bytes()).collect()
}

#[test]
fn quantization_and_raw_rms_are_independent() {
    let data = raw(&[
        0.0,
        0.5,
        -0.5,
        1.0,
        -1.0,
        f32::NAN,
        f32::INFINITY,
        f32::NEG_INFINITY,
    ]);
    let mut out = [0u8; 16];
    assert!(convert_into(&data, &mut out).unwrap().is_nan());
    let samples: Vec<i16> = out
        .as_chunks::<2>()
        .0
        .iter()
        .map(|b| i16::from_le_bytes([b[0], b[1]]))
        .collect();
    assert_eq!(samples, [0, 16384, -16384, 32767, -32767, 0, 32767, -32767]);
    assert_eq!(rms(&raw(&[2.0, -2.0])), 2.0);
}
#[test]
fn malformed_or_short_output_does_not_write() {
    let mut out = [9u8; 2];
    assert!(convert_into(&[1, 2, 3], &mut out).is_err());
    assert_eq!(out, [9, 9]);
    assert!(convert_into(&raw(&[0.5, 1.0]), &mut out).is_err());
    assert_eq!(out, [9, 9]);
}
#[test]
fn rms_keeps_partial_frame_and_nonfinite_behavior() {
    let mut data = raw(&[0.5, -0.5]);
    data.extend([1, 2, 3]);
    assert_eq!(rms(&data), 0.5);
    assert!(rms(&raw(&[f32::NAN])).is_nan());
    assert!(rms(&raw(&[f32::INFINITY])).is_infinite());
    assert_eq!(rms(&[]), 0.0);
}
#[test]
fn ring_wraps_without_losing_tail_and_overflow_is_atomic() {
    let mut q = PcmBuffer::new(6).unwrap();
    q.push(&[0, 64, 0, 192]).unwrap();
    let mut first = [0f32; 1];
    q.pop_into(&mut first).unwrap();
    assert_eq!(first, [0.5]);
    q.push(&[255, 127, 0, 128]).unwrap();
    assert_eq!(q.len(), 6);
    assert!(q.push(&[0, 0]).is_err());
    assert_eq!(q.len(), 6);
    let mut rest = [0f32; 3];
    q.pop_into(&mut rest).unwrap();
    assert_eq!(rest, [-0.5, 32767.0 / 32768.0, -1.0]);
    assert!(q.is_empty());
    assert!(q.push(&[1]).is_err());
    assert!(q.pop_into(&mut first).is_err());
    q.push(&[0, 0]).unwrap();
    q.clear();
    assert!(q.is_empty());
}
#[test]
fn invalid_capacity_is_rejected() {
    assert!(PcmBuffer::new(0).is_err());
    assert!(PcmBuffer::new(3).is_err());
    assert!(PcmBuffer::new(usize::MAX).is_err());
}
