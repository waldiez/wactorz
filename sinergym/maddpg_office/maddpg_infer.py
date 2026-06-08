"""
maddpg_infer.py — training-free inference for the MADDPG OfficeMedium policy.

Extracted verbatim from maddpg_v3.py so the math (model architecture, feature
builder, observation normalizer, [0,1] action squashing) matches training
exactly. Contains NO training machinery: no critic, no replay buffer, no
optimizer, no gym/env, no ensemble_controller import. Only numpy + torch.

Deploy by placing this file on the wactorz process's import path, alongside the
trained checkpoint (model.pt) and the frozen normalizer (normalizer.npz).

Per-zone usage (one ZonePolicy per actor, decentralized execution):

    from maddpg_infer import ObservationBuilder, ZonePolicy

    ob  = ObservationBuilder()                 # one per agent; tracks prev temps
    pol = ZonePolicy("model.pt", "normalizer.npz", zone_index=k)

    # on each global observation message:
    agent_obs = ob.build_agent_obs(obs_raw)    # (15, 29)
    norm_action = pol.step(agent_obs[k])       # -> np.array([htg, clg]) in [-1, 1]
    # publish norm_action to sinergym/env/{env_id}/zone/{zone}/action ; the bridge
    # denormalizes [-1,1] -> real setpoints itself.

    # on episode boundary (done / episode change):
    ob.reset(); pol.reset()
"""

from collections import deque

import numpy as np
import torch
import torch.nn as nn

# =============================================================================
# CONSTANTS (mirror maddpg_v3.py)
# =============================================================================
N_OCCUPIED      = 15
AGENT_OBS_DIM   = 29
SEQ_LEN         = 8
GRU_HIDDEN      = 64
ACTOR_HIDDEN    = 128

IDX_HVAC_DEMAND = 90
IDX_METER       = 91

ZONE_META = {
    0:  {"type": "core",      "floor": "bottom", "orient": "none"},
    1:  {"type": "core",      "floor": "mid",    "orient": "none"},
    2:  {"type": "core",      "floor": "top",    "orient": "none"},
    3:  {"type": "perimeter", "floor": "bottom", "orient": "south"},
    4:  {"type": "perimeter", "floor": "bottom", "orient": "east"},
    5:  {"type": "perimeter", "floor": "bottom", "orient": "north"},
    6:  {"type": "perimeter", "floor": "bottom", "orient": "west"},
    7:  {"type": "perimeter", "floor": "mid",    "orient": "south"},
    8:  {"type": "perimeter", "floor": "mid",    "orient": "east"},
    9:  {"type": "perimeter", "floor": "mid",    "orient": "north"},
    10: {"type": "perimeter", "floor": "mid",    "orient": "west"},
    11: {"type": "perimeter", "floor": "top",    "orient": "south"},
    12: {"type": "perimeter", "floor": "top",    "orient": "east"},
    13: {"type": "perimeter", "floor": "top",    "orient": "north"},
    14: {"type": "perimeter", "floor": "top",    "orient": "west"},
}
# NOTE: the authoritative ZONE_META lives in maddpg_v3.py. If you ever change it
# there, re-copy it here. The encodings below must be byte-identical to training
# or the actors receive a different zone identity than they learned.

ORIENT_ANGLES = {"none": 0, "north": 0, "east": 90, "south": 180, "west": 270}
FLOOR_MAP     = {"bottom": 0.0, "mid": 0.5, "top": 1.0}


def get_zone_encoding(zone_idx):
    meta = ZONE_META[zone_idx]
    is_core    = 1.0 if meta["type"] == "core" else 0.0
    floor_norm = FLOOR_MAP[meta["floor"]]
    angle_deg  = ORIENT_ANGLES[meta["orient"]]
    angle_rad  = np.radians(angle_deg)
    return np.array([is_core, floor_norm, np.sin(angle_rad), np.cos(angle_rad)],
                    dtype=np.float32)


ZONE_ENCODINGS = np.stack([get_zone_encoding(i) for i in range(N_OCCUPIED)])


