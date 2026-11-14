use listary_agent::{
    AGENT_PROTOCOL_VERSION, AgentServer, DEFAULT_ENDPOINT_FILE, load_endpoint, send_request,
};
use listary_core::{CORE_SCHEMA_VERSION, Core, CoreConfig};
use serde_json::{Value, json};
use std::{env, path::PathBuf};
use uuid::Uuid;

const SELF_CHECK_SCHEMA: &str = "spiffy_listary_agent_self_check";
const SELF_CHECK_SCHEMA_VERSION: u32 = 1;

fn main() {
    if let Err(error) = run() {
        eprintln!("listary-agent: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut state_dir = default_state_dir()?;
    let mut workspace_catalog = None;
    let mut http_api = false;
    let mut stop = false;
    let mut status = false;
    let mut self_check = false;
    let mut recover_remove_root = None::<Uuid>;
    let mut expected_root_path = None::<PathBuf>;
    let mut args = env::args().skip(1);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--state-dir" => {
                state_dir = PathBuf::from(args.next().ok_or("--state-dir requires a path")?);
            }
            "--workspace-catalog" => {
                workspace_catalog = Some(PathBuf::from(
                    args.next().ok_or("--workspace-catalog requires a path")?,
                ));
            }
            "--http-api" => http_api = true,
            "--stop" => stop = true,
            "--status" => status = true,
            "--self-check" => self_check = true,
            "--recover-remove-root" => {
                recover_remove_root = Some(
                    args.next()
                        .ok_or("--recover-remove-root requires a UUID")?
                        .parse()?,
                );
            }
            "--expected-root-path" => {
                expected_root_path = Some(PathBuf::from(
                    args.next().ok_or("--expected-root-path requires a path")?,
                ));
            }
            "--help" | "-h" => {
                println!(
                    "listary-agent [--state-dir PATH] [--workspace-catalog PATH] [--http-api] [--status | --stop]\n\
                     listary-agent --self-check\n\
                     listary-agent [--state-dir PATH] --recover-remove-root UUID --expected-root-path PATH\n\
                     Keeps one warm listary-core instance available to local Spiffy integrations."
                );
                return Ok(());
            }
            other => return Err(format!("unknown argument: {other}").into()),
        }
    }

    let control_modes = usize::from(stop)
        + usize::from(status)
        + usize::from(self_check)
        + usize::from(recover_remove_root.is_some());
    if control_modes > 1 {
        return Err(
            "--stop, --status, --self-check, and --recover-remove-root are mutually exclusive"
                .into(),
        );
    }
    if workspace_catalog.is_some()
        && (stop || status || self_check || recover_remove_root.is_some())
    {
        return Err("--workspace-catalog is valid only when starting the agent".into());
    }
    if http_api && (stop || status || self_check || recover_remove_root.is_some()) {
        return Err("--http-api is valid only when starting the agent".into());
    }
    if recover_remove_root.is_none() && expected_root_path.is_some() {
        return Err("--expected-root-path requires --recover-remove-root".into());
    }
    if self_check {
        println!("{}", serde_json::to_string_pretty(&self_check_report())?);
        return Ok(());
    }
    if let Some(root_id) = recover_remove_root {
        let expected_path = expected_root_path
            .as_deref()
            .ok_or("--recover-remove-root requires --expected-root-path")?;
        let report = Core::recover_remove_root(CoreConfig::new(state_dir), root_id, expected_path)?;
        println!("{}", serde_json::to_string_pretty(&report)?);
        return Ok(());
    }
    let endpoint_path = state_dir.join(DEFAULT_ENDPOINT_FILE);
    if stop || status {
        let endpoint = load_endpoint(&endpoint_path)?;
        let response = send_request(
            &endpoint,
            if stop { "shutdown" } else { "status" },
            None,
            Value::Null,
        )?;
        println!("{}", serde_json::to_string_pretty(&response)?);
        return Ok(());
    }

    let server = AgentServer::bind_with_workspace_catalog_and_http_api(
        CoreConfig::new(state_dir),
        Some(endpoint_path),
        workspace_catalog,
        http_api,
    )?;
    let endpoint = server.endpoint()?;
    println!(
        "listary-agent listening on {} (protocol {})",
        endpoint.address, endpoint.protocol_version
    );
    server.run()?;
    Ok(())
}

fn self_check_report() -> Value {
    json!({
        "schema": SELF_CHECK_SCHEMA,
        "schema_version": SELF_CHECK_SCHEMA_VERSION,
        "binary_role": "listary-agent",
        "package_version": env!("CARGO_PKG_VERSION"),
        "agent_protocol_version": AGENT_PROTOCOL_VERSION,
        "core_schema_version": CORE_SCHEMA_VERSION,
        "supports_status": true,
        "supports_shutdown": true,
        "persistent_state_accessed": false,
        "resident_started": false
    })
}

fn default_state_dir() -> Result<PathBuf, Box<dyn std::error::Error>> {
    #[cfg(target_os = "windows")]
    {
        let local = env::var_os("LOCALAPPDATA").ok_or("LOCALAPPDATA is not set")?;
        Ok(PathBuf::from(local).join("ListaryMac").join("state"))
    }
    #[cfg(target_os = "macos")]
    {
        let home = env::var_os("HOME").ok_or("HOME is not set")?;
        return Ok(PathBuf::from(home)
            .join("Library")
            .join("Application Support")
            .join("ListaryMac")
            .join("state"));
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let home = env::var_os("HOME").ok_or("HOME is not set")?;
        Ok(PathBuf::from(home)
            .join(".local")
            .join("share")
            .join("spiffy-listary")
            .join("state"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn self_check_report_is_exact_and_non_live() {
        let report = self_check_report();
        assert_eq!(report["schema"], SELF_CHECK_SCHEMA);
        assert_eq!(report["schema_version"], SELF_CHECK_SCHEMA_VERSION);
        assert_eq!(report["binary_role"], "listary-agent");
        assert_eq!(report["package_version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(report["agent_protocol_version"], AGENT_PROTOCOL_VERSION);
        assert_eq!(report["core_schema_version"], CORE_SCHEMA_VERSION);
        assert_eq!(report["supports_status"], true);
        assert_eq!(report["supports_shutdown"], true);
        assert_eq!(report["persistent_state_accessed"], false);
        assert_eq!(report["resident_started"], false);
        assert_eq!(report.as_object().expect("object").len(), 10);
    }
}
