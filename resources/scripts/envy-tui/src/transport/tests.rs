use super::*;
use serde_json::json;

fn limits() -> Limits {
    Limits {
        timeout: Duration::from_secs(2),
        stdout_bytes: 4096,
        stderr_bytes: 4096,
    }
}

fn shell(script: &str, limits: Limits) -> Result<JsonResponse, TransportError> {
    run_json(Command::new("sh").args(["-c", script]), "storage inspect", limits)
}

#[test]
fn nonzero_business_failure_preserves_the_complete_response() {
    let payload = json!({
        "schemaVersion": 1, "command": "storage inspect", "ok": false,
        "data": {"result": "partial", "rules": [{"id": "cache", "ok": false}],
            "errors": ["unreadable"], "warnings": ["estimate"]},
        "error": {"code": "partial_failure", "message": "inspect rule results"},
        "errors": [{"code": "unreadable", "path": "/synthetic"}],
        "warnings": ["incomplete"]
    });
    let script = format!("printf '%s' '{}'; printf diagnostic >&2; exit 7", payload);
    let response = shell(&script, limits()).unwrap();
    assert_eq!(response.payload, payload);
    assert_eq!(response.status.code(), Some(7));
    assert_eq!(response.stderr, "diagnostic");
    assert!(!response.is_success());
    let error = response.into_success("failed").unwrap_err();
    assert_eq!(error.kind, ErrorKind::Backend);
    assert!(error.to_string().contains("partial_failure"));
    assert_eq!(error.response.unwrap().payload, payload);
}

#[test]
fn zero_exit_does_not_override_business_failure() {
    let response = shell("printf '%s' '{\"schemaVersion\":1,\"command\":\"storage inspect\",\"ok\":false,\"data\":{\"result\":\"partial\"}}'", limits()).unwrap();
    assert!(response.status.success());
    assert!(!response.is_success());
    assert!(response.into_success("failed").is_err());
}

#[test]
fn nonzero_exit_does_not_become_a_successful_mutation() {
    let response = shell("printf '%s' '{\"schemaVersion\":1,\"command\":\"storage inspect\",\"ok\":true,\"data\":{}}'; exit 3", limits()).unwrap();
    assert!(!response.is_success());
    let error = response.into_success("failed").unwrap_err();
    assert_eq!(error.response.unwrap().status.code(), Some(3));
}

#[test]
fn legacy_reports_and_envelopes_keep_their_original_shape() {
    for payload in [json!({"schemaVersion":1,"entries":[]}), json!({"needsTty":true}),
        json!({"schemaVersion":1,"command":"storage inspect","ok":true,"data":{},
            "warnings":[],"rules":[]})] {
        let response = shell(&format!("printf '%s' '{}'", payload), limits()).unwrap();
        assert_eq!(response.into_success("failed").unwrap(), payload);
    }
    // Valid legacy reports remain inspectable on a nonzero process exit too.
    let response = shell("printf '%s' '{\"schemaVersion\":1,\"results\":[]}'; exit 1", limits()).unwrap();
    assert_eq!(response.payload["results"], json!([]));
    assert!(!response.is_success());
}

#[test]
fn validates_exact_schema_and_command_without_prefix_matches() {
    let valid = json!({"schemaVersion":1,"command":"storage inspect","ok":true,"data":{}});
    for (field, value) in [("schemaVersion", json!(2)), ("schemaVersion", json!(1.0)),
        ("command", json!("storage inspection")), ("command", json!("storage.inspect")),
        ("ok", json!("false")), ("warnings", json!({}))] {
        let mut payload = valid.clone();
        payload[field] = value;
        assert_eq!(validate_payload(&payload, "storage inspect").unwrap_err().kind, ErrorKind::Protocol);
    }
    for field in ["schemaVersion", "command", "ok", "data"] {
        let mut payload = valid.clone();
        payload.as_object_mut().unwrap().remove(field);
        assert!(validate_payload(&payload, "storage inspect").is_err());
    }
    assert!(validate_payload(&json!({"schemaVersion":2,"entries":[]}), "log").is_err());
}

#[test]
fn rejects_empty_truncated_multiple_and_non_object_json() {
    for text in ["", "{", "{}{}", "[]", "null", "1", "log noise\n{}"] {
        let error = shell(&format!("printf '%s' '{text}'"), limits()).unwrap_err();
        assert_eq!(error.kind, ErrorKind::Protocol, "{text}");
    }
    assert_eq!(shell("printf '\\377'", limits()).unwrap_err().kind, ErrorKind::Protocol);
}

