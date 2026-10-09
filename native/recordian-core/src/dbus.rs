//! Persistent, owner-pinned Fcitx transport. GIO runs its own I/O worker;
//! synchronous calls do not require a GLib main loop in the Python process.
use std::{
    collections::HashMap,
    ffi::{CStr, CString, c_char, c_int, c_void},
    ptr,
    sync::atomic::{AtomicU64, Ordering},
    time::Instant,
};

const MAX_SESSIONS: usize = 256;
pub const MAX_STRING_BYTES: usize = 1_048_576;
static NEXT_TOKEN: AtomicU64 = AtomicU64::new(1);

#[repr(C)]
struct GError {
    domain: u32,
    code: c_int,
    message: *mut c_char,
}

#[link(name = "gio-2.0")]
unsafe extern "C" {
    fn g_dbus_address_get_for_bus_sync(
        bus_type: c_int,
        cancel: *mut c_void,
        error: *mut *mut GError,
    ) -> *mut c_char;
    fn g_dbus_connection_new_for_address_sync(
        address: *const c_char,
        flags: c_int,
        observer: *mut c_void,
        cancel: *mut c_void,
        error: *mut *mut GError,
    ) -> *mut c_void;
    fn g_dbus_connection_set_exit_on_close(connection: *mut c_void, enabled: c_int);
    fn g_dbus_connection_close_sync(
        connection: *mut c_void,
        cancel: *mut c_void,
        error: *mut *mut GError,
    ) -> c_int;
    fn g_dbus_connection_call_sync(
        connection: *mut c_void,
        name: *const c_char,
        path: *const c_char,
        interface: *const c_char,
        method: *const c_char,
        parameters: *mut c_void,
        reply_type: *const c_void,
        flags: c_int,
        timeout: c_int,
        cancel: *mut c_void,
        error: *mut *mut GError,
    ) -> *mut c_void;
    fn g_dbus_error_get_remote_error(error: *const GError) -> *mut c_char;
    fn g_dbus_error_encode_gerror(error: *const GError) -> *mut c_char;
}

#[link(name = "glib-2.0")]
unsafe extern "C" {
    fn g_variant_new_string(value: *const c_char) -> *mut c_void;
    fn g_variant_new_uint32(value: u32) -> *mut c_void;
    fn g_variant_new_tuple(values: *const *mut c_void, count: usize) -> *mut c_void;
    fn g_variant_ref_sink(value: *mut c_void) -> *mut c_void;
    fn g_variant_unref(value: *mut c_void);
    fn g_variant_is_of_type(value: *mut c_void, kind: *const c_void) -> c_int;
    fn g_variant_get_size(value: *mut c_void) -> usize;
    fn g_variant_get_child_value(value: *mut c_void, index: usize) -> *mut c_void;
    fn g_variant_get_string(value: *mut c_void, length: *mut usize) -> *const c_char;
    fn g_error_free(error: *mut GError);
    fn g_free(memory: *mut c_void);
}

#[link(name = "gobject-2.0")]
unsafe extern "C" {
    fn g_object_unref(object: *mut c_void);
}

#[derive(Debug, Clone)]
pub struct BusError {
    pub name: String,
    pub message: String,
}

impl BusError {
    pub fn local(kind: &str, message: impl Into<String>) -> Self {
        Self {
            name: format!("org.recordian.Native.Error.{kind}"),
            message: message.into(),
        }
    }
}

fn take_error(error: *mut GError) -> BusError {
    if error.is_null() {
        return BusError::local("Transport", "GIO returned no result or error");
    }
    unsafe {
        let mut name = g_dbus_error_get_remote_error(error);
        if name.is_null() {
            name = g_dbus_error_encode_gerror(error);
        }
        let result = BusError {
            name: if name.is_null() {
                "org.recordian.Native.Error.Transport".into()
            } else {
                CStr::from_ptr(name).to_string_lossy().into_owned()
            },
            message: if (*error).message.is_null() {
                "GIO error".into()
            } else {
                CStr::from_ptr((*error).message)
                    .to_string_lossy()
                    .into_owned()
            },
        };
        g_free(name.cast());
        g_error_free(error);
        result
    }
}

fn string(value: &str) -> Result<CString, BusError> {
    if value.len() > MAX_STRING_BYTES {
        return Err(BusError::local(
            "InvalidArgument",
            "string exceeds transport bound",
        ));
    }
    CString::new(value).map_err(|_| BusError::local("InvalidArgument", "string contains NUL"))
}

