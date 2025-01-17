"""
TODO
"""
from __future__ import annotations

import numpy as np

import gym
from gym import spaces


class DataCenterEnv(gym.Env):
    """
    TODO
    """

    metadata = {"render_modes": []}

    def __init__(self):
        raise NotImplementedError

    def step(self, action: np.ndarray) -> tuple:
        raise NotImplementedError

    def reset(self, *,
              seed: int | None = None,
              return_info: bool = False,
              options: dict | None = None) -> dict:
        super().reset(seed=seed)
        raise NotImplementedError

    def render(self):
        raise NotImplementedError

    def close(self):
        return
