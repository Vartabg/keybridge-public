//! Printing that cannot crash the program.
//!
//! `println!` panics when the terminal or pipe it writes to has gone away.
//! A daemon holding a key has to shut down cleanly even then, so every line
//! this program prints goes through these functions instead.

use std::fmt::Display;
use std::io::{self, Write};

use serde_json::Value;

/// Print one line to standard output. Returns false if nobody is reading.
pub fn say(line: impl Display) -> bool {
    let mut stdout = io::stdout();
    writeln!(stdout, "{line}")
        .and_then(|()| stdout.flush())
        .is_ok()
}

/// Print one line to standard error. A failure is ignored.
pub fn warn(line: impl Display) {
    let _ = writeln!(io::stderr(), "{line}");
}

/// JSON text that uses only plain ASCII. Every other character is written as
/// `\uXXXX`, so model output cannot send control or direction-changing
/// characters to a terminal. Python's `json.dumps` does the same by default.
pub fn ascii_json(value: &Value) -> String {
    let mut text = String::new();
    for character in value.to_string().chars() {
        if character.is_ascii() && character != '\u{7f}' {
            text.push(character);
        } else {
            // Outside a JSON string every character is ASCII, so anything
            // reaching here is inside a string, where `\u` escapes are valid.
            let mut units = [0u16; 2];
            for unit in character.encode_utf16(&mut units) {
                text.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    text
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn non_ascii_text_is_escaped_and_still_parses_back() {
        let value = json!({"text": "caf\u{e9} \u{202e}reversed \u{1f600} \u{7f}\u{9b}"});
        let line = ascii_json(&value);
        assert!(line.is_ascii());
        assert!(!line.contains('\u{7f}'));
        assert!(
            line.contains("\\u00e9") && line.contains("\\u202e") && line.contains("\\ud83d\\ude00")
        );
        assert_eq!(serde_json::from_str::<Value>(&line).unwrap(), value);
    }

    #[test]
    fn plain_ascii_is_unchanged() {
        let value = json!({"ok": true, "state": "ready"});
        assert_eq!(ascii_json(&value), value.to_string());
    }
}