struct Variant(*mut c_void);
impl Drop for Variant {
    fn drop(&mut self) {
        unsafe { g_variant_unref(self.0) };
    }
}

struct Binding {
    owner: CString,
    remote_token: String,
    failed: bool,
}

pub struct SessionBus {
    connection: *mut c_void,
    tokens: HashMap<String, Binding>,
}

// GDBusConnection is thread safe. All token state and calls are serialized by
// the ABI's Mutex, and ownership of the connection stays with this instance.
unsafe impl Send for SessionBus {}

impl SessionBus {
    pub fn connect(address: Option<&str>) -> Result<Self, BusError> {
        let address = match address {
            Some(address) => string(address)?,
            None => {
                let mut error = ptr::null_mut();
                let raw =
                    unsafe { g_dbus_address_get_for_bus_sync(2, ptr::null_mut(), &mut error) };
                if raw.is_null() {
                    return Err(take_error(error));
                }
                let address = unsafe { CStr::from_ptr(raw).to_owned() };
                unsafe { g_free(raw.cast()) };
                address
            }
        };
        let mut error = ptr::null_mut();
        let connection = unsafe {
            g_dbus_connection_new_for_address_sync(
                address.as_ptr(),
                9,
                ptr::null_mut(),
                ptr::null_mut(),
                &mut error,
            )
        };
        if connection.is_null() {
            return Err(take_error(error));
        }
        unsafe { g_dbus_connection_set_exit_on_close(connection, 0) };
        Ok(Self {
            connection,
            tokens: HashMap::new(),
        })
    }

    fn invoke(
        &self,
        owner: &CStr,
        method: &str,
        signature: &str,
        args: &[&str],
        timeout_ms: i32,
        bus_method: bool,
    ) -> Result<String, BusError> {
        let method = string(method)?;
        let strings = args
            .iter()
            .map(|value| string(value))
            .collect::<Result<Vec<_>, _>>()?;
        // No variadic FFI: explicitly typed children make `(sus)` stable on
        // every target ABI and copy UTF-8 without shell/display escaping.
        let mut children = Vec::with_capacity(args.len());
        for (index, kind) in signature.bytes().enumerate() {
            children.push(unsafe {
                if kind == b'u' {
                    let value = args[index].parse::<u32>().map_err(|_| {
                        BusError::local("InvalidArgument", "sequence must be uint32")
                    })?;
                    g_variant_new_uint32(value)
                } else {
                    g_variant_new_string(strings[index].as_ptr())
                }
            });
        }
        let parameters = Variant(unsafe {
            g_variant_ref_sink(g_variant_new_tuple(children.as_ptr(), children.len()))
        });
        let mut error = ptr::null_mut();
        let raw = unsafe {
            g_dbus_connection_call_sync(
                self.connection,
                owner.as_ptr(),
                if bus_method {
                    c"/org/freedesktop/DBus".as_ptr()
                } else {
                    c"/recordian".as_ptr()
                },
                if bus_method {
                    c"org.freedesktop.DBus".as_ptr()
                } else {
                    c"org.fcitx.Fcitx.Recordian1".as_ptr()
                },
                method.as_ptr(),
                parameters.0,
                ptr::null(),
                1, // NO_AUTO_START: never activate a replacement service.
                timeout_ms,
                ptr::null_mut(),
                &mut error,
            )
        };
        if raw.is_null() {
            return Err(take_error(error));
        }
        let reply = Variant(raw);
        unsafe {
            if g_variant_is_of_type(reply.0, c"(s)".as_ptr().cast()) == 0
                || g_variant_get_size(reply.0) > MAX_STRING_BYTES
            {
                return Err(BusError::local(
                    "InvalidReply",
                    "expected one bounded string reply (s)",
                ));
            }
            let child = Variant(g_variant_get_child_value(reply.0, 0));
            let value = g_variant_get_string(child.0, ptr::null_mut());
            if value.is_null() {
                return Err(BusError::local("InvalidReply", "missing string reply"));
            }
            CStr::from_ptr(value)
                .to_str()
                .map(str::to_owned)
                .map_err(|_| BusError::local("InvalidReply", "reply is not UTF-8"))
        }
    }

