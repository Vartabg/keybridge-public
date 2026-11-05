//! Keybridge: let a coding agent use an API without handing it the key.
//!
//! This is the Rust version of the key-holding daemon. It reduces accidental
//! exposure of a key. It does not isolate the key from hostile software
//! running as the same OS user. See `SECURITY.md` at the repository root.

// The compiler refuses any `unsafe` block in this crate.
#![forbid(unsafe_code)]

pub mod broker;
pub mod cli;
pub mod demo;
pub mod error;
pub mod output;
pub mod policy;
pub mod providers;
pub mod secret;
pub mod transport;
