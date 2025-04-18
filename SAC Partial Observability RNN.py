import numpy as np
from numpy.ma.core import arctan2
from scipy.integrate import solve_ivp
import gymnasium as gym
from gymnasium import spaces
from stable_baselines3 import SAC
from stable_baselines3.common.vec_env import DummyVecEnv
from stable_baselines3.common.callbacks import BaseCallback
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.sac.policies import SACPolicy
from torch.distributions import Normal

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device}")

# Custom RNN Feature Extractor (unchanged)
class RNNFeaturesExtractor(BaseFeaturesExtractor):
    def __init__(self, observation_space: gym.spaces.Box, features_dim: int = 64, hidden_dim: int = 64):
        super(RNNFeaturesExtractor, self).__init__(observation_space, features_dim)
        self.hidden_dim = hidden_dim
        self.rnn = nn.LSTM(
            input_size=observation_space.shape[0],
            hidden_size=hidden_dim,
            num_layers=1,
            batch_first=True
        )
        self.fc = nn.Linear(hidden_dim, features_dim)
        self.hidden = None

    def reset_hidden(self):
        self.hidden = None

    def forward(self, observations: torch.Tensor) -> torch.Tensor:
        if len(observations.shape) == 2:
            observations = observations.unsqueeze(1)

        batch_size, seq_len, _ = observations.shape
        if self.hidden is None or self.hidden[0].shape[1] != batch_size:
            self.hidden = (torch.zeros(1, batch_size, self.hidden_dim).to(device),
                           torch.zeros(1, batch_size, self.hidden_dim).to(device))

        out, self.hidden = self.rnn(observations, self.hidden)
        out = out[:, -1, :]
        return self.fc(out)

# Custom Actor Network (inspired by friend's code)
class Actor(nn.Module):
    def __init__(self, features_dim, action_dim, hidden_dim=256):
        super(Actor, self).__init__()
        self.net = nn.Sequential(
            nn.Linear(features_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU()
        )
        self.mean = nn.Linear(hidden_dim, action_dim)
        self.log_std = nn.Linear(hidden_dim, action_dim)
        self.to(device)

    def forward(self, features):
        x = self.net(features)
        mean = torch.tanh(self.mean(x))  # Output in [-1, 1]
        log_std = self.log_std(x)
        log_std = torch.clamp(log_std, -20, 2)
        return mean, log_std

    def sample(self, features):
        mean, log_std = self.forward(features)
        std = log_std.exp()
        normal = Normal(mean, std)
        x = normal.rsample()
        action = torch.tanh(x)
        log_prob = normal.log_prob(x) - torch.log(1 - action.pow(2) + 1e-6)
        log_prob = log_prob.sum(1, keepdim=True)
        return action, log_prob

# Custom Critic Network (inspired by friend's code)
class Critic(nn.Module):
    def __init__(self, features_dim, action_dim, hidden_dim=256):
        super(Critic, self).__init__()
        self.q1_net = nn.Sequential(
            nn.Linear(features_dim + action_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1)
        )
        self.q2_net = nn.Sequential(
            nn.Linear(features_dim + action_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1)
        )
        self.to(device)

    def forward(self, features, action):
        x = torch.cat([features, action], dim=-1)
        q1 = self.q1_net(x)
        q2 = self.q2_net(x)
        return q1, q2

# Custom SAC Policy
class CustomSACPolicy(SACPolicy):
    def __init__(self, observation_space, action_space, lr_schedule, *args, **kwargs):
        super(CustomSACPolicy, self).__init__(
            observation_space,
            action_space,
            lr_schedule,
            features_extractor_class=RNNFeaturesExtractor,
            features_extractor_kwargs=dict(features_dim=64, hidden_dim=64),
            *args,
            **kwargs
        )

    def _build(self, lr_schedule):
        self.features_dim = 64
        self.action_dim = self.action_space.shape[0]

        # Actor
        self.actor = Actor(self.features_dim, self.action_dim)

        # Critic
        self.critic = Critic(self.features_dim, self.action_dim)
        self.critic_target = Critic(self.features_dim, self.action_dim)
        self.critic_target.load_state_dict(self.critic.state_dict())

        # Optimizers (Stable-Baselines3 will override these, but we define for clarity)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=lr_schedule(1))
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=lr_schedule(1))

    def reset(self):
        self.features_extractor.reset_hidden()

    def _predict(self, observation, deterministic=False):
        features = self.features_extractor(observation)
        if deterministic:
            mean, _ = self.actor(features)
            return mean
        else:
            action, _ = self.actor.sample(features)
            return action * torch.tensor(self.action_space.high, device=device)

