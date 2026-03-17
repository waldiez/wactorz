/**
 * WeatherAgent — current weather via wttr.in (Node.js port).
 * No API key required. agentType: "data"
 */

import axios from "axios";
import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

const DEFAULT_LOCATION = process.env["WEATHER_DEFAULT_LOCATION"] ?? "London";

function urlEncode(location: string): string {
  return location
    .split("")
    .map((ch) => (ch === " " ? "+" : encodeURIComponent(ch)))
    .join("");
}

export class WeatherAgent extends Actor {
  private _defaultLocation: string;

  constructor(publish: MqttPublisher, actorId?: string) {
    super("weather-agent", publish, actorId);
    this._defaultLocation = DEFAULT_LOCATION;
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("data");
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    text = Actor.stripPrefix(text, "@weather-agent", "@weather_agent");

    if (text.toLowerCase() === "help") {
      this.replyChat(
        `**WeatherAgent** — current conditions via wttr.in (no API key needed)\n\n` +
        "```\n" +
        `@weather-agent              # ${this._defaultLocation} (default)\n` +
        "@weather-agent Tokyo\n" +
        "@weather-agent New York\n" +
        "```\n" +
        "Set `WEATHER_DEFAULT_LOCATION` in env to change the default."
      );
      return;
    }

    const location = text.trim() || this._defaultLocation;
    this.replyChat(`🌦 Fetching weather for **${location}**...`);
    const result = await this._fetch(location);
    this.replyChat(result);
    this.metrics.tasksCompleted++;
  }

  private async _fetch(location: string): Promise<string> {
    const encoded = urlEncode(location);
    const url = `https://wttr.in/${encoded}?format=j1`;
    try {
      const resp = await axios.get(url, {
        timeout: 10_000,
        headers: { "User-Agent": "Wactorz-WeatherAgent/1.0" },
        validateStatus: (s) => s < 500,
      });
      if (resp.status !== 200) {
        // fallback: one-line format
        const r2 = await axios.get(`https://wttr.in/${encoded}?format=3`, {
          timeout: 10_000,
          headers: { "User-Agent": "Wactorz-WeatherAgent/1.0" },
        });
        return String(r2.data).trim();
      }
      return this._format(resp.data as Record<string, unknown>, location, encoded);
    } catch (e) {
      return `⚠ Could not fetch weather for '${location}': ${e}`;
    }
  }

  private _format(data: Record<string, unknown>, location: string, encoded: string): string {
    try {
      const cc = (data["current_condition"] as Record<string, unknown>[])[0];
      const desc = ((cc["weatherDesc"] as { value: string }[])[0]?.value) ?? "N/A";
      const area =
        ((data["nearest_area"] as Record<string, unknown>[])[0]?.["areaName"] as { value: string }[])[0]?.value ??
        location;

      return (
        `**Weather in ${area}**\n\n` +
        `🌡 **${cc["temp_C"]}°C / ${cc["temp_F"]}°F** (feels like ${cc["FeelsLikeC"]}°C)\n` +
        `☁ ${desc}\n` +
        `💧 Humidity: ${cc["humidity"]}%\n` +
        `💨 Wind: ${cc["windspeedKmph"]} km/h ${cc["winddir16Point"]}\n` +
        `👁 Visibility: ${cc["visibility"]} km\n` +
        `☀ UV index: ${cc["uvIndex"]}\n\n` +
        `*Data: [wttr.in](https://wttr.in/${encoded})*`
      );
    } catch {
      return `Received data for **${location}** but could not parse it.`;
    }
  }
}
