//! Fixed provider requests: no arbitrary URLs, headers, tools or redirects.

use std::time::Duration;

use serde_json::{Value, json};
use ureq::http::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE, HeaderName, HeaderValue};
use zeroize::Zeroizing;

use crate::error::BridgeError;
use crate::policy::{Provider, valid_model};
use crate::secret::Secret;

/// Largest provider response body that will be read, in bytes.
pub const MAX_RESPONSE: u64 = 524_288;

/// "Something that can send one text request and return the answer text."
///
/// The broker only knows this trait. The real HTTP client, the demo and the
/// tests each provide their own version, so the broker's rules are tested
/// without any network.
pub trait Upstream: Send + Sync {
    fn send(
        &self,
        provider: Provider,
        key: &Secret,
        model: &str,
        prompt: &str,
        max_output_tokens: u32,
    ) -> Result<String, BridgeError>;
}

/// The fixed address for each service. The model ID is the only caller text
/// that can reach a URL, and only for Gemini, after `valid_model`.
pub fn endpoint(provider: Provider, model: &str) -> Result<String, BridgeError> {
    if !valid_model(model) {
        return Err(BridgeError::new(
            "The requested service or model is not supported.",
        ));
    }
    match provider {
        Provider::Moonshot => Ok("https://api.moonshot.ai/v1/chat/completions".to_string()),
        Provider::OpenRouter => Ok("https://openrouter.ai/api/v1/chat/completions".to_string()),
        Provider::Gemini => {
            if model.contains('/') || model.contains(':') {
                return Err(BridgeError::new(
                    "Use a Gemini model ID without a path or URL.",
                ));
            }
            Ok(format!(
                "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            ))
        }
    }
}

/// The JSON sent to the service. The key is never part of the body.
pub fn request_body(
    provider: Provider,
    model: &str,
    prompt: &str,
    max_output_tokens: u32,
) -> Value {
    match provider {
        Provider::Gemini => json!({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens},
        }),
        Provider::Moonshot | Provider::OpenRouter => json!({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
            "stream": false,
        }),
    }
}

/// Keep only the answer text. Everything else in the response is discarded.
pub fn parse_text(provider: Provider, raw: &[u8]) -> Result<String, BridgeError> {
    let unsupported =
        || BridgeError::new("The service did not return supported text. Check the chosen model.");
    let data: Value = serde_json::from_slice(raw).map_err(|_| unsupported())?;
    let text = match provider {
        Provider::Gemini => {
            let parts = data["candidates"][0]["content"]["parts"]
                .as_array()
                .ok_or_else(unsupported)?;
            let mut text = String::new();
            for part in parts {
                let part = part.as_object().ok_or_else(unsupported)?;
                let Some(piece) = part.get("text") else {
                    continue;
                };
                // Skip the model's "thought" parts; they are not the answer.
                let is_thought = !matches!(
                    part.get("thought"),
                    None | Some(Value::Null | Value::Bool(false))
                );
                if !is_thought {
                    text.push_str(piece.as_str().ok_or_else(unsupported)?);
                }
            }
            text
        }
        Provider::Moonshot | Provider::OpenRouter => data["choices"][0]["message"]["content"]
            .as_str()
            .ok_or_else(unsupported)?
            .to_string(),
    };
    if text.is_empty() {
        return Err(unsupported());
    }
    Ok(text)
}

/// The real client: HTTPS only, no proxy from the environment, no redirects.
pub struct HttpUpstream {
    agent: ureq::Agent,
}

impl HttpUpstream {
    pub fn new() -> Self {
        HttpUpstream {
            agent: build_agent(true),
        }
    }

    fn post(
        &self,
        url: &str,
        provider: Provider,
        key: &Secret,
        body: &Value,
    ) -> Result<String, BridgeError> {
        let unreachable = || {
            BridgeError::new(
                "The service could not be reached. This attempt was counted; retry only if appropriate.",
            )
        };
        // The key travels in one header. `set_sensitive` makes the header
        // print as `Sensitive` if it is ever shown with `{:?}`.
        let (name, text): (HeaderName, Zeroizing<String>) = match provider {
            Provider::Gemini => (
                HeaderName::from_static("x-goog-api-key"),
                Zeroizing::new(key.expose().to_string()),
            ),
            Provider::Moonshot | Provider::OpenRouter => (
                AUTHORIZATION,
                Zeroizing::new(format!("Bearer {}", key.expose())),
            ),
        };
        let mut credential = HeaderValue::from_str(&text).map_err(|_| unreachable())?;
        credential.set_sensitive(true);

        // Library errors are replaced, never formatted: they can hold request details.
        let mut response = self
            .agent
            .post(url)
            .header(CONTENT_TYPE, "application/json")
            .header(ACCEPT, "application/json")
            .header(name, credential)
            .send(body.to_string().as_bytes())
            .map_err(|_| unreachable())?;

        match response.status().as_u16() {
            200..=299 => {}
            401 | 403 => {
                return Err(BridgeError::new(
                    "The service rejected authentication. Check the key for this connection.",
                ));
            }
            429 => {
                return Err(BridgeError::new(
                    "The service reached a usage limit. Check your provider account before retrying.",
                ));
            }
            // Includes redirects: they are refused instead of forwarding the key.
            _ => {
                return Err(BridgeError::new(
                    "The service rejected the request. Check the model and provider account.",
                ));
            }
        }
        let raw = response
            .body_mut()
            .with_config()
            // The reader refuses a body once it reaches its limit, so one
            // extra byte lets a body of exactly MAX_RESPONSE through.
            .limit(MAX_RESPONSE + 1)
            .read_to_vec()
            .map_err(|error| match error {
                ureq::Error::BodyExceedsLimit(_) => {
                    BridgeError::new("The service response was too large. Try a smaller request.")
                }
                _ => unreachable(),
            })?;
        parse_text(provider, &raw)
    }
}

impl Default for HttpUpstream {
    fn default() -> Self {
        HttpUpstream::new()
    }
}

impl Upstream for HttpUpstream {
    fn send(
        &self,
        provider: Provider,
        key: &Secret,
        model: &str,
        prompt: &str,
        max_output_tokens: u32,
    ) -> Result<String, BridgeError> {
        let url = endpoint(provider, model)?;
        let body = request_body(provider, model, prompt, max_output_tokens);
        self.post(&url, provider, key, &body)
    }
}

fn build_agent(https_only: bool) -> ureq::Agent {
    ureq::Agent::config_builder()
        .https_only(https_only)
        .proxy(None) // Ignore HTTP_PROXY and friends from the environment.
        .max_redirects(0) // A redirect is returned as-is, then refused above.
        .http_status_as_error(false) // Status codes are mapped to safe messages above.
        .timeout_global(Some(Duration::from_secs(45)))
        .max_idle_connections(0) // A new connection for every request.
        .build()
        .new_agent()
}

#[cfg(test)]
mod tests {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;

    use super::*;

    const FAKE_KEY: &str = "example-value-for-tests-only";

    #[test]
    fn urls_and_bodies_are_fixed() {
        let expected = [
            (
                Provider::Moonshot,
                "https://api.moonshot.ai/v1/chat/completions",
            ),
            (
                Provider::OpenRouter,
                "https://openrouter.ai/api/v1/chat/completions",
            ),
            (
                Provider::Gemini,
                "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent",
            ),
        ];
        for (provider, url) in expected {
            assert_eq!(endpoint(provider, "test-model").unwrap(), url);
            let body = request_body(provider, "test-model", "hello", 256);
            if provider == Provider::Gemini {
                assert_eq!(body["generationConfig"]["maxOutputTokens"], 256);
            } else {
                assert_eq!(body["max_tokens"], 256);
                assert_eq!(body["stream"], false);
            }
        }
    }

    #[test]
    fn path_escapes_are_rejected() {
        for model in [
            "../../secrets",
            "model?key=x",
            "https://example.org",
            "x\r\ny",
        ] {
            assert!(endpoint(Provider::Gemini, model).is_err(), "{model:?}");
        }
        assert!(endpoint(Provider::Gemini, "models/x").is_err());
    }

    #[test]
    fn only_text_is_returned() {
        let body = br#"{"choices": [{"message": {"content": "hello"}}], "debug": "discard me"}"#;
        assert_eq!(parse_text(Provider::Moonshot, body).unwrap(), "hello");
        let gemini = br#"{"candidates": [{"content": {"parts": [
            {"text": "hello"}, {"text": "hidden", "thought": true}, {"text": " world"}]}}]}"#;
        assert_eq!(parse_text(Provider::Gemini, gemini).unwrap(), "hello world");
    }

    #[test]
    fn invalid_responses_are_rejected() {
        for body in [
            &b"not json"[..],
            b"{}",
            b"[]",
            br#"{"choices": [{"message": {"content": ""}}]}"#,
        ] {
            assert!(parse_text(Provider::Moonshot, body).is_err());
            assert!(parse_text(Provider::Gemini, body).is_err());
        }
    }

    /// A one-request HTTP server on this machine. It returns what it was told
    /// to and hands back the request it saw, so tests can inspect it.
    fn local_server(
        status_line: &'static str,
        extra_headers: &'static str,
        body: Vec<u8>,
    ) -> (String, mpsc::Receiver<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/v1/test", listener.local_addr().unwrap());
        let (seen, requests) = mpsc::channel();
        thread::spawn(move || {
            for stream in listener.incoming() {
                let mut stream = stream.unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut head = String::new();
                let mut length = 0;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        length = value.trim().parse().unwrap();
                    }
                    if line == "\r\n" || line.is_empty() {
                        break;
                    }
                    head.push_str(&line);
                }
                let mut sent = vec![0; length];
                reader.read_exact(&mut sent).unwrap();
                let _ = seen.send(format!("{head}\n{}", String::from_utf8_lossy(&sent)));
                let head = format!(
                    "HTTP/1.1 {status_line}\r\nContent-Length: {}\r\nConnection: close\r\n{extra_headers}\r\n",
                    body.len()
                );
                let _ = stream.write_all(head.as_bytes());
                let _ = stream.write_all(&body);
            }
        });
        (url, requests)
    }

    fn local_client() -> HttpUpstream {
        HttpUpstream {
            agent: build_agent(false),
        }
    }

    fn post(url: &str, provider: Provider) -> Result<String, BridgeError> {
        let key = Secret::new(FAKE_KEY.to_string());
        local_client().post(
            url,
            provider,
            &key,
            &request_body(provider, "test-model", "hi", 256),
        )
    }

    #[test]
    fn key_goes_in_the_auth_header_and_text_comes_back() {
        let ok = br#"{"choices": [{"message": {"content": "hello"}}]}"#.to_vec();
        let (url, requests) = local_server("200 OK", "", ok);
        assert_eq!(post(&url, Provider::Moonshot).unwrap(), "hello");
        let seen = requests.recv().unwrap().to_ascii_lowercase();
        assert!(seen.contains(&format!("authorization: bearer {FAKE_KEY}")));
        assert!(seen.contains("\"max_tokens\":256"));
        // The header is the only place the key was sent: not the URL, not the body.
        assert_eq!(seen.matches(FAKE_KEY).count(), 1);

        let ok = br#"{"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}"#.to_vec();
        let (url, requests) = local_server("200 OK", "", ok);
        assert_eq!(post(&url, Provider::Gemini).unwrap(), "hello");
        let seen = requests.recv().unwrap().to_ascii_lowercase();
        assert!(seen.contains(&format!("x-goog-api-key: {FAKE_KEY}")));
        assert!(!seen.contains("authorization:"));
        assert_eq!(seen.matches(FAKE_KEY).count(), 1);
    }

    #[test]
    fn http_errors_become_fixed_messages() {
        let cases = [
            ("401 Unauthorized", "authentication"),
            ("403 Forbidden", "authentication"),
            ("429 Too Many Requests", "usage limit"),
            ("500 Server Error", "rejected the request"),
        ];
        for (status, expected) in cases {
            let (url, _requests) =
                local_server(status, "", format!("secret detail {FAKE_KEY}").into_bytes());
            let error = post(&url, Provider::Moonshot).unwrap_err();
            assert!(error.message().contains(expected), "{status}: {error}");
            assert!(!error.message().contains(FAKE_KEY));
            assert!(!error.message().contains("secret detail"));
        }
    }

    #[test]
    fn redirects_are_not_followed() {
        let ok = br#"{"choices": [{"message": {"content": "should never be fetched"}}]}"#.to_vec();
        let (target, target_requests) = local_server("200 OK", "", ok);
        let location: &'static str = Box::leak(format!("Location: {target}\r\n").into_boxed_str());
        let (url, _requests) = local_server("302 Found", location, Vec::new());
        let error = post(&url, Provider::Moonshot).unwrap_err();
        assert!(error.message().contains("rejected the request"));
        assert!(
            target_requests.try_recv().is_err(),
            "the redirect target was contacted"
        );
    }

    #[test]
    fn oversized_response_is_rejected() {
        let (url, _requests) = local_server("200 OK", "", vec![b'x'; MAX_RESPONSE as usize + 1]);
        let error = post(&url, Provider::Moonshot).unwrap_err();
        assert!(error.message().contains("too large"), "{error}");
    }

    #[test]
    fn a_response_of_exactly_the_limit_is_accepted() {
        let (open, close) = (r#"{"choices": [{"message": {"content": ""#, r#""}}]}"#);
        let filler = "x".repeat(MAX_RESPONSE as usize - open.len() - close.len());
        let body = format!("{open}{filler}{close}").into_bytes();
        assert_eq!(body.len() as u64, MAX_RESPONSE);
        let (url, _requests) = local_server("200 OK", "", body);
        assert_eq!(post(&url, Provider::Moonshot).unwrap(), filler);
    }

    #[test]
    fn unreachable_service_has_a_fixed_message() {
        // Nothing listens on this port once the listener is dropped.
        let port = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let error = post(&format!("http://127.0.0.1:{port}/"), Provider::Moonshot).unwrap_err();
        assert!(error.message().contains("could not be reached"));
    }

    #[test]
    fn the_real_client_refuses_plain_http() {
        let ok = br#"{"choices": [{"message": {"content": "hello"}}]}"#.to_vec();
        let (url, requests) = local_server("200 OK", "", ok);
        let key = Secret::new(FAKE_KEY.to_string());
        let body = request_body(Provider::Moonshot, "test-model", "hi", 256);
        assert!(
            HttpUpstream::new()
                .post(&url, Provider::Moonshot, &key, &body)
                .is_err()
        );
        assert!(
            requests.try_recv().is_err(),
            "the key was sent over plain HTTP"
        );
    }
}
