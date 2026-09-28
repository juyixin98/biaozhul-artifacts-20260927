//! LZ77B — LZ77 block compression backend with a fixed sliding dictionary.
//!
//! See `README.md` for the format and module contract. The crate is split into
//! four engineering boundaries:
//!
//! * [`core`]      — data format + codec kernel (pure, no I/O)
//! * [`reference`] — independent reference decompressor (shares no core code)
//! * [`store`]     — filesystem persistence adapter
//! * [`http`]      — axum validation/service interface

pub mod core;
pub mod http;
pub mod reference;
pub mod store;
pub mod util_b64;
