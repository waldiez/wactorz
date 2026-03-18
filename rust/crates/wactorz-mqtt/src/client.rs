//! Async MQTT client wrapper.
//!
//! [`MqttClient`] wraps `rumqttc::AsyncClient` and its event loop, exposing
//! ergonomic `publish` / `subscribe` helpers that work directly with
//! [`wactorz_core::Message`] values (serialised as JSON).

use anyhow::Result;
use rumqttc::{AsyncClient, EventLoop, QoS};
use serde::{Deserialize, Serialize};
use tracing::debug;

use wactorz_core::Message;

/// Connection parameters for the MQTT broker.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MqttConfig {
    /// Broker hostname or IP address.
    pub host: String,
    /// Standard MQTT port (default 1883) or TLS port (8883).
    pub port: u16,
    /// Client identifier sent to the broker.
    pub client_id: String,
    /// Optional username for broker authentication.
    pub username: Option<String>,
    /// Optional password for broker authentication.
    pub password: Option<String>,
    /// Keep-alive interval in seconds.
    pub keep_alive_secs: u64,
    /// WebSocket port (for browser clients, default 9001).
    pub ws_port: u16,
}

impl Default for MqttConfig {
    fn default() -> Self {
        Self {
            host: "localhost".into(),
            port: 1883,
            client_id: "wactorz-server".into(),
            username: None,
            password: None,
            keep_alive_secs: 30,
            ws_port: 9001,
        }
    }
}

/// Typed events surfaced by the MQTT event loop.
#[derive(Debug)]
pub enum MqttEvent {
    /// A message arrived on a subscribed topic.
    Incoming { topic: String, payload: Vec<u8> },
    /// The client successfully connected (or reconnected) to the broker.
    Connected,
    /// The client was cleanly disconnected.
    Disconnected,
}

/// Async MQTT client.
///
/// Internally owns both the `rumqttc::AsyncClient` (for publish/subscribe) and
/// the `rumqttc::EventLoop` (which must be polled continuously to keep the
/// connection alive).
pub struct MqttClient {
    inner: AsyncClient,
}

impl MqttClient {
    /// Create a new client and connect to the broker described by `config`.
    ///
    /// The returned `EventLoop` must be driven by calling [`MqttClient::run_event_loop`]
    /// or by polling it manually in a dedicated task.
    pub fn new(config: MqttConfig) -> Result<(Self, EventLoop)> {
        let mut opts = rumqttc::MqttOptions::new(&config.client_id, &config.host, config.port);
        opts.set_keep_alive(std::time::Duration::from_secs(config.keep_alive_secs));
        if let (Some(user), Some(pass)) = (&config.username, &config.password) {
            opts.set_credentials(user, pass);
        }
        opts.set_max_packet_size(256 * 1024, 256 * 1024);
        let (inner, event_loop) = rumqttc::AsyncClient::new(opts, 64);
        Ok((Self { inner }, event_loop))
    }

    /// Publish a serialised [`Message`] to the given topic.
    pub async fn publish_message(&self, topic: &str, message: &Message) -> Result<()> {
        let payload = serde_json::to_vec(message)?;
        self.inner
            .publish(topic, QoS::AtLeastOnce, false, payload)
            .await?;
        Ok(())
    }

    /// Publish a raw JSON payload to the given topic.
    pub async fn publish_json(&self, topic: &str, payload: &impl Serialize) -> Result<()> {
        let bytes = serde_json::to_vec(payload)?;
        self.inner
            .publish(topic, QoS::AtLeastOnce, false, bytes)
            .await?;
        Ok(())
    }

    /// Publish raw bytes to the given topic.
    pub async fn publish_raw(&self, topic: &str, payload: Vec<u8>) -> Result<()> {
        self.inner
            .publish(topic, QoS::AtLeastOnce, false, payload)
            .await?;
        Ok(())
    }

    /// Subscribe to a topic pattern (MQTT wildcards `+` and `#` supported).
    pub async fn subscribe(&self, topic: &str) -> Result<()> {
        self.inner.subscribe(topic, QoS::AtLeastOnce).await?;
        debug!(topic, "subscribed");
        Ok(())
    }

    /// Unsubscribe from a topic.
    pub async fn unsubscribe(&self, topic: &str) -> Result<()> {
        self.inner.unsubscribe(topic).await?;
        Ok(())
    }

    /// Drive the event loop, mapping raw `rumqttc` events to [`MqttEvent`]s
    /// and forwarding them to `handler`.
    ///
    /// This method loops forever; call it in a dedicated `tokio::spawn` task.
    pub async fn run_event_loop(
        event_loop: &mut EventLoop,
        mut handler: impl FnMut(MqttEvent) + Send + 'static,
    ) {
        use rumqttc::{Event, Packet};
        loop {
            match event_loop.poll().await {
                Ok(Event::Incoming(Packet::Publish(p))) => {
                    handler(MqttEvent::Incoming {
                        topic: p.topic,
                        payload: p.payload.to_vec(),
                    });
                }
                Ok(Event::Incoming(Packet::ConnAck(_))) => {
                    handler(MqttEvent::Connected);
                }
                Ok(Event::Incoming(Packet::Disconnect)) => {
                    handler(MqttEvent::Disconnected);
                    break;
                }
                Ok(_) => {} // PingReq, PubAck, SubAck etc — ignore
                Err(e) => {
                    tracing::error!("MQTT event loop error: {e}");
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                }
            }
        }
    }
}
