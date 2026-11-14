#[cfg(feature = "test-fault-injection")]
pub(crate) fn hit(name: &str) {
    use std::io::Write;

    if std::env::var("LISTARY_CORE_TEST_FAILPOINT").ok().as_deref() != Some(name) {
        return;
    }

    if let Some(marker) = std::env::var_os("LISTARY_CORE_TEST_FAILPOINT_MARKER") {
        let marker = std::path::PathBuf::from(marker);
        let mut file = std::fs::File::create(&marker).unwrap_or_else(|error| {
            panic!(
                "failed to create failpoint marker {}: {error}",
                marker.display()
            )
        });
        file.write_all(name.as_bytes()).unwrap_or_else(|error| {
            panic!(
                "failed to write failpoint marker {}: {error}",
                marker.display()
            )
        });
        file.sync_all().unwrap_or_else(|error| {
            panic!(
                "failed to sync failpoint marker {}: {error}",
                marker.display()
            )
        });
    }

    std::process::exit(86);
}

#[cfg(not(feature = "test-fault-injection"))]
#[inline(always)]
pub(crate) fn hit(_name: &str) {}
