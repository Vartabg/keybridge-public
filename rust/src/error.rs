//! One error type for the whole crate.
//!
//! A `BridgeError` only ever carries a fixed, safe sentence that is fine to
//! show to a person or an agent. Library errors are never formatted into it,
//! because those can contain request details.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BridgeError(String);

impl BridgeError {
    pub fn new(message: impl Into<String>) -> Self {
        BridgeError(message.into())
    }

    pub fn message(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for BridgeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for BridgeError {}

/// Why a command stopped early.
#[derive(Debug)]
pub enum Stop {
    Error(BridgeError),
    /// Control-C, or the terminal was closed.
    Cancelled,
}

/// Lets `?` turn a `BridgeError` into a `Stop` automatically.
impl From<BridgeError> for Stop {
    fn from(error: BridgeError) -> Self {
        Stop::Error(error)
    }
}
