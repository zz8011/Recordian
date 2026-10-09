//! C ABI. Handles are exclusively owned by their caller; buffers do not overlap.
use crate::audio::{MAX_AUDIO_BYTES, PcmBuffer, convert_into, rms};
use std::{ffi::c_void, ptr, slice};
unsafe fn input<'a>(data: *const u8, len: usize) -> Option<&'a [u8]> {
    if len > MAX_AUDIO_BYTES || (len > 0 && data.is_null()) {
        return None;
    }
    Some(if len == 0 {
        &[]
    } else {
        unsafe { slice::from_raw_parts(data, len) }
    })
}
/// # Safety
/// Input/output address disjoint live buffers of the given sizes. RMS is writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_audio_convert(
    data: *const u8,
    len: usize,
    out: *mut u8,
    capacity: usize,
    level: *mut f64,
) -> i32 {
    if level.is_null() || !len.is_multiple_of(4) || capacity < len / 2 || (len > 0 && out.is_null())
    {
        return -1;
    }
    let Some(raw) = (unsafe { input(data, len) }) else {
        return -1;
    };
    let output = if len == 0 {
        &mut []
    } else {
        unsafe { slice::from_raw_parts_mut(out, len / 2) }
    };
    match convert_into(raw, output) {
        Ok(value) => {
            unsafe {
                *level = value;
            }
            0
        }
        Err(_) => -1,
    }
}
/// # Safety
/// Input is readable for len bytes and level points to one writable f64.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_audio_rms(data: *const u8, len: usize, level: *mut f64) -> i32 {
    if level.is_null() {
        return -1;
    }
    let Some(raw) = (unsafe { input(data, len) }) else {
        return -1;
    };
    unsafe {
        *level = rms(raw);
    }
    0
}
#[unsafe(no_mangle)]
pub extern "C" fn recordian_pcm_buffer_new(capacity: usize) -> *mut c_void {
    match PcmBuffer::new(capacity) {
        Ok(buffer) => Box::into_raw(Box::new(buffer)).cast(),
        Err(_) => ptr::null_mut(),
    }
}
/// # Safety
/// Handle is a live exclusive buffer handle and input is readable for len bytes.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_pcm_buffer_push(
    handle: *mut c_void,
    data: *const u8,
    len: usize,
) -> i32 {
    if handle.is_null() {
        return -1;
    }
    let Some(raw) = (unsafe { input(data, len) }) else {
        return -1;
    };
    unsafe { &mut *handle.cast::<PcmBuffer>() }
        .push(raw)
        .map_or(-1, |_| 0)
}
/// # Safety
/// Handle is live and exclusively accessed; output is writable for count f32 values.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_pcm_buffer_pop(
    handle: *mut c_void,
    out: *mut f32,
    count: usize,
) -> i32 {
    if handle.is_null() || count > MAX_AUDIO_BYTES / 2 || (count > 0 && out.is_null()) {
        return -1;
    }
    let output = if count == 0 {
        &mut []
    } else {
        unsafe { slice::from_raw_parts_mut(out, count) }
    };
    unsafe { &mut *handle.cast::<PcmBuffer>() }
        .pop_into(output)
        .map_or(-1, |_| 0)
}
/// # Safety
/// Handle is live and not concurrently accessed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_pcm_buffer_len(handle: *const c_void) -> usize {
    if handle.is_null() {
        0
    } else {
        unsafe { &*handle.cast::<PcmBuffer>() }.len()
    }
}
/// # Safety
/// Handle is live and exclusively accessed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_pcm_buffer_clear(handle: *mut c_void) {
    if !handle.is_null() {
        unsafe { &mut *handle.cast::<PcmBuffer>() }.clear();
    }
}
/// # Safety
/// Handle is owned by this caller, not concurrently used, and freed exactly once.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_pcm_buffer_free(handle: *mut c_void) {
    if !handle.is_null() {
        unsafe {
            drop(Box::from_raw(handle.cast::<PcmBuffer>()));
        }
    }
}
