//! The `keybridge` program. All the logic lives in the library (`lib.rs`).

fn main() {
    // If the program crashes, print a fixed line. The default crash report
    // can include values from memory; this one cannot. `warn` cannot fail,
    // so the crash handler itself never crashes.
    std::panic::set_hook(Box::new(|_| {
        keybridge::output::warn("[Keybridge] Internal error. Details are withheld on purpose.");
    }));
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    std::process::exit(keybridge::cli::run(&arguments));
}