#[test]
fn startup_failure_is_distinct() {
    let error = run_json(&mut Command::new("/synthetic-envy-missing-binary"), "status", limits()).unwrap_err();
    assert_eq!(error.kind, ErrorKind::Startup);
}

#[test]
fn stdout_and_stderr_limits_are_enforced_separately() {
    let mut small = limits();
    small.stdout_bytes = 2;
    small.stderr_bytes = 2;
    assert!(shell("printf '{}'; printf ab >&2", small).is_ok());
    for (script, stream) in [("printf '{} '", "stdout"), ("printf '{}'; printf abc >&2", "stderr")] {
        let error = shell(script, small).unwrap_err();
        assert_eq!(error.kind, ErrorKind::OutputLimit);
        assert!(error.message.contains(stream));
    }
}

#[test]
fn drains_both_streams_without_pipe_buffer_deadlock() {
    let mut large = limits();
    large.stdout_bytes = 256 * 1024;
    large.stderr_bytes = 256 * 1024;
    let response = shell("i=0; while [ $i -lt 12000 ]; do printf '            '; printf 'diagnostic  ' >&2; i=$((i+1)); done; printf '{}'", large).unwrap();
    assert_eq!(response.payload, json!({}));
    assert_eq!(response.stderr.len(), 144000);
}

#[test]
fn null_stdin_prevents_interactive_prompts() {
    assert!(shell("if read value; then exit 9; fi; printf '{}'", limits()).unwrap().is_success());
}

#[test]
fn timeout_terminates_a_child_that_ignores_term() {
    let mut short = limits();
    short.timeout = Duration::from_millis(80);
    let started = Instant::now();
    let error = shell("trap '' TERM; while :; do :; done", short).unwrap_err();
    assert_eq!(error.kind, ErrorKind::Timeout);
    assert!(started.elapsed() < Duration::from_secs(1));
}

#[test]
fn descendant_output_handles_cannot_hold_the_request_open() {
    let mut short = limits();
    short.timeout = Duration::from_millis(80);
    let started = Instant::now();
    // The shell exits immediately; its short-lived child inherits both streams.
    let error = shell("sleep 0.4 & printf '{}'", short).unwrap_err();
    assert_eq!(error.kind, ErrorKind::Timeout);
    assert!(started.elapsed() < Duration::from_secs(1));
}

#[test]
fn continuous_output_cannot_starve_timeout_or_memory_limits() {
    let error = shell("while :; do printf '0123456789'; done", limits()).unwrap_err();
    assert_eq!(error.kind, ErrorKind::OutputLimit);
}


#[test]
fn controlled_cancel_preserves_live_stderr_and_stops_inherited_handles() {
    let cancelled = AtomicBool::new(false);
    let mut progress = String::new();
    let started = Instant::now();
    let error = run_json_controlled(
        Command::new("sh").args(["-c", "trap '' TERM; printf 'building fixture\n' >&2; sleep 2 & wait"]),
        "plan", limits(), &cancelled,
        |chunk| { progress.push_str(chunk); cancelled.store(true, Ordering::Relaxed); },
    ).unwrap_err();
    assert_eq!(error.kind, ErrorKind::Cancelled);
    assert!(progress.contains("building fixture"));
    assert!(error.stderr.contains("building fixture"));
    assert!(started.elapsed() < Duration::from_secs(1));
}

#[test]
fn real_plan_producer_fixture_survives_transport_with_progress_and_nonzero_status() {
    let fixture: Value = serde_json::from_str(include_str!("../workflow/fixtures/plan-v1.json")).unwrap();
    let encoded = fixture.to_string();
    let mut progress = String::new();
    let mut bound = limits();
    bound.stdout_bytes = 64 * 1024;
    let response = run_json_controlled(
        Command::new("sh").args(["-c", "printf 'freeze\nbuild\ncompare\n' >&2; printf '%s' \"$1\"; exit 7", "fixture", &encoded]),
        "plan", bound, &AtomicBool::new(false), |chunk| progress.push_str(chunk),
    ).unwrap();
    assert_eq!(response.payload, fixture);
    assert_eq!(response.status.code(), Some(7));
    assert!(!response.is_success());
    assert_eq!(response.stderr, progress);
    assert!(progress.contains("build"));
    assert_eq!(response.payload["data"]["sections"]["settings"]["status"], "unknown");
    assert_eq!(response.payload["data"]["activation"]["actions"][0]["realized"], "unknown");
}
