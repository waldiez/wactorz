use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;

use agentflow_core::backend_parity::{expected_report, load_fixture, run_fixture};

#[derive(Debug, Parser)]
struct Args {
    #[arg(long)]
    fixture: PathBuf,
    #[arg(long, default_value_t = false)]
    assert_expected: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let actual = run_fixture(&args.fixture).await?;
    if args.assert_expected {
        let fixture = load_fixture(&args.fixture)?;
        let expected = expected_report(&fixture);
        anyhow::ensure!(actual == expected, "rust backend parity output did not match fixture");
    }
    println!("{}", serde_json::to_string_pretty(&actual)?);
    Ok(())
}
