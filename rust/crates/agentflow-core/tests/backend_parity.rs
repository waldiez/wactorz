use anyhow::Result;

use agentflow_core::backend_parity::{expected_report, load_fixture, run_fixture};

#[tokio::test]
async fn backend_supervisor_contract_matches_fixture() -> Result<()> {
    let fixture_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/parity_fixtures/backend_supervisor_parity.json");
    let fixture = load_fixture(&fixture_path)?;
    let expected = expected_report(&fixture);
    let actual = run_fixture(&fixture_path).await?;
    assert_eq!(actual, expected);
    Ok(())
}