# =============================================================================
# MODEL (architecture must match training exactly)
# =============================================================================
class ZoneGRUEncoder(nn.Module):
    def __init__(self, input_dim, hidden_dim):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.gru = nn.GRU(input_size=input_dim, hidden_size=hidden_dim,
                          batch_first=True)
        self.ln  = nn.LayerNorm(hidden_dim)

    def forward(self, x):
        x = x.contiguous()
        h0 = torch.zeros(1, x.size(0), self.hidden_dim,
                         device=x.device, dtype=x.dtype)
        output, _ = self.gru(x, h0)
        return self.ln(output[:, -1, :])


class ZoneActor(nn.Module):
    def __init__(self, obs_dim, gru_hidden=64, mlp_hidden=128):
        super().__init__()
        self.encoder = ZoneGRUEncoder(obs_dim, gru_hidden)
        self.head = nn.Sequential(
            nn.Linear(gru_hidden, mlp_hidden),
            nn.LayerNorm(mlp_hidden),
            nn.ReLU(),
            nn.Linear(mlp_hidden, mlp_hidden),
            nn.LayerNorm(mlp_hidden),
            nn.ReLU(),
            nn.Linear(mlp_hidden, 2),
        )

    def forward(self, seq):
        h = self.encoder(seq)
        return (torch.tanh(self.head(h)) + 1.0) / 2.0   # a01 in [0, 1]


# =============================================================================
# OBSERVATION NORMALIZER (frozen at inference)
# =============================================================================
class AgentObsNormalizer:
    def __init__(self, obs_dim=AGENT_OBS_DIM):
        self.obs_dim = obs_dim
        self.mean    = np.zeros(obs_dim, dtype=np.float64)
        self.var     = np.ones(obs_dim, dtype=np.float64)
        self.count   = 0
        self.frozen  = False

    def normalize(self, obs):
        if self.count < 2:
            return obs.astype(np.float32)
        std = np.sqrt(np.maximum(self.var / max(1, self.count - 1), 1e-4)) + 1e-8
        return ((obs.astype(np.float64) - self.mean) / std).astype(np.float32)

    def load(self, path):
        d = np.load(path)
        self.mean, self.var = d["mean"], d["var"]
        self.count, self.frozen = int(d["count"]), bool(d["frozen"])
        return self


# =============================================================================
# FEATURE BUILDER (must match maddpg_v3.ObservationBuilder exactly)
# =============================================================================
class ObservationBuilder:
    def __init__(self):
        self.prev_zone_temps = None

    def reset(self):
        self.prev_zone_temps = None

    def build_agent_obs(self, obs_raw):
        obs_raw = np.asarray(obs_raw, dtype=np.float32)
        month, day, hour = obs_raw[0], obs_raw[1], obs_raw[2]
        day_of_year = (month - 1) * 30.44 + day

        def sc(val, period):
            a = 2 * np.pi * val / period
            return np.sin(a), np.cos(a)

        h_s, h_c = sc(hour, 24)
        d_s, d_c = sc(day, 31)
        m_s, m_c = sc(month - 1, 12)
        y_s, y_c = sc(day_of_year, 365.25)

        try:
            from datetime import datetime
            dow = datetime(2024, int(month), int(day)).weekday()
            is_wknd = 1.0 if dow >= 5 else 0.0
        except Exception:
            is_wknd = 0.0
        is_occ = 1.0 if (is_wknd == 0 and 7 <= hour <= 19) else 0.0
        h_norm = hour / 23.0

        time_feats = np.array([h_s, h_c, d_s, d_c, m_s, m_c, y_s, y_c,
                               is_wknd, is_occ, h_norm], dtype=np.float32)
        weather = obs_raw[3:9].astype(np.float32)
        hvac    = np.array([obs_raw[IDX_HVAC_DEMAND], obs_raw[IDX_METER]],
                           dtype=np.float32)
        global_feats = np.concatenate([time_feats, weather, hvac])

        zone_temps     = obs_raw[9:24].astype(np.float32)
        zone_humidity  = obs_raw[27:42].astype(np.float32)
        zone_occupancy = obs_raw[45:60].astype(np.float32)

        htg_setpoints = np.array([obs_raw[60 + i * 2]     for i in range(N_OCCUPIED)], dtype=np.float32)
        clg_setpoints = np.array([obs_raw[60 + i * 2 + 1] for i in range(N_OCCUPIED)], dtype=np.float32)

        if self.prev_zone_temps is not None:
            temp_deltas = zone_temps - self.prev_zone_temps
        else:
            temp_deltas = np.zeros(N_OCCUPIED, dtype=np.float32)
        self.prev_zone_temps = zone_temps.copy()

        agent_obs = np.zeros((N_OCCUPIED, AGENT_OBS_DIM), dtype=np.float32)
        for i in range(N_OCCUPIED):
            local = np.array([
                zone_temps[i], zone_humidity[i], zone_occupancy[i],
                htg_setpoints[i], clg_setpoints[i],
                ZONE_ENCODINGS[i, 0], ZONE_ENCODINGS[i, 1],
                ZONE_ENCODINGS[i, 2], ZONE_ENCODINGS[i, 3],
                temp_deltas[i],
            ], dtype=np.float32)
            agent_obs[i] = np.concatenate([global_feats, local])

        return agent_obs


