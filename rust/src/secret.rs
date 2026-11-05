//! The broker keeps the API key inside a `Secret`.
//!
//! Short-lived copies exist only while a request is being built (the
//! `Authorization` text, and the encoded forms used for redaction). Those are
//! wiped too. Search for `expose` to find every place the key is read.

use std::fmt;

use zeroize::Zeroizing;

use crate::error::BridgeError;

/// Holds the API key in memory.
///
/// `Zeroizing` overwrites the text with zeros when the `Secret` is dropped
/// (goes out of use). There is no `Clone`, no `Display` and no way to turn a
/// `Secret` into JSON, so it cannot be copied or printed by accident.
pub struct Secret(Zeroizing<String>);

impl Secret {
    pub fn new(value: String) -> Self {
        Secret(Zeroizing::new(value))
    }

    /// Accept a key typed by the owner: 8 to 4096 visible ASCII characters,
    /// no spaces. A rejected value is still wiped, because it is wrapped first.
    pub fn from_owner_input(value: String) -> Result<Self, BridgeError> {
        let secret = Secret::new(value);
        let text = secret.expose();
        let visible = text.bytes().all(|byte| (0x21..=0x7e).contains(&byte));
        if !(8..=4096).contains(&text.len()) || !visible {
            return Err(BridgeError::new(
                "Enter only the API key, with no spaces or explanatory text.",
            ));
        }
        Ok(secret)
    }

    /// The one place the key text can be read. Search for `expose` to audit
    /// every use.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

/// Printing a `Secret` for debugging shows this label, never the key.
impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret([hidden])")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_output_hides_the_value() {
        let secret = Secret::new("example-credential-for-tests-only".to_string());
        assert_eq!(format!("{secret:?}"), "Secret([hidden])");
    }

    #[test]
    fn owner_input_must_look_like_a_key() {
        assert!(Secret::from_owner_input("fake-value-123".to_string()).is_ok());
        for bad in [
            "short",
            "has a space in it",
            "tab\tinside-value",
            "caf\u{e9}-value-123",
            "",
        ] {
            assert!(
                Secret::from_owner_input(bad.to_string()).is_err(),
                "{bad:?}"
            );
        }
        assert!(Secret::from_owner_input("x".repeat(4097)).is_err());
    }
}