class TrainingMonitorCallback(BaseCallback):
    def __init__(self, check_freq=1000, patience=10, loss_threshold=0.01, verbose=1):
        super(TrainingMonitorCallback, self).__init__(verbose)
        self.check_freq = check_freq
        self.patience = patience
        self.loss_threshold = loss_threshold
        self.loss_change = 0.005
        self.total_losses = []
        self.rewards = []
        self.episode_rewards = []
        self.episode_lengths = []
        self.theta1_values = []

    def _on_step(self):
        reward = self.locals['rewards'][0]
        self.rewards.append(reward)

        obs = self.locals['new_obs'][0]
        s1, c1 = obs[3], obs[4]
        theta1 = np.arctan2(s1, c1)
        self.theta1_values.append(theta1)

        if self.locals['dones'][0]:
            self.episode_rewards.append(sum(self.rewards))
            self.episode_lengths.append(len(self.rewards))
            self.rewards = []
            self.model.policy.reset()

        if self.n_calls % self.check_freq == 0:
            logger = self.model.logger
            actor_loss = logger.name_to_value.get('train/actor_loss', np.nan)
            critic_loss = logger.name_to_value.get('train/critic_loss', np.nan)
            total_loss = np.nanmean([actor_loss, critic_loss]) if not (np.isnan(actor_loss) or np.isnan(critic_loss)) else np.nan

            if not np.isnan(total_loss):
                self.total_losses.append(total_loss)
                if len(self.total_losses) > self.patience:
                    recent_losses = self.total_losses[-self.patience:]
                    loss_change = np.abs(np.mean(np.diff(recent_losses)))
                    avg_loss = np.mean(recent_losses)
                    if abs(avg_loss) < self.loss_threshold or loss_change < self.loss_change:
                        print(f"Loss converged: Avg = {avg_loss:.4f}, Change = {loss_change:.4f}. Stopping.")
                        return False

            avg_reward = np.mean(self.episode_rewards[-10:]) if self.episode_rewards else 0
            avg_theta1 = np.mean(np.abs(np.degrees(self.theta1_values[-self.check_freq:])))
            success_rate = np.mean(np.abs(self.theta1_values[-self.check_freq:]) > np.pi / 2)

            print(f"Step {self.n_calls}")
            self.logger.record('custom/avg_episode_reward', avg_reward)
            self.logger.record('custom/avg_theta1_degrees', avg_theta1)
            self.logger.record('custom/success_rate', success_rate)
            self.logger.record('custom/combined_loss', total_loss)
            self.logger.dump(step=self.n_calls)

        return True

class QubeServo2Env(gym.Env):
    def __init__(self):
        super().__init__()
        self.action_limit = 6.0
        self.action_space = spaces.Box(low=-self.action_limit, high=self.action_limit, shape=(1,), dtype=np.float32)
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(5,), dtype=np.float32)
        self.state = None
        self.dt = 0.01
        self.max_steps = 2000
        self.step_count = 0
        self.max_theta_0 = 5.0 * np.pi / 6.0

        self.nominal_params = {
            'm_1': 0.024, 'l_1': 0.128 / 2, 'I_1': 0.0000235,
            'm_0': 0.053, 'L_0': 0.086, 'I_0': 0.0000572 + 0.00006,
            'g': 9.81, 'b_0': 0.0004, 'b_1': 0.000003, 'k': 0.002,
            'K_m': 0.0431, 'R_m': 8.94
        }
        self.params = self.nominal_params.copy()

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        if seed is not None:
            np.random.seed(seed)

        self.params = self.nominal_params.copy()
        for key in self.params:
            self.params[key] *= np.random.uniform(0.8, 1.2) if key != 'g' else np.random.uniform(0.95, 1.05)

        theta0 = np.random.uniform(-np.pi / 6, np.pi / 6)
        theta1 = np.random.uniform(-np.pi, np.pi)
        self.state = np.array([np.sin(theta0), np.cos(theta0), 0.0, np.sin(theta1), np.cos(theta1), 0.0], dtype=np.float32)
        self.step_count = 0
        return self.state[:-1], {}

    def step(self, action):
        m_1, l_1, I_1, m_0, L_0, I_0, g, b_0, b_1, k, K_m, R_m = [self.params[p] for p in self.params]
        voltage = np.clip(action[0], -self.action_limit, self.action_limit)
        torque = K_m * (voltage - K_m * self.state[2]) / R_m

        def dynamics(t, x):
            s0, c0, d0, s1, c1, d1 = x
            alpha = I_0 + m_1 * L_0 ** 2 + m_1 * l_1 ** 2 * s1 ** 2
            beta = -m_1 * l_1 ** 2 * (2 * s1 * c1)
            gamma = -m_1 * L_0 * l_1 * c1
            sigma = m_1 * L_0 * l_1 * s1
            M = np.array([[-alpha, -gamma], [-gamma, -(I_1 + m_1 * l_1 ** 2)]])
            f = np.array([-torque + b_0 * d0 + k * np.arctan2(s0, c0) + sigma * d1 ** 2 - beta * d0 * d1,
                          b_1 * d1 + m_1 * g * l_1 * s1 + 0.5 * beta * d0 ** 2])
            acc = np.linalg.solve(M, f)
            return [d0 * c0, -d0 * s0, acc[0], d1 * c1, -d1 * s1, acc[1]]

        sol = solve_ivp(dynamics, [0, self.dt], self.state, method='RK45')
        self.state = sol.y[:, -1]

        s0, c0, d0, s1, c1, d1 = self.state

        x_cm_dot = -L_0 * s0 * d0 - l_1 * c1 * d1 * s0 - l_1 * d0 * s1 * c0
        y_cm_dot = L_0 * c0 * d0 + l_1 * c1 * d1 * c0 - l_1 * d0 * s1 * s0
        z_cm_dot = -d1 * l_1 * s1
        T = 0.5 * I_0 * d0 ** 2 + 0.5 * m_1 * (x_cm_dot ** 2 + y_cm_dot ** 2 + z_cm_dot ** 2) + 0.5 * I_1 * d1 ** 2
        V = m_1 * g * l_1 * (1 - c1)
        upright_reward = -2.0 * c1
        pos_penalty = -0.1 * np.tanh(arctan2(s0, c0) ** 2 / 2.0)
        upright_closeness = np.exp(-10.0 * (abs(arctan2(s1, c1)) - np.pi) ** 2)
        stability_factor = np.exp(-1.0 * d1 ** 2)
        downright_closeness = np.exp(-1.0 * abs(arctan2(s1, c1)) ** 2)
        stability_0 = np.exp(-1.0 * d0 ** 2)
        bonus = (10.0 * upright_closeness * stability_factor +
                 -10.0 * downright_closeness * stability_factor +
                 5.0 * upright_closeness * stability_0)
        limit_distance = np.clip(0.8 - 0.2 * (self.max_theta_0 - abs(arctan2(s0, c0))), 0, 1)
        limit_penalty = -15.0 * limit_distance ** 3
        energy_reward = 2 - 0.15 * abs(m_1 * g * l_1 * (c1 + 1.0) + 0.5 * I_1 * d1 ** 2)
        reward = 20.0 + upright_reward + pos_penalty + bonus + limit_penalty + energy_reward

        self.step_count += 1
        done = abs(np.arctan2(s0, c0)) > self.max_theta_0 or self.step_count >= self.max_steps
        truncated = self.step_count >= self.max_steps
        return self.state[:-1], reward, done, truncated, {}

