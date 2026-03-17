/**
 * Wactorz Node.js runtime entry point.
 *
 * Starts all agents and connects to MQTT broker.
 *
 * Environment:
 *   MQTT_URL        MQTT broker URL (default: mqtt://localhost:1883)
 *   MQTT_CLIENT_ID  Client ID (default: wactorz-node-<random>)
 */

import { ActorSystem } from "./core/registry";
import { IOAgent }        from "./agents/io_agent";
import { MonitorAgent }   from "./agents/monitor_agent";
import { UdxAgent }       from "./agents/udx_agent";
import { WeatherAgent }   from "./agents/weather_agent";
import { NewsAgent }      from "./agents/news_agent";
import { WifAgent }       from "./agents/wif_agent";
import { WizAgent }       from "./agents/wiz_agent";
import { QAAgent }        from "./agents/qa_agent";
import { NautilusAgent }  from "./agents/nautilus_agent";
import { FusekiAgent }    from "./agents/fuseki_agent";
import { TickAgent }      from "./agents/tick_agent";
import { HomeAssistantAgent } from "./agents/ha_agent";

const MQTT_URL = process.env["MQTT_URL"] ?? "mqtt://localhost:1883";

async function main() {
  console.log("[wactorz-node] Starting...");

  const system = new ActorSystem({
    mqttUrl: MQTT_URL,
    clientId: process.env["MQTT_CLIENT_ID"],
  });

  const pub = system.getPublisher();

  // Instantiate agents
  const io       = new IOAgent(pub, system.registry);
  const monitor  = new MonitorAgent(pub, system.registry);
  const udx      = new UdxAgent(pub, system.registry);
  const weather  = new WeatherAgent(pub);
  const news     = new NewsAgent(pub);
  const wif      = new WifAgent(pub);
  const wiz      = new WizAgent(pub);
  const qa       = new QAAgent(pub);
  const nautilus = new NautilusAgent(pub);
  const fuseki   = new FusekiAgent(pub);
  const tick     = new TickAgent(pub);
  const ha       = new HomeAssistantAgent(pub);

  // Connect to MQTT
  await system.connect();
  console.log(`[wactorz-node] Connected to MQTT at ${MQTT_URL}`);

  // Spawn all agents
  for (const agent of [io, monitor, udx, weather, news, wif, wiz, qa, nautilus, fuseki, tick, ha]) {
    system.spawnActor(agent);
    console.log(`[wactorz-node] Spawned ${agent.name}`);
  }

  // Graceful shutdown
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, async () => {
      console.log(`\n[wactorz-node] ${sig} received, shutting down...`);
      await system.shutdown();
      process.exit(0);
    });
  }

  console.log("[wactorz-node] All agents running. Press Ctrl-C to stop.");
}

main().catch((e) => {
  console.error("[wactorz-node] Fatal:", e);
  process.exit(1);
});