# =============================================================================
# ZONE POLICY — one trained actor + its rolling window + normalizer
# =============================================================================
class ZonePolicy:
    """
    Inference wrapper for a single zone's actor (actor_{zone_index} in the
    checkpoint). Maintains a SEQ_LEN rolling window of that zone's 29-dim
    observation and returns the normalized [-1, 1] action the MAS bridge expects.
    """

    def __init__(self, model_path, normalizer_path, zone_index,
                 seq_len=SEQ_LEN, device="cpu"):
        self.zone_index = int(zone_index)
        self.seq_len    = int(seq_len)
        self.device     = torch.device(device)

        # Load only this zone's actor from the shared checkpoint.
        ckpt = torch.load(model_path, map_location=self.device)
        key  = f"actor_{self.zone_index}"
        if key not in ckpt:
            raise KeyError(
                f"'{key}' not in checkpoint {model_path}. "
                f"Available actor keys: {[k for k in ckpt if k.startswith('actor_') and k[6:].isdigit()]}"
            )
        self.actor = ZoneActor(AGENT_OBS_DIM, GRU_HIDDEN, ACTOR_HIDDEN).to(self.device)
        self.actor.load_state_dict(ckpt[key])
        self.actor.eval()
        del ckpt  # don't hold the critic / other actors in memory

        self.normalizer = AgentObsNormalizer(AGENT_OBS_DIM).load(normalizer_path)
        self.normalizer.frozen = True

        self.window = deque(maxlen=self.seq_len)

    def reset(self):
        """Clear the rolling window — call on episode boundaries."""
        self.window.clear()

    def step(self, agent_obs_row):
        """
        Push this zone's 29-dim observation and return the normalized action
        np.array([htg, clg]) in [-1, 1]. The window pads with its first frame
        until full, so the policy acts from the first step of an episode.
        """
        row = np.asarray(agent_obs_row, dtype=np.float32)
        self.window.append(row)

        frames = list(self.window)
        if len(frames) < self.seq_len:               # warm-up: left-pad with first frame
            frames = [frames[0]] * (self.seq_len - len(frames)) + frames
        seq = np.stack(frames, axis=0)                # (seq_len, 29)

        seq_norm = self.normalizer.normalize(seq)     # broadcast over time dim
        seq_t = torch.from_numpy(seq_norm).unsqueeze(0).to(self.device)  # (1, seq_len, 29)
        with torch.no_grad():
            a01 = self.actor(seq_t).cpu().numpy()[0]  # [0, 1]^2

        norm_action = np.clip(2.0 * a01 - 1.0, -1.0, 1.0).astype(np.float32)
        return norm_action
