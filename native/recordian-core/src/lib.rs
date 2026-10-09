pub mod audio;
pub mod audio_ffi;
pub mod dbus;
pub mod dbus_ffi;

#[unsafe(no_mangle)]
pub extern "C" fn recordian_core_abi_version() -> u32 {
    1
}
