//! What the owner approved for one session. Callers cannot change any of it.

use crate::error::BridgeError;

/// The supported API services. Same list as the Python prototype.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Provider {
    Moonshot,
    OpenRouter,
    Gemini,
}

impl Provider {
    pub const ALL: [Provider; 3] = [Provider::Moonshot, Provider::OpenRouter, Provider::Gemini];

    pub fn name(self) -> &'static str {
        match self {
            Provider::Moonshot => "moonshot",
            Provider::OpenRouter => "openrouter",
            Provider::Gemini => "gemini",
        }
    }

    pub fn from_name(name: &str) -> Option<Provider> {
        Provider::ALL
            .into_iter()
            .find(|provider| provider.name() == name)
    }
}

/// A model ID is 1 to 160 characters: letters, digits and `_ . : / -`,
/// starting with a letter or digit, with no `..` or `//`.
pub fn valid_model(model: &str) -> bool {
    let allowed = |c: char| c.is_ascii_alphanumeric() || "_.:/-".contains(c);
    let starts_well = model
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_alphanumeric());
    starts_well
        && model.len() <= 160
        && model.chars().all(allowed)
        && !model.contains("..")
        && !model.contains("//")
}

/// Session limits. The fields are private, so the only way to get a `Policy`
/// is through a constructor that checks every value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Policy {
    provider: Provider,
    models: Vec<String>,
    max_calls: u32,
    duration_seconds: u64,
    max_output_tokens: u32,
}

impl Policy {
    /// Default permission: 10 attempts, one hour, 1024 output tokens per call.
    pub fn new(provider: Provider, models: Vec<String>) -> Result<Policy, BridgeError> {
        Policy::with_limits(provider, models, 10, 3600, 1024)
    }

    pub fn with_limits(
        provider: Provider,
        models: Vec<String>,
        max_calls: u32,
        duration_seconds: u64,
        max_output_tokens: u32,
    ) -> Result<Policy, BridgeError> {
        if !(1..=8).contains(&models.len()) {
            return Err(BridgeError::new("Choose between one and eight model IDs."));
        }
        if !models.iter().all(|model| valid_model(model)) {
            return Err(BridgeError::new(
                "Use model IDs from your service's model list.",
            ));
        }
        let has_path = |model: &String| model.contains('/') || model.contains(':');
        if provider == Provider::Gemini && models.iter().any(has_path) {
            return Err(BridgeError::new(
                "Use the Gemini model ID without a path or URL.",
            ));
        }
        let in_range = (1..=100).contains(&max_calls)
            && (60..=28800).contains(&duration_seconds)
            && (1..=8192).contains(&max_output_tokens);
        if !in_range {
            return Err(BridgeError::new(
                "Session limits are outside the allowed range. Run setup again.",
            ));
        }
        Ok(Policy {
            provider,
            models,
            max_calls,
            duration_seconds,
            max_output_tokens,
        })
    }

    pub fn provider(&self) -> Provider {
        self.provider
    }

    pub fn models(&self) -> &[String] {
        &self.models
    }

    pub fn max_calls(&self) -> u32 {
        self.max_calls
    }

    pub fn duration_seconds(&self) -> u64 {
        self.duration_seconds
    }

    pub fn max_output_tokens(&self) -> u32 {
        self.max_output_tokens
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn models(names: &[&str]) -> Vec<String> {
        names.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn defaults_match_the_python_prototype() {
        let policy = Policy::new(Provider::Moonshot, models(&["demo-model"])).unwrap();
        assert_eq!(policy.max_calls(), 10);
        assert_eq!(policy.duration_seconds(), 3600);
        assert_eq!(policy.max_output_tokens(), 1024);
    }

    #[test]
    fn invalid_policies_are_rejected() {
        let one = || models(&["demo-model"]);
        assert!(Policy::with_limits(Provider::Moonshot, one(), 0, 3600, 1024).is_err());
        assert!(Policy::with_limits(Provider::Moonshot, one(), 101, 3600, 1024).is_err());
        assert!(Policy::with_limits(Provider::Moonshot, one(), 10, 0, 1024).is_err());
        assert!(Policy::with_limits(Provider::Moonshot, one(), 10, 3600, 100_000).is_err());
        assert!(Policy::new(Provider::Moonshot, models(&["../bad"])).is_err());
        assert!(Policy::new(Provider::Moonshot, models(&[])).is_err());
        assert!(Policy::new(Provider::Moonshot, models(&["m"; 9])).is_err());
        assert!(Policy::new(Provider::Gemini, models(&["models/x"])).is_err());
    }

    #[test]
    fn model_ids_cannot_escape_a_url_path() {
        for bad in [
            "../../secrets",
            "model?key=x",
            "https://example.org",
            "x\r\ny",
            "",
            "-x",
            "a//b",
        ] {
            assert!(!valid_model(bad), "{bad:?}");
        }
        assert!(!valid_model(&"x".repeat(161)));
        for good in [
            "test-model",
            "vendor/model:free",
            "kimi-k2.5",
            &"x".repeat(160),
        ] {
            assert!(valid_model(good), "{good:?}");
        }
    }

    #[test]
    fn provider_names_round_trip() {
        for provider in Provider::ALL {
            assert_eq!(Provider::from_name(provider.name()), Some(provider));
        }
        assert_eq!(Provider::from_name("unknown"), None);
    }
}