# Train with RNN policy
env = DummyVecEnv([lambda: QubeServo2Env()])
model = SAC(
    policy=CustomSACPolicy,
    env=env,
    learning_rate=3e-4,
    buffer_size=400000,
    batch_size=1024,
    tau=0.005,
    gamma=0.99,
    ent_coef="auto",
    verbose=1,
    tensorboard_log="./tensorboard_logs/",
    device=device
)
callback = TrainingMonitorCallback(check_freq=1000, patience=10, loss_threshold=0.01, verbose=1)
try:
    model.learn(total_timesteps=2000000, callback=callback)
except KeyboardInterrupt:
    print("Training interrupted! Model saved")
model.save("pendulum_sac_rnn_pomdp")

# Test and collect data
env = QubeServo2Env()
obs, _ = env.reset()
rewards = []
thetas = []
alphas = []
voltages = []
times = []

for i in range(1000):
    action, _states = model.predict(obs, deterministic=True)
    obs, reward, done, truncated, _ = env.step(action)
    rewards.append(reward)
    thetas.append(np.degrees(np.arctan2(obs[0], obs[1])))
    alphas.append(np.degrees(np.arctan2(obs[3], obs[4])))
    voltages.append(action[0])
    times.append(i * env.dt)
    if done or truncated:
        obs, _ = env.reset()
        model.policy.reset()

# Plotting results
plt.figure(figsize=(12, 10))
plt.subplot(3, 1, 1)
plt.scatter(times, thetas, label=r'$\theta_0$ (arm angle)')
plt.scatter(times, alphas, label=r'$\theta_1$ (pendulum angle)')
plt.axhline(y=180, color='r', linestyle='--', label=r'$\theta_1 = +-180º$ (up)')
plt.axhline(y=-180, color='r', linestyle='--')
plt.axhline(y=150, color='k', linestyle='--', label=r'$\theta_0$ limits')
plt.axhline(y=-150, color='k', linestyle='--')
plt.xlabel('Time (s)')
plt.ylabel('Angle (degrees)')
plt.legend()
plt.grid(True)

plt.subplot(3, 1, 2)
plt.scatter(times, rewards, label='Reward')
plt.xlabel('Time (s)')
plt.ylabel('Reward')
plt.legend()
plt.grid(True)

plt.subplot(3, 1, 3)
plt.scatter(times, voltages, label='Voltage')
plt.xlabel('Time (s)')
plt.ylabel('Voltage (V)')
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()