    pub fn call(
        &mut self,
        method: &str,
        signature: &str,
        args: &[&str],
        timeout_ms: i32,
    ) -> Result<String, BusError> {
        let expected = match method {
            "Ping" => "",
            "BeginSession" | "CommitText" | "CancelSession" => "s",
            "UpdatePreedit" | "CommitSession" => "ss",
            "CommitSegment" => "sus",
            _ => {
                return Err(BusError::local(
                    "InvalidArgument",
                    "method is not allowlisted",
                ));
            }
        };
        if signature != expected
            || args.len() != expected.len()
            || !(1..=60_000).contains(&timeout_ms)
        {
            return Err(BusError::local(
                "InvalidArgument",
                "invalid signature, arity or timeout",
            ));
        }
        for value in args {
            string(value)?;
        }
        if method == "CommitSegment" && args[1].parse::<u32>().is_err() {
            return Err(BusError::local(
                "InvalidArgument",
                "sequence must be uint32",
            ));
        }
        match method {
            "Ping" | "CommitText" => self.invoke(
                c"org.fcitx.Fcitx5",
                method,
                signature,
                args,
                timeout_ms,
                false,
            ),
            "BeginSession" => {
                // Refuse before dispatch when full; otherwise Begin could
                // leave a session whose binding we cannot retain.
                if self.tokens.len() >= MAX_SESSIONS {
                    return Err(BusError::local("SessionBusy", "native token table is full"));
                }
                let started = Instant::now();
                let owner = self.invoke(
                    c"org.freedesktop.DBus",
                    "GetNameOwner",
                    "s",
                    &["org.fcitx.Fcitx5"],
                    timeout_ms,
                    true,
                )?;
                if !owner.starts_with(':') {
                    return Err(BusError::local(
                        "InvalidReply",
                        "owner is not a unique bus name",
                    ));
                }
                let owner = string(&owner)?;
                let remaining =
                    timeout_ms - started.elapsed().as_millis().min(i32::MAX as u128) as i32;
                if remaining <= 0 {
                    return Err(BusError::local(
                        "Timeout",
                        "owner resolution exhausted call deadline",
                    ));
                }
                let descriptor = self.invoke(&owner, method, signature, args, remaining, false)?;
                let remote = descriptor
                    .split_whitespace()
                    .next()
                    .filter(|token| token.len() <= 1024)
                    .ok_or_else(|| BusError::local("InvalidReply", "invalid BeginSession token"))?;
                // Tokens are opaque to Python. A monotonically unique local
                // token prevents a restarted addon reusing a raw token from
                // redirecting an old session, even after a binding is removed.
                let id = NEXT_TOKEN
                    .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                        value.checked_add(1)
                    })
                    .map_err(|_| BusError::local("SessionBusy", "native token IDs exhausted"))?;
                let token = format!("recordian-native-{id}");
                let suffix = &descriptor[descriptor.find(remote).unwrap() + remote.len()..];
                self.tokens.insert(
                    token.clone(),
                    Binding {
                        owner,
                        remote_token: remote.into(),
                        failed: false,
                    },
                );
                Ok(format!("{token}{suffix}"))
            }
            _ => {
                let token = args[0];
                let binding = self.tokens.get(token).ok_or_else(|| {
                    BusError::local(
                        "UnknownToken",
                        "session token is not bound on this connection",
                    )
                })?;
                if binding.failed && method != "CancelSession" {
                    return Err(BusError::local(
                        "SessionClosed",
                        "failed mutation cannot be replayed",
                    ));
                }
                let mut remote_args = args.to_vec();
                remote_args[0] = &binding.remote_token;
                let result = self.invoke(
                    &binding.owner,
                    method,
                    signature,
                    &remote_args,
                    timeout_ms,
                    false,
                );
                if method == "CancelSession"
                    || (method == "CommitSession" && result.is_ok())
                    || result.as_ref().is_err_and(|error| {
                        error.name == "org.fcitx.Fcitx.Recordian.Error.StaleSession"
                    })
                {
                    // Unknown tokens are never resolved against a current
                    // owner. Removing a terminal binding is safe and bounded.
                    // Exact StaleSession is definitive: Python closes without
                    // cancellation, so retaining it would exhaust this table.
                    self.tokens.remove(token);
                } else if let Err(error) = &result
                    && !(method == "CommitSegment"
                        && error.name == "org.fcitx.Fcitx.Recordian.Error.SegmentsUnsafe")
                    && let Some(binding) = self.tokens.get_mut(token)
                {
                    binding.failed = true;
                }
                result
            }
        }
    }
}

impl Drop for SessionBus {
    fn drop(&mut self) {
        unsafe {
            g_dbus_connection_close_sync(self.connection, ptr::null_mut(), ptr::null_mut());
            g_object_unref(self.connection);
        }
    }
}
