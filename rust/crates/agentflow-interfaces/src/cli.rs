//! Interactive command-line interface.
//!
//! The CLI reads lines from stdin and dispatches them to the `MainActor`
//! mailbox.  Special commands (prefixed with `/`) control the system:
//!
//! | Command | Effect |
//! |---------|--------|
//! | `/list` | List all registered actors and their states |
//! | `/stop <name>` | Stop the named actor |
//! | `/status` | Print system health summary |
//! | `/quit` | Gracefully shut down the entire system |

use anyhow::Result;
use tokio::io::{AsyncBufReadExt, BufReader};

use agentflow_core::{ActorSystem, Message};

/// Start the interactive CLI loop.
///
/// Reads from stdin asynchronously and dispatches messages to `system`.
/// Returns when the user types `/quit` or stdin is closed.
pub async fn run_cli(system: ActorSystem) -> Result<()> {
    let stdin = tokio::io::stdin();
    let mut reader = BufReader::new(stdin).lines();

    println!("AgentFlow CLI — type a message or /help for commands.");

    while let Some(line) = reader.next_line().await? {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        if let Some(cmd) = trimmed.strip_prefix('/') {
            handle_command(cmd, &system).await?;
            if cmd.trim() == "quit" {
                break;
            }
        } else {
            dispatch_to_main_actor(trimmed, &system).await?;
        }
    }

    Ok(())
}

/// Parse and execute a `/`-prefixed CLI command.
async fn handle_command(cmd: &str, system: &ActorSystem) -> Result<()> {
    let parts: Vec<&str> = cmd.splitn(2, ' ').collect();
    match parts.as_slice() {
        ["help"] => {
            println!("Commands: /list, /stop <name>, /status, /quit");
        }
        ["list"] => {
            let actors = system.registry.list().await;
            for entry in actors {
                println!("  {:<20} {} ({})", entry.name, entry.id, entry.state);
            }
        }
        ["stop", name] => {
            system.stop_actor(name).await?;
            println!("Sent stop signal to '{name}'");
        }
        ["status"] => {
            let actors = system.registry.list().await;
            println!("System status: {} actor(s)", actors.len());
            for e in &actors {
                println!("  {} {} — {}", e.name, e.id, e.state);
            }
        }
        ["quit"] => {
            println!("Shutting down…");
            system.shutdown().await?;
        }
        _ => {
            println!("Unknown command: /{cmd}. Type /help.");
        }
    }
    Ok(())
}

/// Send a plain text message to the MainActor.
async fn dispatch_to_main_actor(text: &str, system: &ActorSystem) -> Result<()> {
    let entry = system
        .registry
        .get_by_name("main-actor")
        .await
        .ok_or_else(|| anyhow::anyhow!("main-actor not found"))?;
    let msg = Message::text(None, Some(entry.id.clone()), text);
    system.registry.send(&entry.id, msg).await
}
