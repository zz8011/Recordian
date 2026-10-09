//! Version 1 D-Bus C ABI. Strings are UTF-8 C strings, allocated outputs
//! belong to the caller and must be released with recordian_dbus_string_free_v1.
use crate::dbus::{BusError, MAX_STRING_BYTES, SessionBus};
use std::{
    ffi::{CStr, CString, c_char},
    panic::{AssertUnwindSafe, catch_unwind},
    ptr,
    sync::Mutex,
};

pub struct BusHandle(Mutex<SessionBus>);

#[unsafe(no_mangle)]
pub extern "C" fn recordian_dbus_abi_version() -> u32 {
    1
}

unsafe fn input<'a>(value: *const c_char) -> Result<&'a str, BusError> {
    if value.is_null() {
        return Err(BusError::local("InvalidArgument", "null input"));
    }
    let value = unsafe { CStr::from_ptr(value) };
    if value.to_bytes().len() > MAX_STRING_BYTES {
        return Err(BusError::local("InvalidArgument", "input exceeds bound"));
    }
    value
        .to_str()
        .map_err(|_| BusError::local("InvalidArgument", "input is not UTF-8"))
}

fn allocated(value: String) -> *mut c_char {
    // GLib errors cannot contain embedded NUL. Sanitize panic diagnostics too.
    CString::new(value.replace('\0', "�")).unwrap().into_raw()
}

unsafe fn finish<T>(
    action: impl FnOnce() -> Result<T, BusError>,
    error_name: *mut *mut c_char,
    error_message: *mut *mut c_char,
) -> Result<T, i32> {
    if error_name.is_null() || error_message.is_null() {
        return Err(1);
    }
    unsafe {
        *error_name = ptr::null_mut();
        *error_message = ptr::null_mut();
    }
    let result = catch_unwind(AssertUnwindSafe(action)).unwrap_or_else(|_| {
        Err(BusError::local(
            "Panic",
            "native transport panicked; request must not be replayed",
        ))
    });
    result.map_err(|error| {
        unsafe {
            *error_name = allocated(error.name);
            *error_message = allocated(error.message);
        }
        1
    })
}

/// # Safety
/// address is null for the session bus or a valid UTF-8 C string. All output
/// pointers must be writable and distinct. No native handle is returned on error.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_dbus_new_v1(
    address: *const c_char,
    output: *mut *mut BusHandle,
    error_name: *mut *mut c_char,
    error_message: *mut *mut c_char,
) -> i32 {
    if output.is_null() {
        return 1;
    }
    unsafe {
        *output = ptr::null_mut();
    }
    let result = unsafe {
        finish(
            || {
                let address = if address.is_null() {
                    None
                } else {
                    Some(input(address)?)
                };
                SessionBus::connect(address).map(|bus| Box::new(BusHandle(Mutex::new(bus))))
            },
            error_name,
            error_message,
        )
    };
    match result {
        Ok(handle) => {
            unsafe {
                *output = Box::into_raw(handle);
            }
            0
        }
        Err(status) => status,
    }
}

/// # Safety
/// handle is a live handle from new_v1. Input pointers and args' count entries
/// must be valid C strings for this call. Output pointers are writable/distinct.
/// free_v1 must not run concurrently with this function.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_dbus_call_v1(
    handle: *mut BusHandle,
    method: *const c_char,
    signature: *const c_char,
    args: *const *const c_char,
    count: usize,
    timeout_ms: i32,
    output: *mut *mut c_char,
    error_name: *mut *mut c_char,
    error_message: *mut *mut c_char,
) -> i32 {
    if output.is_null() {
        return 1;
    }
    unsafe {
        *output = ptr::null_mut();
    }
    let result = unsafe {
        finish(
            || {
                if handle.is_null() || count > 3 || (count > 0 && args.is_null()) {
                    return Err(BusError::local(
                        "InvalidArgument",
                        "invalid handle or argument array",
                    ));
                }
                let method = input(method)?;
                let signature = input(signature)?;
                let pointers = if count == 0 {
                    &[][..]
                } else {
                    std::slice::from_raw_parts(args, count)
                };
                let values = pointers
                    .iter()
                    .map(|value| input(*value))
                    .collect::<Result<Vec<_>, _>>()?;
                let mut bus = (*handle).0.lock().map_err(|_| {
                    BusError::local(
                        "Panic",
                        "transport lock poisoned; request must not be replayed",
                    )
                })?;
                bus.call(method, signature, &values, timeout_ms)
            },
            error_name,
            error_message,
        )
    };
    match result {
        Ok(value) => {
            unsafe {
                *output = allocated(value);
            }
            0
        }
        Err(status) => status,
    }
}

/// # Safety
/// handle must be null or a live handle, freed exactly once, with no calls in flight.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_dbus_free_v1(handle: *mut BusHandle) {
    if !handle.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| unsafe {
            drop(Box::from_raw(handle));
        }));
    }
}

/// # Safety
/// value must be null or an output from this ABI, freed exactly once.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn recordian_dbus_string_free_v1(value: *mut c_char) {
    if !value.is_null() {
        unsafe {
            drop(CString::from_raw(value));
        }
    }
}
